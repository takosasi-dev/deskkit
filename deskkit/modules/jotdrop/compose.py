# 書式の検証と展開({date} {time} {text}、J-13)、本文の整え(J-14)、ファイル名のパターンの検証(§9)。
# 形の記号は YYYY MM DD HH mm の5つだけ。str.format は使わない(属性の参照などの道を塞ぐ)。GUI も OS 呼び出しも持たない。
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime

DEFAULT_DATE = "YYYY-MM-DD"
DEFAULT_TIME = "HH:mm"
_TOKENS = ("YYYY", "MM", "DD", "HH", "mm")
NEWLINES = ("\r", "\n", " ", " ", "\x85")
VARIABLES = ("date", "time", "text")


class FormatError(ValueError):
    """書式が使えない。args[0] は利用者に見せる文。"""


@dataclass(frozen=True)
class Part:
    kind: str          # "lit" | "date" | "time" | "text"
    value: str = ""    # lit の文字 / date・time の形


def parse(fmt: str, allowed: tuple[str, ...] = VARIABLES) -> list[Part]:
    """書式を部品に分ける。知らない変数・閉じていない括弧・改行は FormatError。"""
    if any(c in fmt for c in NEWLINES):
        raise FormatError("書式に改行は使えません")
    parts: list[Part] = []
    lit: list[str] = []
    i = 0
    while i < len(fmt):
        c = fmt[i]
        if c == "{" and fmt.startswith("{{", i):
            lit.append("{")
            i += 2
            continue
        if c == "}" and fmt.startswith("}}", i):
            lit.append("}")
            i += 2
            continue
        if c == "}":
            raise FormatError("「}」は「}}」と書いてください")
        if c == "{":
            end = fmt.find("}", i + 1)
            if end < 0:
                raise FormatError("「{」が閉じていません")
            body = fmt[i + 1:end]
            if "{" in body:
                raise FormatError("「{」が閉じていません")
            name, sep, spec = body.partition(":")
            if name not in allowed:
                raise FormatError(f"使えない変数です: {{{name}}}")
            if name == "text" and sep:
                raise FormatError("{text} に形は付けられません")
            if lit:
                parts.append(Part("lit", "".join(lit)))
                lit = []
            default = DEFAULT_DATE if name == "date" else DEFAULT_TIME if name == "time" else ""
            parts.append(Part(name, spec if sep else default))
            i = end + 1
            continue
        lit.append(c)
        i += 1
    if lit:
        parts.append(Part("lit", "".join(lit)))
    return parts


def validate_line_format(fmt: str) -> str | None:
    """1行の書式の検証。使えなければ理由の文を返す(J-13。{text} がちょうど1つ)。"""
    try:
        parts = parse(fmt)
    except FormatError as e:
        return str(e.args[0])
    n = sum(1 for p in parts if p.kind == "text")
    if n == 0:
        return "{text}(書いた文)を1つ入れてください"
    if n > 1:
        return "{text} は1つだけにしてください"
    return None


def validate_header(fmt: str) -> str | None:
    """新しいファイルの先頭の行。{date} だけを使える(改行は不可)。"""
    try:
        parse(fmt, ("date",))
    except FormatError as e:
        return str(e.args[0])
    return None


def validate_separator(sep: str) -> str | None:
    if any(c in sep for c in NEWLINES):
        return "区切りの行に改行は使えません"
    if len(sep) > 200:
        return "区切りの行は 200 文字までです"
    return None


def fmt_stamp(shape: str, when: datetime) -> str:
    """YYYY MM DD HH mm を置き換え、それ以外の文字はそのまま出す。"""
    vals = {"YYYY": f"{when.year:04d}", "MM": f"{when.month:02d}", "DD": f"{when.day:02d}",
            "HH": f"{when.hour:02d}", "mm": f"{when.minute:02d}"}
    out: list[str] = []
    i = 0
    while i < len(shape):
        for t in _TOKENS:
            if shape.startswith(t, i):
                out.append(vals[t])
                i += len(t)
                break
        else:
            out.append(shape[i])
            i += 1
    return "".join(out)


def expand(fmt: str, when: datetime, text: str = "", allowed: tuple[str, ...] = VARIABLES) -> str:
    out: list[str] = []
    for p in parse(fmt, allowed):
        if p.kind == "lit":
            out.append(p.value)
        elif p.kind in ("date", "time"):
            out.append(fmt_stamp(p.value, when))
        else:
            out.append(text)
    return "".join(out)


def clean_text(text: str, max_chars: int) -> str:
    """J-14: 改行を空白1つに、タブ以外の制御文字を除き、前後の空白を除く。Markdown の記号はそのまま。"""
    s = text.replace("\r\n", "\n")
    s = re.sub("[\r\n  \x85]", " ", s)
    s = "".join(c for c in s if c == "\t" or unicodedata.category(c) != "Cc")
    s = s.strip()
    return s[:max_chars]


# ------------------------------------------------------------------ ファイル名のパターン(§9)
_BAD_CHARS = set('<>:"/\\|?*')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
EXTENSIONS = (".md", ".txt")


def validate_pattern(pattern: str, today: datetime) -> str | None:
    """ファイル名のパターン。{date} だけを使える。今日の日付で展開した名前が Windows の決まりに合うか(§9・J-16)。"""
    if "\\" in pattern or "/" in pattern:
        return "ファイル名に「\\」「/」は使えません(フォルダは別の欄で選びます)"
    try:
        name = expand(pattern, today, allowed=("date",))
    except FormatError as e:
        return str(e.args[0])
    return validate_filename(name)


def validate_filename(name: str) -> str | None:
    if not name:
        return "ファイル名が空です"
    if any(c in _BAD_CHARS or ord(c) < 32 for c in name):
        return 'ファイル名に < > : " / \\ | ? * と制御文字は使えません'
    if name.endswith((" ", ".")):
        return "ファイル名の最後に空白と「.」は使えません"
    stem = name.split(".", 1)[0].rstrip(" ").upper()
    if stem in _RESERVED:
        return f"「{stem}」は Windows が予約している名前なので使えません"
    if not name.lower().endswith(EXTENSIONS):
        return "拡張子は .md か .txt にしてください"
    return None
