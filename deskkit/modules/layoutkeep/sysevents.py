# WM_DISPLAYCHANGE / WM_POWERBROADCAST(復帰)を ctx.on_native で受け、デバウンス(単発タイマの再始動)する。
# 満了後はシグネチャを取り直し、settle_checks 回連続で同じ値になったら「落ち着いた」として通知する(§10)。
# host の隠しウィンドウ1枚を購読するだけで、自前のウィンドウは作らない(§13 Q-1)。
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

WM_DISPLAYCHANGE = 0x007E
WM_POWERBROADCAST = 0x0218
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
SETTLE_INTERVAL_MS = 1000
MAX_SETTLE_CHECKS = 30


class TimerLike(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def setInterval(self, ms: int) -> None: ...  # noqa: N802


class DisplayWatcher:
    def __init__(self, ctx: Any, debounce_ms: Callable[[], int], settle_checks: Callable[[], int],
                 compute_sig: Callable[[], str | None], on_settled: Callable[[str | None], None],
                 on_kick: Callable[[str], None] | None = None) -> None:
        self.ctx = ctx
        self._debounce_ms = debounce_ms
        self._settle_checks = settle_checks
        self._compute = compute_sig
        self._on_settled = on_settled
        self._on_kick = on_kick
        self._timer: TimerLike | None = None
        self._last: str | None = None
        self._same = 0
        self._checks = 0
        self.pending = False
        self.events = 0  # 受けたイベント数(画面表示用)

    def attach(self) -> None:
        self.ctx.on_native(WM_DISPLAYCHANGE, self._on_display)
        self.ctx.on_native(WM_POWERBROADCAST, self._on_power)

    def _on_display(self, _wp: int, _lp: int) -> None:
        self.kick("display")

    def _on_power(self, wp: int, _lp: int) -> None:
        if wp in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
            self.kick("resume")

    def kick(self, why: str) -> None:
        """イベントを受けた: 判定をやり直し、デバウンスタイマを(再)始動する。"""
        self.events += 1
        self._last = None
        self._same = 0
        self._checks = 0
        self.pending = True
        self._arm(self._debounce_ms())
        self.ctx.log.info("構成変化の候補を受信 (%s) → %d ms 待つ", why, self._debounce_ms())
        if self._on_kick is not None:
            self._on_kick(why)

    def _arm(self, ms: int) -> None:
        if self._timer is None:
            self._timer = self.ctx.start_timer(ms, self._fire, single_shot=True)
        else:
            self._timer.stop()
            self._timer.setInterval(ms)
            self._timer.start()

    def _fire(self) -> None:
        if not self.pending:
            return
        sig = self._compute()
        self._checks += 1
        if sig == self._last:
            self._same += 1
        else:
            self._last = sig
            self._same = 1
        if self._same >= max(1, self._settle_checks()):
            self.pending = False
            self.ctx.log.info("構成が落ち着いた sig=%s(%d 回確認)", sig, self._checks)
            self._on_settled(sig)
            return
        if self._checks >= MAX_SETTLE_CHECKS:
            self.pending = False
            self.ctx.log.warning("構成が %d 回確認しても落ち着かないため提案しません", self._checks)
            return
        self._arm(SETTLE_INTERVAL_MS)

    def cancel(self) -> None:
        self.pending = False
        if self._timer is not None:
            self._timer.stop()
