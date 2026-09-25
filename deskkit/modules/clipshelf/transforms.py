# 変換して貼り付け(v0.2 C2)の変換。純関数で、利用者がパレットで明示的に選んだ1回の貼り付けにだけ使う
# (自動で変換しない。INV-8)。変換の種類 ID はログに出してよいが、変換前後の本文は出さない(INV-1)。
from __future__ import annotations

import re
from collections.abc import Callable

_LINE_BREAKS = re.compile(r"[ \t　]*(?:\r\n|\r|\n)+[ \t　]*")
# 全角英数(Ａ-Ｚ ａ-ｚ ０-９)だけを半角にする。記号・カナ・全角スペースは変えない
_ZEN_ALNUM = {c: c - 0xFEE0 for r in ((0xFF10, 0xFF19), (0xFF21, 0xFF3A), (0xFF41, 0xFF5A)) for c in range(r[0], r[1] + 1)}


def _strip(text: str) -> str:
    return text.strip()


def _join_lines(text: str) -> str:
    """改行(と、その前後の空白)を半角スペース1つにする。続く改行・空行も1つにまとめる。"""
    return _LINE_BREAKS.sub(" ", text)


def _zen_to_han(text: str) -> str:
    return text.translate(_ZEN_ALNUM)


def _first_line(text: str) -> str:
    """空白だけの行を飛ばした最初の行(行末の空白は除く)。"""
    for line in text.splitlines():
        if line.strip():
            return line.rstrip()
    return ""


# (ID, 表示名, 関数)。並び順がパレットの番号(1〜6)になる
TRANSFORMS: tuple[tuple[str, str, Callable[[str], str]], ...] = (
    ("strip", "前後の空白を除く", _strip),
    ("join_lines", "改行を除く(スペースに)", _join_lines),
    ("zen_to_han", "全角英数を半角に", _zen_to_han),
    ("upper", "大文字", str.upper),
    ("lower", "小文字", str.lower),
    ("first_line", "1行目だけ", _first_line),
)
IDS: tuple[str, ...] = tuple(t[0] for t in TRANSFORMS)
LABELS: dict[str, str] = {t[0]: t[1] for t in TRANSFORMS}


def apply(transform_id: str, text: str) -> str:
    for tid, _label, fn in TRANSFORMS:
        if tid == transform_id:
            return fn(text)
    raise ValueError(f"未知の変換です: {transform_id}")
