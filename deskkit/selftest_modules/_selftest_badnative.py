# WM_DISPLAYCHANGE のハンドラでわざと例外を出すダミーモジュール(AC-5 用)。
from __future__ import annotations

from deskkit.context import ModuleContextImpl

WM_DISPLAYCHANGE = 0x007E
calls: list[int] = []


class _M:
    def __init__(self, ctx: ModuleContextImpl) -> None:
        self.ctx = ctx

    def start(self) -> None:
        self.ctx.on_native(WM_DISPLAYCHANGE, self._boom)

    def _boom(self, wp: int, lp: int) -> None:
        calls.append(wp)
        raise ValueError("わざと失敗する nativeEvent ハンドラ")

    def stop(self) -> None:
        pass

    def handle_cli(self, args: list[str]) -> tuple[int, str]:
        return 2, "unsupported"


def create(ctx: ModuleContextImpl) -> _M:
    return _M(ctx)
