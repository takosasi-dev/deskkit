# 見張りのスレッド(W-3・FR-3・FR-4・FR-15・INV-7)。鍵には RegNotifyChangeKeyValue、フォルダには FindFirstChangeNotificationW を置き、
# 止める event・読み直しの event と一緒に WaitForMultipleObjects で待つ。合図が来たら知らせを掛け直し、settle_ms の間ほかの合図が
# 来なくなるまで待って(最初の合図から最大 10 秒)、scan(理由) を呼ぶ。知らせを置けない場所があるときだけ poll_minutes ごとに起きる。
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from deskkit.modules.startupwatch import _win32
from deskkit.modules.startupwatch.sources import LOCATIONS, Location

MAX_SETTLE_MS = 10_000
STOP_WAIT_S = 2.0

MODE_NOTIFY = "notify"
MODE_POLL = "poll"
MODE_UNREADABLE = "unreadable"


@dataclass
class _Armed:
    loc: Location
    handle: int      # 鍵(reg)か、変更の知らせ(folder)
    event: int = 0   # reg だけ: 知らせを受ける event
    closed: bool = False

    @property
    def wait_handle(self) -> int:
        return self.event if self.loc.kind == "reg" else self.handle


class WatcherError(RuntimeError):
    pass


class Watcher:
    def __init__(self, api: _win32.Api, *, settle_ms: int, poll_minutes: int, scan: Callable[[str], None],
                 log: logging.Logger, clock: Callable[[], float] = time.monotonic,
                 on_state: Callable[[], None] | None = None) -> None:
        self.api = api
        self.settle_ms = int(settle_ms)
        self.poll_ms = int(poll_minutes) * 60_000
        self._scan_cb = scan
        self.log = log
        self._clock = clock
        self._on_state = on_state or (lambda: None)
        self.modes: dict[str, str] = {loc.key: MODE_POLL for loc in LOCATIONS}
        self.degraded = False
        self.failures = 0
        self._armed: dict[str, _Armed] = {}
        self._stop_flag = threading.Event()
        self._wake = threading.Event()
        self._kick_flag = threading.Event()
        self._stop_ev = 0
        self._kick_ev = 0
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ 外から(GUI スレッド)
    def start(self, *, threaded: bool = True) -> None:
        self._stop_ev = self.api.create_event()
        self._kick_ev = self.api.create_event()
        if threaded:
            self._thread = threading.Thread(target=self._main, name="startupwatch-watch", daemon=True)
            self._thread.start()
        else:
            self._main()

    def stop(self, timeout: float = STOP_WAIT_S) -> bool:
        """止める event を合図し、スレッドの終わりを最大 timeout 秒待つ。終わっていれば event も閉じる。"""
        self._stop_flag.set()
        self._wake.set()
        if self._stop_ev:
            self.api.set_event(self._stop_ev)
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout)
            if t.is_alive():
                self.log.warning("watcher did not stop in time")
                return False
        for h in (self._stop_ev, self._kick_ev):
            if h:
                self.api.close_handle(h)
        self._stop_ev = self._kick_ev = 0
        return True

    def kick(self) -> None:
        """読み直しを頼む(「読み直す」・一時停止の解除)。"""
        self._kick_flag.set()
        self._wake.set()
        if self._kick_ev:
            self.api.set_event(self._kick_ev)

    @property
    def stopping(self) -> bool:
        return self._stop_flag.is_set()

    # ------------------------------------------------------------ スレッド
    def _main(self) -> None:
        first = True
        while not self._stop_flag.is_set():
            try:
                self._run(first)
                return
            except Exception as e:  # noqa: BLE001 - §10: 1回だけ作り直す
                self.failures += 1
                self.log.warning("watcher failed: %s (n=%d)", type(e).__name__, self.failures)
                first = False
                if self.failures >= 2:
                    break
        if self._stop_flag.is_set():
            return
        self.degraded = True
        self.modes = {k: (MODE_UNREADABLE if v == MODE_UNREADABLE else MODE_POLL) for k, v in self.modes.items()}
        self._on_state()
        self._degraded_loop()

    def _run(self, first: bool) -> None:
        try:
            for loc in LOCATIONS:
                self._arm(loc)
            self._on_state()
            self._scan("start" if first else "restart")
            self._loop()
        finally:
            self._disarm_all()

    def _degraded_loop(self) -> None:
        """見張りを続けられないとき: poll_minutes ごとの読み直しだけ(§10)。"""
        while not self._stop_flag.is_set():
            self._wake.wait(self.poll_ms / 1000.0)
            self._wake.clear()
            if self._stop_flag.is_set():
                return
            reason = "manual" if self._kick_flag.is_set() else "poll"
            self._kick_flag.clear()
            self._scan(reason)

    def _scan(self, reason: str) -> None:
        if not self._stop_flag.is_set():
            self._scan_cb(reason)

    def _handles(self) -> tuple[list[int], list[_Armed]]:
        armed = list(self._armed.values())
        return [self._stop_ev, self._kick_ev, *[a.wait_handle for a in armed]], armed

    def _poll_needed(self) -> bool:
        return any(m != MODE_NOTIFY for m in self.modes.values())

    def _loop(self) -> None:
        while True:
            handles, armed = self._handles()
            timeout = self.poll_ms if self._poll_needed() else _win32.INFINITE
            idx = self.api.wait_any(handles, timeout)
            if idx == 0 or self._stop_flag.is_set():
                return
            if idx == _win32.WAIT_RESULT_TIMEOUT:
                self._retry_poll_locations()
                self._scan("poll")
                continue
            if idx < 0:
                raise WatcherError("wait failed")
            if idx == 1:
                self._kick_flag.clear()
                self._scan("manual")
                continue
            self._rearm(armed[idx - 2])
            if not self._settle():
                return
            self._scan("notify")

    def _settle(self) -> bool:
        """FR-3: settle_ms の間に次の合図が来たら待ち直す。最初の合図から 10 秒で打ち切る。False は止める合図。"""
        first = self._clock()
        while True:
            elapsed = (self._clock() - first) * 1000.0
            if elapsed >= MAX_SETTLE_MS:
                return True
            wait = int(max(1.0, min(float(self.settle_ms), MAX_SETTLE_MS - elapsed)))
            handles, armed = self._handles()
            idx = self.api.wait_any(handles, wait)
            if idx == 0 or self._stop_flag.is_set():
                return False
            if idx == _win32.WAIT_RESULT_TIMEOUT:
                return True
            if idx < 0:
                raise WatcherError("wait failed")
            if idx == 1:
                self._kick_flag.clear()
                return True
            self._rearm(armed[idx - 2])

    # ------------------------------------------------------------ 知らせの置き方
    def _arm(self, loc: Location) -> None:
        if loc.kind == "reg":
            self._arm_reg(loc)
        else:
            self._arm_folder(loc)

    def _arm_reg(self, loc: Location, event: int = 0) -> None:
        rc, hkey = self.api.reg_open_notify(loc.root, loc.subkey, loc.view)
        if rc != 0:
            if event:
                self.api.close_handle(event)
            # 鍵が無い(2)は読み直しで見る。開けない(5 など)は読めない場所
            self.modes[loc.key] = MODE_POLL if rc == _win32.ERROR_FILE_NOT_FOUND else MODE_UNREADABLE
            self.log.info("arm loc=%s rc=%d", loc.key, rc)
            return
        ev = event or self.api.create_event()
        nrc = self.api.reg_notify(hkey, ev)
        if nrc != 0:
            self.api.reg_close(hkey)
            self.api.close_handle(ev)
            self.modes[loc.key] = MODE_POLL
            self.log.info("notify loc=%s rc=%d", loc.key, nrc)
            return
        self._armed[loc.key] = _Armed(loc, hkey, ev)
        self.modes[loc.key] = MODE_NOTIFY

    def _arm_folder(self, loc: Location) -> None:
        path = self.api.known_folder(loc.folder)
        if not path:
            self.modes[loc.key] = MODE_POLL
            return
        try:
            remote = self.api.is_remote(path)
        except OSError:
            remote = False
        if remote:  # §10: ネットワークの場所には知らせを置かない
            self.modes[loc.key] = MODE_POLL
            return
        h = self.api.find_first_change(path)
        if h is None:
            self.modes[loc.key] = MODE_POLL
            self.log.info("arm loc=%s failed", loc.key)
            return
        self._armed[loc.key] = _Armed(loc, h)
        self.modes[loc.key] = MODE_NOTIFY

    def _rearm(self, a: _Armed) -> None:
        """合図のあと、その場所の知らせを掛け直す。鍵は消された・作り直されたときに備えて開き直す(§10)。"""
        self._armed.pop(a.loc.key, None)
        if a.loc.kind == "reg":
            self.api.reg_close(a.handle)
            self._arm_reg(a.loc, a.event)
        else:
            if self.api.find_next_change(a.handle):
                self._armed[a.loc.key] = a
                return
            self.api.find_close_change(a.handle)
            self._arm_folder(a.loc)
        self._on_state()

    def _retry_poll_locations(self) -> None:
        changed = False
        for loc in LOCATIONS:
            if loc.key not in self._armed and self.modes.get(loc.key) != MODE_NOTIFY:
                before = self.modes.get(loc.key)
                self._arm(loc)
                changed = changed or before != self.modes.get(loc.key)
        if changed:
            self._on_state()

    def _disarm_all(self) -> None:
        for a in list(self._armed.values()):
            self._close(a)
        self._armed.clear()

    def _close(self, a: _Armed) -> None:
        if a.closed:
            return
        a.closed = True
        if a.loc.kind == "reg":
            self.api.reg_close(a.handle)
            self.api.close_handle(a.event)
        else:
            self.api.find_close_change(a.handle)
