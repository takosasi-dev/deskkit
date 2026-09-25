# メモリ上の平文キャッシュに対する部分一致検索(FTS は使わない。D-5)。
# 空白区切りの語をすべて含む項目を NFKC 正規化+大文字小文字無視で探し、ピン → last_used_at 降順に並べる(FR-10)。
from __future__ import annotations

import unicodedata
from collections.abc import Iterable

from deskkit.modules.clipshelf.store import Item


def normalize(s: str) -> str:
    return unicodedata.normalize("NFKC", s).casefold()


def _haystack(item: Item) -> str:
    if not item.norm:
        base = item.text if item.name is None else f"{item.name}\n{item.text}"
        item.norm = normalize(base)
    return item.norm


def terms_of(query: str) -> list[str]:
    return [t for t in normalize(query).split() if t]


def sort_key(item: Item) -> tuple[int, float, int]:
    return (0 if item.pinned else 1, -item.last_used_at.timestamp(), -item.id)


def search(items: Iterable[Item], query: str, limit: int | None = None) -> list[Item]:
    terms = terms_of(query)
    hits = [i for i in items if all(t in _haystack(i) for t in terms)] if terms else list(items)
    hits.sort(key=sort_key)
    return hits if limit is None else hits[:limit]
