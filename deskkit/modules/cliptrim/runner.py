# ffmpeg の子プロセスの起動・中止・時間切れ・進捗(T-12・T-13・FR-12・FR-13)。SendPrep の Runner と同じ考えで書き直した(import しない)。
# 引数は配列で渡してシェルを通さない。優先度は BELOW_NORMAL、窓は出さない。標準入力は使わない(concat の一覧だけはパイプで渡す)。
# stderr はここで持つだけで、ログには書かない(画面に最後の1行を出すのは呼ぶ側。INV-3)。
from __future__ import annotations

import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deskkit import ffmpeg as _ff

BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
FLAGS = _ff.CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS
STALL_S = 120.0          # 書き出し: 進捗がこの秒数変わらなければ止める(FR-13)
KILL_WAIT_S = 10.0       # 止めるとき子プロセスの終わりを待つ上限(FR-13)
_STDERR_KEEP = 64 * 1024

Popen = Callable[..., Any]


def arg_path(p: Path) -> str:
    """ffmpeg に渡すパス(常に file: を付ける。- で始まる名前をオプションと取り違えない)。"""
    return _ff.arg_path(p)


def _default_popen(args: list[str], *, stdin: Any, stdout: Any, stderr: Any) -> Any:
    return subprocess.Popen(args, stdin=stdin, stdout=stdout, stderr=stderr, creationflags=FLAGS)


def kill(p: Any) -> None:
    """子プロセスを終わらせ、最大 10 秒待つ。"""
    try:
        if p.poll() is None:
            p.kill()
    except OSError:
        pass
    try:
        p.wait(KILL_WAIT_S)
    except (subprocess.TimeoutExpired, OSError):
        pass


class Token:
    """中止の合図。cancel() はどのスレッドからでも呼べ、結び付いた子プロセスをすぐ終わらせる。"""

    def __init__(self) -> None:
        self._ev = threading.Event()
        self._mu = threading.Lock()
        self._procs: list[Any] = []

    @property
    def cancelled(self) -> bool:
        return self._ev.is_set()

    def cancel(self) -> None:
        with self._mu:
            self._ev.set()
            procs = list(self._procs)
        for p in procs:
            kill(p)

    def attach(self, p: Any) -> bool:
        with self._mu:
            if not self._ev.is_set():
                self._procs.append(p)
                return True
        kill(p)
        return False

    def detach(self, p: Any) -> None:
        with self._mu:
            if p in self._procs:
                self._procs.remove(p)


@dataclass(frozen=True)
class Captured:
    returncode: int | None   # None = 起こせなかった
    stdout: bytes
    stderr: bytes
    cancelled: bool = False
    timed_out: bool = False

    def last_line(self) -> str:
        lines = [s.strip() for s in self.stderr.decode("utf-8", "replace").splitlines() if s.strip()]
        return lines[-1] if lines else ""


