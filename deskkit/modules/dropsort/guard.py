# 危険名の検査(FR-9): 二重拡張子 / 双方向制御文字 / 拡張子直前の連続空白 / リパースポイント。
# 該当したファイルはどのルールにも当てず「要確認」にする(D-8)。安全性を保証するものではない。
from __future__ import annotations

import ntpath
import re
import unicodedata

from deskkit.modules.dropsort._win32 import FILE_ATTRIBUTE_REPARSE_POINT

# 双方向制御文字(LRE/RLE/PDF/LRO/RLO, LRI/RLI/FSI/PDI, LRM/RLM, ALM)
BIDI_CHARS = frozenset("‪‫‬‭‮⁦⁧⁨⁩‎‏؜")
_EXT_LIKE = re.compile(r"^\.(?=[0-9A-Za-z]*[A-Za-z])[0-9A-Za-z]{1,6}$")
_SPACED = re.compile(r"\s{2,}$")

REASONS = ("reparse_point", "bidi_control_char", "spaced_extension", "double_extension")


def check_name(name: str, exec_extensions: frozenset[str]) -> str | None:
    """名前だけで判定できる理由コードを返す。問題なければ None。"""
    if any(c in BIDI_CHARS or unicodedata.category(c) == "Cc" for c in name):
        return "bidi_control_char"
    stem, ext = ntpath.splitext(name)
    if ext and _SPACED.search(stem):
        return "spaced_extension"
    if ext.lower() in exec_extensions:
        inner = ntpath.splitext(stem.rstrip())[1]
        if inner and _EXT_LIKE.match(inner):
            return "double_extension"
    return None


def check(name: str, attrs: int, exec_extensions: frozenset[str]) -> str | None:
    if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
        return "reparse_point"
    return check_name(name, exec_extensions)
