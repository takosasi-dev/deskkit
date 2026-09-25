# 一時停止(スヌーズ, H2)。利用者が「30分 / 1時間 / 再開するまで」止めると、モジュールは自動で動く処理の入口で
# ctx.is_snoozed() を見て止まる(手で押した操作は止めない)。状態はメモリだけに持ち、DeskKit を再起動すると解除される。
# 設定で、Windows がプレゼン中・全画面 D3D・通知を控えている(QUNS)間も一時停止扱いにできる(既定オフ)。
from __future__ import annotations

import datetime as _dt
import threading
import time
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

# SHQueryUserNotificationState のうち「一時停止扱い」にする状態(foreground.query_quns の名前)。
# quiet_time(新しいユーザーの初回サインイン後の静かな時間)と app(ストアアプリの全画面)は利用者の意図と限らないので入れない。
QUNS_SNOOZE_STATES = frozenset({"busy", "running_d3d_full_screen", "presentation_mode"})
_QUNS_CACHE_S = 2.0
_QUNS_POLL_MS = 5000


class SnoozeManager(QObject):
    """一時停止の状態を持つ。is_snoozed() はどのスレッドからでも呼べる。changed は実効状態が変わったとき(GUI スレッド)。"""

    changed = Signal()

    def __init__(self, quns_reader: Callable[[], str | None], follow_quns: Callable[[], bool],
                 now: Callable[[], _dt.datetime] = _dt.datetime.now) -> None:
        super().__init__()
        self._quns_reader = quns_reader
        self._follow_quns = follow_quns
        self._now = now
        self._lock = threading.Lock()
        self._manual = False
        self._until: _dt.datetime | None = None
        self._quns_at = 0.0
        self._quns_hit = False
        self._last: tuple[bool, str | None] = (False, None)
        self._expire_timer = QTimer(self)
        self._expire_timer.setSingleShot(True)
        self._expire_timer.timeout.connect(self._expire)
        self._poll = QTimer(self)
        self._poll.setInterval(_QUNS_POLL_MS)
        self._poll.timeout.connect(self._emit_if_changed)
        self.auto_resumed = False  # 直近の解除が時間切れによるものか(通知文の出し分け用)

    # ---- 操作(GUI スレッド)
    def snooze(self, minutes: int | None) -> None:
        """minutes 分だけ一時停止する。None は「再開するまで」。"""
        with self._lock:
            self._manual = True
            self._until = None if minutes is None else self._now() + _dt.timedelta(minutes=max(1, int(minutes)))
        self._expire_timer.stop()
        if self._until is not None:
            ms = int((self._until - self._now()).total_seconds() * 1000)
            self._expire_timer.start(max(0, ms) + 50)
        self.auto_resumed = False
        self._emit_if_changed()

    def resume(self) -> None:
        with self._lock:
            self._manual = False
            self._until = None
        self._expire_timer.stop()
        self.auto_resumed = False
        self._emit_if_changed()

    def sync_settings(self) -> None:
        """QUNS 連動の設定が変わったら呼ぶ。連動中だけ 5 秒ごとに状態を確かめる。"""
        if self._follow_quns():
            if not self._poll.isActive():
                self._poll.start()
        else:
            self._poll.stop()
        with self._lock:
            self._quns_at = 0.0
        self._emit_if_changed()

    def _expire(self) -> None:
        with self._lock:
            due = self._manual and self._until is not None and self._now() >= self._until
            if due:
                self._manual = False
                self._until = None
        if not due and self._manual and self._until is not None:  # タイマーが早く来た(時計の変更など)
            ms = int((self._until - self._now()).total_seconds() * 1000)
            self._expire_timer.start(max(0, ms) + 50)
            return
        self.auto_resumed = bool(due)
        self._emit_if_changed()

    # ---- 参照(任意のスレッド)
    def manual_active(self) -> bool:
        with self._lock:
            if not self._manual:
                return False
            return self._until is None or self._now() < self._until

    def until(self) -> _dt.datetime | None:
        with self._lock:
            return self._until if self._manual else None

    def quns_active(self) -> bool:
        if not self._follow_quns():
            return False
        t = time.monotonic()
        with self._lock:
            if t - self._quns_at < _QUNS_CACHE_S and self._quns_at > 0:
                return self._quns_hit
        try:
            hit = self._quns_reader() in QUNS_SNOOZE_STATES
        except Exception:  # noqa: BLE001 - 判定できなければ止めない
            hit = False
        with self._lock:
            self._quns_at, self._quns_hit = t, hit
        return hit

    def is_snoozed(self) -> bool:
        return self.manual_active() or self.quns_active()

    def payload(self) -> dict[str, Any]:
        """host.snooze_changed の payload。"""
        u = self.until() if self.manual_active() else None
        return {"snoozed": self.is_snoozed(), "until": u.isoformat(timespec="seconds") if u else None}

    def status_text(self) -> str:
        """画面・トレイに出す短い状態文。止まっていなければ空文字。"""
        if self.manual_active():
            u = self.until()
            if u is None:
                return "一時停止中(再開するまで)"
            day = "" if u.date() == self._now().date() else f"{u.month}/{u.day} "
            return f"一時停止中({day}{u:%H:%M} まで)"
        if self.quns_active():
            return "一時停止中(Windows がプレゼン中・通知を控えています)"
        return ""

    def _emit_if_changed(self) -> None:
        cur = (self.is_snoozed(), self.payload()["until"])
        if cur != self._last:
            self._last = cur
            self.changed.emit()