def capture(args: list[str], *, timeout: float, token: Token | None = None, stdin: bytes | None = None,
            popen: Popen | None = None) -> Captured:
    """短い読み取り(情報・コマ・検証)。時間切れ・中止で子プロセスを終わらせる。"""
    try:
        p = (popen or _default_popen)(args, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        return Captured(None, b"", b"")
    if token is not None and not token.attach(p):
        return Captured(None, b"", b"", cancelled=True)
    timed_out = False
    try:
        try:
            out, err = p.communicate(input=stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill(p)
            try:
                out, err = p.communicate(timeout=KILL_WAIT_S)
            except (subprocess.TimeoutExpired, ValueError, OSError):
                out, err = b"", b""
        except (OSError, ValueError):
            kill(p)
            out, err = b"", b""
    finally:
        if token is not None:
            token.detach(p)
    cancelled = token is not None and token.cancelled
    return Captured(p.returncode, out or b"", (err or b"")[-_STDERR_KEEP:], cancelled, timed_out)


def stream(args: list[str], on_line: Callable[[str], bool], *, timeout: float, token: Token | None = None,
           popen: Popen | None = None) -> Captured:
    """標準出力を1行ずつ on_line に渡す。on_line が False を返したら、そこで子プロセスを終わらせる(読みすぎない。T-3)。"""
    try:
        p = (popen or _default_popen)(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        return Captured(None, b"", b"")
    if token is not None and not token.attach(p):
        return Captured(None, b"", b"", cancelled=True)
    tail: deque[bytes] = deque(maxlen=40)
    flag = {"timed_out": False}

    def read_err() -> None:
        try:
            for raw in p.stderr:
                tail.append(raw)
        except (OSError, ValueError):
            pass

    def on_timeout() -> None:
        flag["timed_out"] = True
        kill(p)

    te = threading.Thread(target=read_err, daemon=True)
    te.start()
    timer = threading.Timer(timeout, on_timeout)
    timer.daemon = True
    timer.start()
    try:
        for raw in p.stdout:
            if not on_line(raw.decode("ascii", "replace").rstrip("\r\n")):
                kill(p)
                break
    except (OSError, ValueError):
        kill(p)
    finally:
        timer.cancel()
        try:
            p.wait(KILL_WAIT_S)
        except subprocess.TimeoutExpired:
            kill(p)
        te.join(2)
        if token is not None:
            token.detach(p)
    cancelled = token is not None and token.cancelled
    return Captured(p.returncode, b"", b"".join(tail)[-_STDERR_KEEP:], cancelled, flag["timed_out"])


# ------------------------------------------------------------------ 書き出し(進捗・中止・止まったら終わらせる)
@dataclass(frozen=True)
class RunResult:
    returncode: int
    last_line: str        # stderr の最後の1行(画面にだけ出す)
    cancelled: bool = False
    stalled: bool = False  # 進捗が STALL_S 秒変わらなかった
    stderr_text: str = ""  # 失敗の種類を見分けるためだけに使う(ログに書かない)


class Runner:
    """ffmpeg を1回動かす。stop() でいつでも止められる。popen・clock・sleep はテストで差し替える。"""

    def __init__(self, *, popen: Popen | None = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, poll_s: float = 0.1) -> None:
        self._popen = popen or _default_popen
        self._clock = clock
        self._sleep = sleep
        self._poll = poll_s
        self._proc: Any = None
        self._mu = threading.Lock()
        self._stopped = False

    def stop(self) -> None:
        with self._mu:
            self._stopped = True
            p = self._proc
        if p is not None:
            kill(p)

    def run(self, args: list[str], *, duration: float | None, on_progress: Callable[[float], None],
            cancelled: Callable[[], bool], stdin: bytes | None = None, stall_s: float = STALL_S) -> RunResult:
        with self._mu:
            if self._stopped:
                return RunResult(-1, "", cancelled=True)
            try:
                self._proc = self._popen(args, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except OSError:
                return RunResult(-1, "")
        p = self._proc
        tail: deque[str] = deque(maxlen=30)
        state = {"last": None, "changed": self._clock()}
        mu = threading.Lock()

        def read_err() -> None:
            try:
                for raw in p.stderr:
                    s = raw.decode("utf-8", "replace").strip()
                    if s:
                        tail.append(s)
            except (OSError, ValueError):
                pass

        def read_out() -> None:
            try:
                for raw in p.stdout:
                    s = raw.decode("ascii", "replace").strip()
                    if not s.startswith("out_time_us="):
                        continue
                    try:
                        us = int(s.split("=", 1)[1])
                    except ValueError:
                        continue
                    with mu:
                        if us != state["last"]:
                            state["last"] = us
                            state["changed"] = self._clock()
                    if duration:
                        on_progress(max(0.0, min(1.0, us / 1_000_000 / duration)))
            except (OSError, ValueError):
                pass

        def write_in() -> None:
            try:
                if stdin is not None and p.stdin is not None:
                    p.stdin.write(stdin)
                    p.stdin.close()
            except (OSError, ValueError):
                pass

        threads = [threading.Thread(target=f, daemon=True) for f in (read_err, read_out, write_in)]
        for t in threads:
            t.start()
        was_cancelled = stalled = False
        while p.poll() is None:
            if cancelled() or self._stopped:
                was_cancelled = True
                break
            with mu:
                idle = self._clock() - float(state["changed"] or 0.0)
            if idle > stall_s:
                stalled = True
                break
            self._sleep(self._poll)
        if was_cancelled or stalled:
            kill(p)
        try:
            p.wait(KILL_WAIT_S)
        except subprocess.TimeoutExpired:
            kill(p)
        for t in threads:
            t.join(5)
        rc = p.returncode if p.returncode is not None else -1
        return RunResult(rc, tail[-1] if tail else "", was_cancelled, stalled, "\n".join(tail))
