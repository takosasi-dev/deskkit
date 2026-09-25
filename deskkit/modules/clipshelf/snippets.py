# 定型文のプレースホルダ展開(FR-16)。{date} {time} {clipboard} と、v0.2 の入力欄 {input:ラベル}({input:ラベル=既定値})・
# 選択欄 {select:A|B|C}。{{ と }} はそれぞれ { と } の文字。未知の {name} はそのまま残して警告を返す。
# 入力欄・選択欄の値は貼り付けのときにパレットで聞き、展開にだけ使う(ログ・DB・設定に残さない。INV-1/2)。
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

PLACEHOLDERS: tuple[str, ...] = ("date", "time", "clipboard", "input:ラベル", "select:A|B|C")
_TOKEN = re.compile(r"\{\{|\}\}|\{(input|select):([^{}\r\n]*)\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")
FIELD_INPUT = "input"
FIELD_SELECT = "select"
DEFAULT_INPUT_LABEL = "入力"


@dataclass(frozen=True)
class Field:
    """貼り付け時に聞く欄。同じ key の欄は1つにまとめ、同じ値を全部の出現箇所に入れる。"""

    key: str                       # "input:ラベル" / "select:A|B|C"
    kind: str                      # FIELD_INPUT / FIELD_SELECT
    label: str
    default: str = ""
    options: tuple[str, ...] = ()


@dataclass
class Expansion:
    text: str
    warnings: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    used_clipboard: bool = False
    fields: list[Field] = field(default_factory=list)


def uses_clipboard(template: str) -> bool:
    return any(m.group(3) == "clipboard" for m in _TOKEN.finditer(template))


def _parse_field(kind: str, body: str) -> Field | None:
    if kind == FIELD_INPUT:
        label, _sep, default = body.partition("=")
        label = label.strip() or DEFAULT_INPUT_LABEL
        return Field(f"{FIELD_INPUT}:{label}", FIELD_INPUT, label, default.strip())
    options = tuple(o.strip() for o in body.split("|") if o.strip())
    if not options:
        return None  # 選択肢の無い {select:} は欄にしない(警告してそのまま残す)
    return Field(f"{FIELD_SELECT}:{'|'.join(options)}", FIELD_SELECT, "", options[0], options)


def fields(template: str) -> list[Field]:
    """テンプレートに出てくる入力欄・選択欄(出現順・重複なし)。選択欄のラベルは「選択 1」から順に付ける。"""
    out: list[Field] = []
    seen: set[str] = set()
    n_select = 0
    for m in _TOKEN.finditer(template):
        kind = m.group(1)
        if kind is None:
            continue
        f = _parse_field(kind, m.group(2))
        if f is None or f.key in seen:
            continue
        seen.add(f.key)
        if f.kind == FIELD_SELECT:
            n_select += 1
            f = Field(f.key, f.kind, f"選択 {n_select}", f.default, f.options)
        out.append(f)
    return out


def expand(template: str, now: datetime, clipboard: str | None, *, date_format: str = "%Y-%m-%d",
           time_format: str = "%H:%M", clipboard_marker: str | None = None,
           values: Mapping[str, str] | None = None) -> Expansion:
    """テンプレートを展開する。clipboard_marker を渡すと {clipboard} を実際の内容ではなく目印で置き換える
    (設定画面・パレットのプレビュー用。プレビューのためにクリップボードの本文を読まない)。
    values が None なら入力欄・選択欄を〔ラベル〕の目印にする(プレビュー用)。辞書なら key の値を入れ、
    無い key は既定値(入力欄は既定値か空、選択欄は最初の選択肢)にする。値の中の { } は展開しない(1回だけ置換)。"""
    out: list[str] = []
    exp = Expansion("")
    exp.fields = fields(template)
    by_key = {f.key: f for f in exp.fields}
    pos = 0
    for m in _TOKEN.finditer(template):
        out.append(template[pos:m.start()])
        tok = m.group(0)
        kind = m.group(1)
        name = m.group(3)
        if tok == "{{":
            out.append("{")
        elif tok == "}}":
            out.append("}")
        elif kind is not None:
            parsed = _parse_field(kind, m.group(2))
            f = by_key.get(parsed.key) if parsed is not None else None
            if f is None:
                out.append(tok)
                _warn(exp, f"選択肢の無い {tok} はそのまま残ります({{select:A|B|C}} の形で書いてください)")
            elif values is None:
                out.append(f"〔{f.label}〕")
            else:
                out.append(values.get(f.key, f.default))
        elif name == "date":
            out.append(_strftime(now, date_format, exp))
        elif name == "time":
            out.append(_strftime(now, time_format, exp))
        elif name == "clipboard":
            exp.used_clipboard = True
            if clipboard_marker is not None:
                out.append(clipboard_marker)
            else:
                if not clipboard:
                    _warn(exp, "クリップボードにテキストが無いため {clipboard} は空になります")
                out.append(clipboard or "")
        else:
            out.append(tok)
            if name not in exp.unknown:
                exp.unknown.append(name)
                _warn(exp, f"未知のプレースホルダ {{{name}}} はそのまま残ります")
        pos = m.end()
    out.append(template[pos:])
    exp.text = "".join(out)
    return exp


def _warn(exp: Expansion, msg: str) -> None:
    if msg not in exp.warnings:
        exp.warnings.append(msg)


def _strftime(now: datetime, fmt: str, exp: Expansion) -> str:
    try:
        return now.strftime(fmt)
    except ValueError:
        _warn(exp, "日付・時刻の書式が不正です(設定の date_format / time_format を確認してください)")
        return ""
