# EyeBreak(休憩の声かけ)の入口。create(ctx) だけを公開する。
# 重い import は create の中でもしない。最初に使うときに読む(v0.3 共通 NFR-5)。
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from deskkit.modules.eyebreak.module import EyeBreakModule


def create(ctx: Any) -> EyeBreakModule:
    from deskkit.modules.eyebreak.module import EyeBreakModule

    return EyeBreakModule(ctx)
