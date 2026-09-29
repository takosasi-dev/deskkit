# サムネイルのメモリ上の LRU(P-13・INV-6)。最大 300 枚。ディスクには書かない。GUI スレッドからだけ使う。
# 値は QImage(描けなかった物は None。「表示できません」を出す印)。
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Hashable
from typing import Any

MAX_THUMBS = 300


class ThumbCache:
    def __init__(self, max_items: int = MAX_THUMBS) -> None:
        self._max = max_items
        self._d: OrderedDict[Hashable, Any] = OrderedDict()

    def __len__(self) -> int:
        return len(self._d)

    def __contains__(self, key: Hashable) -> bool:
        return key in self._d

    def get(self, key: Hashable) -> tuple[bool, Any]:
        """(あるか, 値)。あれば新しい方へ動かす。"""
        if key in self._d:
            self._d.move_to_end(key)
            return True, self._d[key]
        return False, None

    def put(self, key: Hashable, value: Any) -> None:
        self._d[key] = value
        self._d.move_to_end(key)
        while len(self._d) > self._max:
            self._d.popitem(last=False)

    def drop(self, pred: Any) -> None:
        for k in [k for k in self._d if pred(k)]:
            del self._d[k]

    def clear(self) -> None:
        self._d.clear()
