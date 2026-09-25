# TwinSweep(似た写真の整理)の入口。create(ctx) だけを公開する。
# 重い import(Pillow・numpy・winrt など)は create の中でもしない。最初に使うときに読む(v0.3 共通 NFR-5)。
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from deskkit.modules.twinsweep.module import TwinSweepModule


def create(ctx: Any) -> TwinSweepModule:
    from deskkit.modules.twinsweep.module import TwinSweepModule

    return TwinSweepModule(ctx)
