# ModeShift — 作業モードのワンタッチ切替(電源プラン・音量・アプリ起動/終了・配置イベント)。
# host からは create(ctx) だけが呼ばれる。重い import(Qt 画面・COM)は create の中で遅延する。
from __future__ import annotations

from typing import Any


def create(ctx: Any) -> Any:
    from deskkit.modules.modeshift.module import ModeShiftModule

    return ModeShiftModule(ctx)
