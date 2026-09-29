# 画面の裏で動かす読み取りの通り道(T-12: コマ・フィルムストリップ・まわりの読み取り・開く、の各1本まで)。
# 1本の通り道は1つのスレッドで順に動かし、同じ鍵の新しい頼みが来たら古い頼み(動いている子プロセスも)を捨てる。
# 結果は post(= ctx.call_soon)で GUI スレッドへ返す。捨てた頼みの結果は返さない。
from __future__ import annotations

import functools
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from deskkit.modules.cliptrim import runner

Done = Callable[[Any, BaseException | None], None]


@dataclass(eq=False)
class _Item:
    work: Callable[[runner.Token], Any]
    done: Done
    key: str | None
    token: runner.Token = field(default_factory=runner.Token)


def _deliver(item: _Item, res: Any, err: BaseException | None) -> None:
    if not item.token.cancelled:  # GUI スレッドに着く前に捨てられた頼みの結果は返さない
        item.done(res, err)


class Lane:
    def __init__(self, name: str, post: Callable[[Callable[[], None]], None], *, delay_s: float = 0.0,
                 clock: Callable[[], float] = time.monotonic, idle_exit_s: float = 30.0) -> None:
        self.name = name
        self._post = post
        self._delay = delay_s
        self._clock = clock
        self._idle_exit = idle_exit_s
        self._q: deque[_Item] = deque()
        self._cv = threading.Condition()
        self._running: _Item | None = None
        self._thread: threading.Thread | None = None
        self._last_submit = 0.0
        self._stopped = False

    def submit(self, work: Callable[[runner.Token], Any], done: Done, *, key: str | None = None) -> runner.Token:
        item = _Item(work, done, key)
        cancel: list[_Item] = []
        with self._cv:
            if self._stopped:
                item.token.cancel()
                return item.token
            if key is not None:
                for old in [i for i in self._q if i.key == key]:
                    self._q.remove(old)
                    cancel.append(old)
                if self._running is not None and self._running.key == key:
                    cancel.append(self._running)
            self._q.append(item)
            self._last_submit = self._clock()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name=f"cliptrim-{self.name}", daemon=True)
                self._thread.start()
            self._cv.notify_all()
        for old in cancel:
            old.token.cancel()
        return item.token

    def busy(self) -> bool:
        with self._cv:
            return self._running is not None or bool(self._q)

    def clear(self) -> None:
        """待っている頼みを捨て、動いている頼みを止める(動画を開き直したとき)。"""
        with self._cv:
            items = list(self._q)
            self._q.clear()
            if self._running is not None:
                items.append(self._running)
        for it in items:
            it.token.cancel()

    def stop(self, timeout: float = 12.0) -> None:
        with self._cv:
            self._stopped = True
            self._cv.notify_all()
        self.clear()
        th = self._thread
        if th is not None and th is not threading.current_thread():
            th.join(timeout)

    def _loop(self) -> None:
        while True:
            with self._cv:
                while not self._q and not self._stopped:
                    if not self._cv.wait(self._idle_exit) and not self._q:
                        self._thread = None
                        return
                if self._stopped:
                    self._thread = None
                    return
                # 最後の頼みから delay 秒待つ(その間に来た新しい頼みが古い頼みを置き換える。T-4 の 150ms)
                while self._delay > 0 and not self._stopped:
                    rest = self._last_submit + self._delay - self._clock()
                    if rest <= 0:
                        break
                    self._cv.wait(rest)
                if self._stopped or not self._q:
                    continue
                item = self._q.popleft()
                self._running = item
            res: Any = None
            err: BaseException | None = None
            if not item.token.cancelled:
                try:
                    res = item.work(item.token)
                except Exception as e:  # noqa: BLE001 - 結果として GUI スレッドへ返す
                    err = e
            with self._cv:
                self._running = None
            if not item.token.cancelled:
                self._post(functools.partial(_deliver, item, res, err))
