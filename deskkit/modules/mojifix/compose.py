# 分かれた濁点・半濁点(NFD)をくっつける規則(M-13)。NFC が「1字を別の1字に置き換える」字(CJK 互換漢字など。P-12)で
# 文字列を区切り、残りにだけ NFC を当てる。「神」U+FA19 は変わらず、「か」+U+3099 は「が」になる。U+309B「゛」はくっつけない。
from __future__ import annotations

import functools
import sys
import unicodedata


@functools.lru_cache(maxsize=1)
def singletons() -> frozenset[str]:
    """NFC で別の1字に置き換わる字の集合(約 1,035 字)。初めて使うときに1回だけ作る。"""
    out: set[str] = set()
    for cp in range(sys.maxunicode + 1):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        d = unicodedata.decomposition(chr(cp))
        if not d or d.startswith("<"):
            continue  # 正規分解を持たない字は NFC で変わらない(互換分解は NFC に効かない)
        c = chr(cp)
        n = unicodedata.normalize("NFC", c)
        if len(n) == 1 and n != c:
            out.add(c)
    return frozenset(out)


def compose(text: str) -> str:
    """M-13 のくっつけ。変わらなければ同じ文字列を返す。"""
    if text.isascii() or unicodedata.is_normalized("NFC", text):
        return text
    single = singletons()
    parts: list[str] = []
    buf: list[str] = []
    for ch in text:
        if ch in single:
            if buf:
                parts.append(unicodedata.normalize("NFC", "".join(buf)))
                buf = []
            parts.append(ch)  # 置き換わる字はそのまま残す
        else:
            buf.append(ch)
    if buf:
        parts.append(unicodedata.normalize("NFC", "".join(buf)))
    return "".join(parts)


def separated_positions(text: str) -> list[int]:
    """画面で強調する「分かれた文字」(結合する記号・ハングルの字母)の位置。"""
    out: list[int] = []
    for i, ch in enumerate(text):
        o = ord(ch)
        if unicodedata.combining(ch) or 0x1160 <= o <= 0x11FF:
            out.append(i)
    return out
