# 押して確かめる(K-9・FR-12)。選んだ空きの組を ctx.hotkeys.register で try_seconds 秒だけ預かり、押されたら「届きました」。
# 時間切れ・中止・届いた・stop() のどれでも ctx.hotkeys.unregister で返す(INV-2)。ホットキーの登録の API は直接呼ばない(INV-1)。
from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from typing import Any

from deskkit.modules.keyfree.combos import Combo

HOTKEY_NAME = "try_key"
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

# 結果
RECEIVED, TIMEOUT, CONFLICT, FAILED, CANCELLED = "received", "timeout", "conflict", "failed", "cancelled"


def _precise(timer: Any) -> None:
    """単発のタイマーを精度の高い種類にして始め直す(粗いタイマーは 5% 遅れうる。INV-2 の上限を守るため)。"""
    try:
        from PySide6.QtCore import Qt

        timer.setTimerType(Qt.TimerType.PreciseTimer)
        timer.start()
    except (AttributeError, RuntimeError, TypeError):
        pass


class TryKey:
    def __init__(self, ctx: Any, *, on_change: Callable[[], None], log: logging.Logger | None = None,
                 mono: Callable[[], float] = time.monotonic) -> None:
        self.ctx = ctx
        self.log = log or ctx.log
        self._on_change = on_change
        self._mono = mono
        self.active: Combo | None = None
        self.last: Combo | None = None
        self.result: str | None = None
        self.error = 0
        self._deadline = 0.0
        self._seconds = 0
        self._tick_timer: Any = None
        self._end_timer: Any = None
        self._token = 0

    def remaining(self) -> int:
        if self.active is None:
            return 0
        return max(0, math.ceil(self._deadline - self._mono()))

    def start(self, combo: Combo, seconds: int) -> str | None:
        """預かり始める。登録できなければその結果(CONFLICT / FAILED)を返し、預からない。預かったら None。"""
        if self.active is not None:
            self.finish(CANCELLED)
        self.last = combo
        self.result = None
        self.error = 0
        mods, vk = combo
        try:
            self.ctx.hotkeys.register(HOTKEY_NAME, mods, vk)
        except Exception as e:  # noqa: BLE001 - HotkeyConflictError(1409)・HotkeyError(その他)。overlaykit を import しない
            code = int(getattr(e, "code", 0) or 0)
            self._release()
            self.result = CONFLICT if code == ERROR_HOTKEY_ALREADY_REGISTERED else FAILED
            self.error = code
            self.log.info("try key refused result=%s", self.result)
            self._on_change()
            return self.result
        self._token += 1
        token = self._token
        self.active = combo
        self._seconds = int(seconds)
        self._deadline = self._mono() + self._seconds
        self.ctx.hotkeys.triggered(HOTKEY_NAME).connect(lambda: self._hit(token))
        self._end_timer = self.ctx.start_timer(self._seconds * 1000, lambda: self._timeout(token), single_shot=True)
        _precise(self._end_timer)
        self._tick_timer = self.ctx.start_timer(250, lambda: self._tick(token))
        self.log.info("try key start seconds=%d", self._seconds)
        self._on_change()
        return None

    def _hit(self, token: int) -> None:
        if token == self._token and self.active is not None:
            self.finish(RECEIVED)

    def _timeout(self, token: int) -> None:
        if token == self._token and self.active is not None:
            self.finish(TIMEOUT)

    def _tick(self, token: int) -> None:
        if token != self._token or self.active is None:
            return
        if self._mono() >= self._deadline:   # 念のため: 終わりのタイマーが遅れても上限を超えて預からない
            self.finish(TIMEOUT)
            return
        self._on_change()

    def finish(self, result: str) -> None:
        """預かりを返して結果を残す。何も預かっていなければ何もしない。"""
        if self.active is None:
            return
        self._token += 1
        self.active = None
        self.result = result
        self._release()
        self.log.info("try key end result=%s", result)
        self._on_change()

    def stop(self) -> None:
        self.finish(CANCELLED)

    def _release(self) -> None:
        for t in (self._end_timer, self._tick_timer):
            if t is not None:
                try:
                    t.stop()
                except RuntimeError:
                    pass
        self._end_timer = self._tick_timer = None
        try:
            self.ctx.hotkeys.unregister(HOTKEY_NAME)
        except Exception as e:  # noqa: BLE001 - 返せなくても host がモジュールの停止時に片付ける
            self.log.warning("try key release failed: %s", type(e).__name__)
