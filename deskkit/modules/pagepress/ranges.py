# ページ範囲の文字列の解釈(FR-6。純粋な関数)。`1-3, 5, 8-` のようにカンマで区切った範囲ごとに1ファイル。
# 全角の数字と `－`・`ー`・`〜`・`~`、区切りの `、` も受け付ける。誤りは画面にそのまま出せる文の RangeError にする。
from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass

MSG_EMPTY = "範囲を書いてください"
MSG_NOT_NUMBER = "数字で書いてください"
MSG_ZERO = "ページは 1 から数えます"
MSG_OVER_ALL = "範囲がページ数を超えています"

_DASHES = "-‐‑‒–—―−ー〜~～－"
_COMMAS = ",、，､"


class RangeError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Span:
    """1 から数えたページの範囲。end が None なら最後まで(`8-`)。"""

    start: int
    end: int | None

    def resolve(self, pages: int) -> tuple[int, int]:
        end = pages if self.end is None else self.end
        if self.start > pages or end > pages:
            raise RangeError(MSG_OVER_ALL)
        return self.start, end


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    for d in _DASHES:
        t = t.replace(d, "-")
    for c in _COMMAS:
        t = t.replace(c, ",")
    return "".join(t.split())  # 空白は全部捨てる


def _num(s: str) -> int:
    if not s.isascii() or not s.isdigit():
        raise RangeError(MSG_NOT_NUMBER)
    n = int(s)
    if n == 0:
        raise RangeError(MSG_ZERO)
    return n


def parse(text: str, pages: int | None = None) -> list[Span]:
    """範囲の一覧。pages を渡すと、それを超える数を「N ページまでしかありません」で断る。"""
    t = _norm(text)
    if not t.strip(","):
        raise RangeError(MSG_EMPTY)
    spans: list[Span] = []
    for tok in t.split(","):
        if not tok:
            continue
        if tok.count("-") > 1:
            raise RangeError(MSG_NOT_NUMBER)
        if "-" in tok:
            a, b = tok.split("-")
            if not a and not b:
                raise RangeError(MSG_NOT_NUMBER)
            start = _num(a) if a else 1
            end = _num(b) if b else None
            if end is not None and start > end:
                raise RangeError(f"{start}-{end} の順が逆です")
        else:
            start = end = _num(tok)
        for n in (start, end):
            if pages is not None and n is not None and n > pages:
                raise RangeError(f"{pages} ページまでしかありません")
        spans.append(Span(start, end))
    if not spans:
        raise RangeError(MSG_EMPTY)
    return spans


def every(pages: int, n: int) -> list[tuple[int, int]]:
    """N ページごと(最後は余り)。"""
    return [(s, min(s + n - 1, pages)) for s in range(1, pages + 1, n)]


def single(pages: int) -> list[tuple[int, int]]:
    return [(i, i) for i in range(1, pages + 1)]


def count_every(pages: int, n: int) -> int:
    return math.ceil(pages / n) if pages > 0 and n > 0 else 0
