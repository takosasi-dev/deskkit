# 【任意】自動切替(FR-23): プロセス一覧(exe 名だけ。INV-12)を poll_interval_s ごとに取り、ルールの exe が
# 「動いていない → 動いている」になったらモード適用、on_exit=undo なら「動いている → 動いていない」で undo を頼む。
# v0.2: 電源(AC ⇄ バッテリー)の切り替わりもきっかけにできる(WM_POWERBROADCAST。起動時の状態はきっかけにしない)。
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from deskkit.modules.modeshift.config import TRIGGER_AC, TRIGGER_BATTERY, TRIGGER_EXE, AutoRule

log = logging.getLogger("deskkit.modeshift")

# winuser.h / pbt.h
WM_POWERBROADCAST = 0x0218
PBT_APMPOWERSTATUSCHANGE = 0x000A
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
POWER_WPARAMS = frozenset({PBT_APMPOWERSTATUSCHANGE, PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC})


class AutoSwitcher:
    def __init__(self, list_exes: Callable[[], set[str]], interval_s: float, rules: list[AutoRule],
                 on_appear: Callable[[AutoRule], None], on_vanish: Callable[[AutoRule], None],
                 post_main: Callable[[Callable[[], None]], None]) -> None:
        self._list = list_exes
        self._interval = max(0.5, float(interval_s))
        self._rules = [r for r in rules if r.error is None and r.trigger == TRIGGER_EXE]
        self._on_appear = on_appear
        self._on_vanish = on_vanish
        self._post = post_main
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._prev: set[str] | None = None

    def start(self) -> None:
        if not self._rules:
            return
        self._thread = threading.Thread(target=self._loop, name="modeshift-autoswitch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)

    def poll_once(self) -> None:
        """1回分の比較(テストから直接呼べる)。"""
        try:
            now = self._list()
        except Exception:  # noqa: BLE001
            log.exception("プロセス一覧を取得できません")
            return
        prev = self._prev
        self._prev = now
        if prev is None:
            return
        for r in self._rules:
            if r.exe in now and r.exe not in prev:
                log.info("自動切替: %s が起動 → %s", r.exe, r.mode)
                self._post(self._bind(self._on_appear, r))
            elif r.on_exit == "undo" and r.exe in prev and r.exe not in now:
                log.info("自動切替: %s が終了 → 元に戻す", r.exe)
                self._post(self._bind(self._on_vanish, r))

    @staticmethod
    def _bind(fn: Callable[[AutoRule], None], rule: AutoRule) -> Callable[[], None]:
        def call() -> None:
            fn(rule)
        return call

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self._interval)


class PowerSourceWatcher:
    """電源のきっかけ(v0.2): WM_POWERBROADCAST(電源状態の変化・スリープからの復帰)を受けたら AC かどうかを読み直し、
    切り替わっていればルールを呼ぶ。on_battery ルール = 「AC → バッテリー」で適用し、on_exit=undo なら「バッテリー → AC」で
    元に戻す(on_ac はその逆)。入る側に適用するルールがあれば、そちらを優先して元に戻すは頼まない(1段の undo を
    続けて2回動かさない)。メインスレッド(host の隠しウィンドウ)から呼ばれる。状態が読めない(None)ときは何もしない。"""

    def __init__(self, read_ac: Callable[[], bool | None], rules: list[AutoRule],
                 on_appear: Callable[[AutoRule], None], on_vanish: Callable[[AutoRule], None]) -> None:
        self._read = read_ac
        self._rules = [r for r in rules if r.error is None and r.is_power]
        self._on_appear = on_appear
        self._on_vanish = on_vanish
        self.on_ac: bool | None = self._safe_read()   # 起動時の状態はきっかけにしない

    def _safe_read(self) -> bool | None:
        try:
            return self._read()
        except Exception:  # noqa: BLE001
            log.exception("電源の状態を読めません")
            return None

    def handle(self, wparam: int, _lparam: int) -> None:
        if wparam in POWER_WPARAMS:
            self.check()

    def check(self) -> None:
        now = self._safe_read()
        if now is None:
            return
        prev, self.on_ac = self.on_ac, now
        if prev is None or prev == now or not self._rules:
            return
        enter, leave = (TRIGGER_AC, TRIGGER_BATTERY) if now else (TRIGGER_BATTERY, TRIGGER_AC)
        log.info("自動切替: 電源が %s になった", "AC" if now else "バッテリー")
        entering = [r for r in self._rules if r.trigger == enter]
        if entering:
            self._on_appear(entering[0])
            return
        for r in self._rules:
            if r.trigger == leave and r.on_exit == "undo":
                self._on_vanish(r)
