# 正常に動くダミーモジュール。トレイ項目・イベント購読・CLI 応答を持つ(host selftest 用)。
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from deskkit.context import ModuleContextImpl

received: list[Mapping[str, Any]] = []
tray_hits: list[int] = []


class _M:
    def __init__(self, ctx: ModuleContextImpl) -> None:
        self.ctx = ctx

    def start(self) -> None:
        self.ctx.add_tray_action("ダミー項目", lambda: tray_hits.append(1))
        self.ctx.on("layout.apply", lambda p: received.append(p))
        self.ctx.set_tray_status("ダミー動作中")

    def stop(self) -> None:
        pass

    def handle_cli(self, args: list[str]) -> tuple[int, str]:
        return (0, "pong") if args == ["ping"] else (2, "unsupported")


def create(ctx: ModuleContextImpl) -> _M:
    return _M(ctx)
