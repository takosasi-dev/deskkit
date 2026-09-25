# ReadDirectoryChangesW(同期呼び出し)を専用スレッドで回し、「変化あり」の合図だけを出す(D-1 / D-2)。
# 通知の中身は解釈しない。バッファ溢れ(戻り値0バイト / ERROR_NOTIFY_ENUM_DIR)も合図として扱う。
# 停止は CancelIoEx(効かなければ CancelSynchronousIo)でブロッキング呼び出しを解除する。スレッドが死んだら on_dead。
from __future__ import annotations

import ctypes
import logging
import threading
from collections.abc import Callable

from deskkit.modules.dropsort._win32 import ERROR_NOTIFY_ENUM_DIR, ERROR_OPERATION_ABORTED, WatchApi

BUF_SIZE = 64 * 1024


class DirWatcher:
    def __init__(self, api: WatchApi, path: str, on_signal: Callable[[], None], on_dead: Callable[[str], None],
                 log: logging.Logger | None = None) -> None:
        self._api = api
        self.path = path
        self._on_signal = on_signal
        self._on_dead = on_dead
        self._log = log or logging.getLogger("deskkit.dropsort")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._handle: int | None = None
        self._native_id: int | None = None
        self._ready = threading.Event()
        self._hmu = threading.Lock()  # ハンドルを閉じる処理と取り消し要求がぶつからないように

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="dropsort-watcher", daemon=True)
        self._thread.start()
        self._ready.wait(2.0)

    def _run(self) -> None:
        self._native_id = threading.get_native_id()
        reason: str | None = None
        try:
            h, err = self._api.open_dir(self.path)
            if h is None:
                reason = f"監視を開始できません(Win32 エラー {err})"
                return
            self._handle = h
            self._ready.set()
            buf = ctypes.create_string_buffer(BUF_SIZE)
            while not self._stop.is_set():
                ok, n, err = self._api.read_changes(h, buf)
                if self._stop.is_set():
                    break
                if not ok:
                    if err == ERROR_NOTIFY_ENUM_DIR:
                        self._on_signal()  # 溢れ: フルスキャン1回で補う
                        continue
                    if err == ERROR_OPERATION_ABORTED:
                        break
                    reason = f"監視が止まりました(Win32 エラー {err})"
                    break
                self._on_signal()  # n == 0(溢れ)も含め、合図としてだけ使う
        except Exception as e:  # noqa: BLE001 - 監視スレッドの例外はポーリングへの切替で吸収する(FR-18)
            reason = f"監視スレッドで例外: {type(e).__name__}"
            self._log.exception("監視スレッドで例外")
        finally:
            self._ready.set()
            with self._hmu:
                h = self._handle
                self._handle = None
                if h is not None:
                    self._api.close(h)
            if reason is not None and not self._stop.is_set():
                self._log.warning("%s。ポーリングだけで動作を続けます", reason)
                self._on_dead(reason)

    def stop(self, timeout: float = 3.0) -> bool:
        """停止を要求して待つ。止まったら True。"""
        self._stop.set()
        t = self._thread
        if t is None:
            return True
        for _ in range(3):
            with self._hmu:
                h = self._handle
                if h is not None:
                    try:
                        self._api.cancel(h, self._native_id)
                    except OSError:
                        pass
            t.join(timeout / 3)
            if not t.is_alive():
                return True
        self._log.warning("監視スレッドが停止しません(デーモンスレッドのため終了時に破棄されます)")
        return False
