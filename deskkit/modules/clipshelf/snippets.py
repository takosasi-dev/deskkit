# 定型文のプレースホルダ展開(FR-16)。使えるのは {date} {time} {clipboard} だけ(§8.6)。
# {{ と }} はそれぞれ { と } の文字になる。未知の {name} はそのまま残して警告を返す。
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

PLACEHOLDERS: tuple[str, ...] = ("date", "time", "clipboard")
_TOKEN = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class Expansion:
    text: str
    warnings: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    used_clipboard: bool = False


def uses_clipboard(template: str) -> bool:
    return any(m.group(1) == "clipboard" for m in _TOKEN.finditer(template))


def expand(template: str, now: datetime, clipboard: str | None, *, date_format: str = "%Y-%m-%d",
           time_format: str = "%H:%M", clipboard_marker: str | None = None) -> Expansion:
    """テンプレートを展開する。clipboard_marker を渡すと {clipboard} を実際の内容ではなく目印で置き換える
    (設定画面・パレットのプレビュー用。プレビューのためにクリップボードの本文を読まない)。"""
    out: list[str] = []
    exp = Expansion("")
    pos = 0
    for m in _TOKEN.finditer(template):
        out.append(template[pos:m.start()])
        tok = m.group(0)
        name = m.group(1)
        if tok == "{{":
            out.append("{")
        elif tok == "}}":
            out.append("}")
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
