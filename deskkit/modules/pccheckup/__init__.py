# PcCheckup(PC 不調の診断)の入口。create(ctx) だけを公開する。
# 重い import(Pillow・numpy・winrt など)は create の中でもしない。最初に使うときに読む(v0.3 共通 NFR-5)。
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from deskkit.modules.pccheckup.module import PcCheckupModule


def create(ctx: Any) -> PcCheckupModule:
    from deskkit.modules.pccheckup.module import PcCheckupModule

    return PcCheckupModule(ctx)
