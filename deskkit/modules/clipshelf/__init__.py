# ClipShelf(暗号化クリップボード履歴+定型文)の入口。create(ctx) だけを公開する。
# 重い import(Qt・ctypes・sqlite3)は create の中で行い、無効時は何も読み込まない。
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from deskkit.modules.clipshelf.module import ClipShelfModule


def create(ctx: Any) -> ClipShelfModule:
    from deskkit.modules.clipshelf.module import ClipShelfModule

    return ClipShelfModule(ctx)
