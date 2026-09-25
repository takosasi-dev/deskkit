# DropSort(ダウンロード自動整理)モジュールの入口。host は create(ctx) だけを呼ぶ。
# 重い import(Qt・ctypes の定義)はここでせず、create の中で行う。
from __future__ import annotations

from typing import Any


def create(ctx: Any) -> Any:
    from deskkit.modules.dropsort.module import DropSortModule

    return DropSortModule(ctx)
