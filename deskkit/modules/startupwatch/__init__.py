# StartupWatch(自動起動の見張り)の入口。create(ctx) だけを公開する。
# 重い import は create の中でもしない。最初に使うときに読む(v0.3 共通 NFR-5)。
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from deskkit.modules.startupwatch.module import StartupWatchModule


def create(ctx: Any) -> StartupWatchModule:
    from deskkit.modules.startupwatch.module import StartupWatchModule

    return StartupWatchModule(ctx)
