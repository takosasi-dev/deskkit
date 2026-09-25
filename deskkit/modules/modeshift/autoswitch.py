# 【任意】自動切替(FR-23): プロセス一覧(exe 名だけ。INV-12)を poll_interval_s ごとに取り、ルールの exe が
# 「動いていない → 動いている」になったらモード適用、on_exit=undo なら「動いている → 動いていない」で undo を頼む。
# 監視は別スレッド。結果の呼び出しはメインスレッドへ渡す。起動時に既に動いているものはきっかけにしない。
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from deskkit.modules.modeshift.config import AutoRule

log = logging.getLogger("deskkit.modeshift")


class AutoSwitcher:
    def __init__(self, list_exes: Callable[[], set[str]], interval_s: float, rules: list[AutoRule],
                 on_appear: Callable[[AutoRule], None], on_vanish: Callable[[AutoRule], None],
                 post_main: Callable[[Callable[[], None]], None]) -> None:
        self._list = list_exes
        self._interval = max(0.5, float(interval_s))
        self._rules = [r for r in rules if r.error is None]
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
