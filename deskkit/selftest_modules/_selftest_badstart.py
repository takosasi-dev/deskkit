# start() でわざと例外を出すダミーモジュール(AC-4 用)。
from __future__ import annotations

from deskkit.context import ModuleContextImpl


class _M:
    def start(self) -> None:
        raise RuntimeError("わざと失敗する start")

    def stop(self) -> None:
        pass

    def handle_cli(self, args: list[str]) -> tuple[int, str]:
        return 2, "unsupported"


def create(ctx: ModuleContextImpl) -> _M:
    return _M()
