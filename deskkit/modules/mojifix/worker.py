# 重い処理を1つずつ行うワーカー(FR-3)。同時に動くのは1本だけで、段階・進捗・中止を持つ。
# 結果と進捗はワーカーのスレッドから呼ぶので、受け取る側(module)が ctx.call_soon で GUI スレッドへ移す。
# モジュールの停止では中止を出して最大 5 秒待つ(FR-4)。後片づけは各処理が中止を受けて自分で行う。
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

PROGRESS_INTERVAL = 0.1


class Task:
    def __init__(self, kind: str, on_progress: Callable[[str, str, int, int], None]) -> None:
        self.kind = kind
        self._cancel = threading.Event()
        self._on_progress = on_progress
        self._last = 0.0

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        self._cancel.set()

    def progress(self, stage: str, done: int, total: int) -> None:
        now = time.monotonic()
        if done < total and now - self._last < PROGRESS_INTERVAL:
            return
        self._last = now
        self._on_progress(self.kind, stage, done, total)


class Worker:
    def __init__(self, on_progress: Callable[[str, str, int, int], None]) -> None:
        self._on_progress = on_progress
        self._mu = threading.Lock()
        self._thread: threading.Thread | None = None
        self._task: Task | None = None

    def busy(self) -> bool:
        with self._mu:
            return self._task is not None

    def kind(self) -> str | None:
        with self._mu:
            return self._task.kind if self._task is not None else None

    def submit(self, kind: str, fn: Callable[[Task], Any], done: Callable[[Any, BaseException | None], None]) -> bool:
        """動いていなければ fn を別のスレッドで始める。動いていれば False(ほかの開始ボタンは押せない)。"""
        with self._mu:
            if self._task is not None:
                return False
            task = Task(kind, self._on_progress)
            self._task = task

        def run() -> None:
            res: Any = None
            err: BaseException | None = None
            try:
                res = fn(task)
            except BaseException as e:  # noqa: BLE001 - 結果として返す
                err = e
            with self._mu:
                self._task = None
            done(res, err)

        th = threading.Thread(target=run, name=f"mojifix-{kind}", daemon=True)
        with self._mu:
            self._thread = th
        th.start()
        return True

    def cancel(self) -> None:
        with self._mu:
            t = self._task
        if t is not None:
            t.cancel()

    def stop(self, timeout: float = 5.0) -> bool:
        """中止を出して最大 timeout 秒待つ。止まれば True。"""
        self.cancel()
        with self._mu:
            th = self._thread
        if th is not None and th.is_alive():
            th.join(timeout)
            return not th.is_alive()
        return True

    def wait(self, timeout: float = 30.0) -> bool:
        """テスト用: 今の処理が終わるまで待つ。"""
        with self._mu:
            th = self._thread
        if th is not None:
            th.join(timeout)
            return not th.is_alive()
        return True
