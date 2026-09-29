# PagePress(PDF の手元作業)の入口。create(ctx) だけを公開する。
# 重い import は create の中でもしない。最初に使うときに読む(v0.3 共通 NFR-5)。
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from deskkit.modules.pagepress.module import PagePressModule


def create(ctx: Any) -> PagePressModule:
    from deskkit.modules.pagepress.module import PagePressModule

    return PagePressModule(ctx)
