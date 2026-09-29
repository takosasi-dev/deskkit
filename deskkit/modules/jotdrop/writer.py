# 追記・取り消し・照合と、それを順番どおりに1件ずつ行うワーカースレッド(1本)。J-8〜J-12・J-18・J-19。
# 追記のハンドルは FILE_APPEND_DATA だけ(INV-1)。書く前の内容は読むだけで、書く前の大きさより手前は変えない。
# 切り詰めは取り消しの条件(大きさと末尾のバイト列が書いた直後と同じ)がそろったときだけ(INV-2)。ログには理由コードだけ。
from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from deskkit.modules.jotdrop import compose, target
from deskkit.modules.jotdrop._win32 import (
    APPEND_ACCESS,
    CREATE_NEW,
    ERROR_ACCESS_DENIED,
    ERROR_FILE_EXISTS,
    ERROR_FILE_NOT_FOUND,
    ERROR_LOCK_VIOLATION,
    ERROR_PATH_NOT_FOUND,
    ERROR_SHARING_VIOLATION,
    FILE_SHARE_NONE,
    FILE_SHARE_READ,
    OPEN_EXISTING,
    READ_ACCESS,
    READ_SHARE,
    TRUNCATE_ACCESS,
    Win32Api,
)

TAIL_BYTES = 64 * 1024          # J-10: 末尾 64KB を UTF-8 として確かめる
VERIFY_MAX_BYTES = 5 * 1024 * 1024  # J-19: 5MB まで照合する
RETRIES = 5                     # J-9: 200ms おきに 5 回まで
RETRY_WAIT_S = 0.2
UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


@dataclass(frozen=True)
class Framing:
    """足すバイト列の組み方(設定から。J-11・J-12・FR-10)。"""

    newline: str = "auto"          # auto / lf / crlf
    blank_line_before: bool = False
    separator: str = ""
    create_file: bool = True
    header: str = ""               # new_file_header({date} を展開する前)


@dataclass(frozen=True)
class Job:
    id: str
    created: datetime              # Enter を押した瞬間(時刻と書き込み先はこれで決めてある)
    path: str
    line: str                      # 書式を展開した1行(改行なし)


@dataclass(frozen=True)
class UndoRecord:
    """直前に書いた1行の取り消しに使う(メモリだけ)。before は書く前の大きさ(新しいファイルはヘッダーの終わり)。"""

    job_id: str
    path: str
    before: int
    after: int
    tail: bytes                    # before から after までに足したバイト列
    written_at: float              # time.monotonic()


@dataclass
class WriteResult:
    ok: bool
    reason: str | None = None
    retries: int = 0
    ms: int = 0
    record: UndoRecord | None = None
    created_file: bool = False


class BusyError(Exception):
    """開けない・書けない(使用中)。再試行する。"""


class RefusedError(Exception):
    """書かない(理由コードつき)。再試行しない。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _winerror(e: OSError) -> int:
    return int(getattr(e, "winerror", 0) or 0)


def _open_error(e: OSError) -> Exception:
    code = _winerror(e)
    if code in (ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION):
        return BusyError()
    if code == ERROR_ACCESS_DENIED:
        return RefusedError(target.REASON_DENIED)
    if code == ERROR_PATH_NOT_FOUND:
        return RefusedError(target.REASON_FOLDER_MISSING)
    return RefusedError(target.REASON_OPEN_FAILED)


def _newline_of(tail: bytes, setting: str) -> bytes:
    """J-11: 設定が auto なら、ファイルの中で最後に現れる改行の形に合わせる(改行が無ければ LF)。"""
    if setting == "crlf":
        return b"\r\n"
    if setting == "lf":
        return b"\n"
    i = tail.rfind(b"\n")
    if i > 0 and tail[i - 1:i] == b"\r":
        return b"\r\n"
    return b"\n"


def _ends_blank(data: bytes) -> bool:
    """末尾が空行か(改行の直前がまた改行)。"""
    d = data.replace(b"\r\n", b"\n")
    return d.endswith(b"\n\n") or d == b"\n"


def build_bytes(existing_tail: bytes, existing_empty: bool, created: bool, line: str, framing: Framing,
                when: datetime) -> tuple[bytes, int]:
    """J-12・FR-10 の順で足すバイト列を組む。(足すバイト列, そのうちヘッダーの長さ) を返す。"""
    nl = _newline_of(existing_tail, framing.newline)
    head = b""
    if created and framing.header:
        head = compose.expand(framing.header, when, allowed=("date",)).encode("utf-8") + nl
    body = b""
    virtual = existing_tail + head          # 末尾の形を見るための「書いたあと」の末尾
    empty = existing_empty and not head
    if not empty and not virtual.endswith(b"\n"):
        body += nl                          # (1) 末尾に改行が無ければ改行を1つ
    if framing.blank_line_before and not empty and not _ends_blank(virtual + body):
        body += nl                          # (2) 空行
    if framing.separator and not created and not existing_empty:
        if not _ends_blank(virtual + body):
            body += nl                      # 区切りの前は必ず空行(setext 見出しにしない)
        body += framing.separator.encode("utf-8") + nl + nl   # (3) 区切りの行と空行
    body += line.encode("utf-8") + nl       # (4) 1行と改行
    return head + body, len(head)


def check_utf8(head: bytes, tail: bytes, tail_offset: int) -> bool:
    """J-10: 先頭が UTF-16 の BOM でなく、末尾 64KB が UTF-8 として読める。"""
    if head.startswith(UTF16_BOMS):
        return False
    t = tail
    if tail_offset > 0:  # 途中から読んだので、文字の途中のバイト(0x80〜0xBF)を最大3つ飛ばす
        k = 0
        while k < 3 and k < len(t) and 0x80 <= t[k] <= 0xBF:
            k += 1
        t = t[k:]
    try:
        t.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _open_for_append(api: Win32Api, path: str, create: bool) -> tuple[int, bool]:
    """J-8: まず OPEN_EXISTING、無ければ CREATE_NEW、ERROR_FILE_EXISTS なら OPEN_EXISTING に戻る。(handle, 作ったか)。"""
    try:
        return api.create_file(path, APPEND_ACCESS, FILE_SHARE_READ, OPEN_EXISTING), False
    except OSError as e:
        if _winerror(e) != ERROR_FILE_NOT_FOUND:
            raise _open_error(e) from None
    if not create:
        raise RefusedError(target.REASON_NO_FILE)
    try:
        return api.create_file(path, APPEND_ACCESS, FILE_SHARE_READ, CREATE_NEW), True
    except OSError as e:
        if _winerror(e) != ERROR_FILE_EXISTS:
            raise _open_error(e) from None
    try:
        return api.create_file(path, APPEND_ACCESS, FILE_SHARE_READ, OPEN_EXISTING), False
    except OSError as e:
        raise _open_error(e) from None


def append_once(api: Win32Api, job: Job, framing: Framing, now: Callable[[], float] = time.monotonic) -> WriteResult:
    """1回だけ試す。使用中は Busy、書かない理由は Refused。"""
    reason = target.check(job.path, api)
    if reason is not None:
        raise RefusedError(reason)
    h, created = _open_for_append(api, job.path, framing.create_file)
    try:
        size = api.size(h)
        head = api.read_at(h, 0, min(size, 4)) if size else b""
        off = max(0, size - TAIL_BYTES)
        tail = api.read_at(h, off, size - off) if size else b""
        if size and not check_utf8(head, tail, off):
            raise RefusedError(target.REASON_NOT_UTF8)
        data, head_len = build_bytes(tail, size == 0, created, job.line, framing, job.created)
        try:
            api.append(h, data)
        except OSError:
            raise BusyError() from None  # WriteFile の失敗(J-9)
        after = api.size(h)
    finally:
        api.close(h)
    before = size + head_len
    rec = UndoRecord(job.id, job.path, before, after, data[head_len:], now())
    return WriteResult(True, None, 0, 0, rec, created)


def _plain_sleep(s: float) -> bool:
    time.sleep(s)
    return False


def append_line(api: Win32Api, job: Job, framing: Framing, *, sleep: Callable[[float], bool] | None = None,
                now: Callable[[], float] = time.monotonic) -> WriteResult:
    """J-9: 使用中なら 200ms おきに 5 回まで試す。sleep(秒) が True を返したら中止(預ける)。"""
    t0 = now()
    wait = sleep or _plain_sleep
    tries = 0
    while True:
        tries += 1
        try:
            res = append_once(api, job, framing, now)
            res.retries = tries - 1
            res.ms = int((now() - t0) * 1000)
            return res
        except RefusedError as r:
            return WriteResult(False, r.reason, tries - 1, int((now() - t0) * 1000))
        except BusyError:
            if tries >= RETRIES or wait(RETRY_WAIT_S):
                return WriteResult(False, target.REASON_BUSY, tries - 1, int((now() - t0) * 1000))


# ------------------------------------------------------------------ 取り消し(J-18)
UNDO_OK = "ok"
UNDO_BUSY = "busy"           # 排他で開けない(他のアプリが開いている)
UNDO_CHANGED = "changed"     # そのあとファイルが変わった
UNDO_FAILED = "failed"


def undo(api: Win32Api, rec: UndoRecord) -> str:
    """共有 0 で開き、大きさが書いた直後と同じで、末尾が足したバイト列と一致するときだけ、書く前の大きさに切り詰める。"""
    try:
        h = api.create_file(rec.path, TRUNCATE_ACCESS, FILE_SHARE_NONE, OPEN_EXISTING)
    except OSError as e:
        return UNDO_BUSY if _winerror(e) in (ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION) else UNDO_CHANGED
    try:
        size = api.size(h)
        if size != rec.after or rec.before > rec.after or rec.after - rec.before != len(rec.tail):
            return UNDO_CHANGED
        if api.read_at(h, rec.before, len(rec.tail)) != rec.tail:
            return UNDO_CHANGED
        api.truncate(h, rec.before)  # INV-2: 書く前の大きさより短くしない
        return UNDO_OK
    except OSError:
        return UNDO_FAILED
    finally:
        api.close(h)


# ------------------------------------------------------------------ 照合(J-19)
VERIFY_FOUND = "found"
VERIFY_MISSING = "missing"
VERIFY_SKIPPED = "skipped"   # 開けない・5MB を超える


def verify(api: Win32Api, path: str, line: str) -> str:
    """ファイルの中に足した1行がまだあるか(行の中身で探すので、位置が動いても見つかる)。"""
    try:
        h = api.create_file(path, READ_ACCESS, READ_SHARE, OPEN_EXISTING)
    except OSError as e:
        return VERIFY_MISSING if _winerror(e) == ERROR_FILE_NOT_FOUND else VERIFY_SKIPPED
    try:
        size = api.size(h)
        if size > VERIFY_MAX_BYTES:
            return VERIFY_SKIPPED
        data = api.read_at(h, 0, size)
    except OSError:
        return VERIFY_SKIPPED
    finally:
        api.close(h)
    return VERIFY_FOUND if line.encode("utf-8") in data else VERIFY_MISSING


# ------------------------------------------------------------------ ワーカー
@dataclass
class Task:
    kind: str                          # "write" | "undo" | "verify" | "call"
    job: Job | None = None
    record: UndoRecord | None = None
    fn: Callable[[], None] | None = None
    done: Callable[[object], None] | None = None
    extra: dict[str, object] = field(default_factory=dict)


class Worker:
    """渡された順に1件ずつ行う(FR-8)。結果は done(結果) で返す(呼ぶ側がメインスレッドへ渡す)。"""

    def __init__(self, api: Win32Api, framing: Callable[[], Framing], log: logging.Logger, *, threaded: bool = True,
                 sleep: Callable[[float], bool] | None = None) -> None:
        self.api = api
        self._framing = framing
        self.log = log
        self._threaded = threaded
        self._sleep_fn = sleep
        self._q: queue.Queue[Task | None] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._busy = threading.Event()

    def start(self) -> None:
        self._stop.clear()
        if self._threaded and (self._thread is None or not self._thread.is_alive()):
            self._thread = threading.Thread(target=self._loop, name="jotdrop-writer", daemon=True)
            self._thread.start()

    def submit(self, task: Task) -> None:
        if self._threaded:
            self._q.put(task)
        else:
            self._run(task)

    def stop(self, wait_s: float = 2.0) -> list[Task]:
        """2 秒まで待ち(§10)、まだ始めていない仕事を返す(呼ぶ側が預ける)。"""
        self._stop.set()
        left: list[Task] = []
        while True:
            try:
                t = self._q.get_nowait()
            except queue.Empty:
                break
            if t is not None:
                left.append(t)
        self._q.put(None)
        th = self._thread
        if th is not None and th.is_alive():
            th.join(wait_s)
        return left

    def sleep(self, s: float) -> bool:
        """再試行の待ち。止める途中なら True(すぐ抜けて預ける)。"""
        if self._sleep_fn is not None:
            return self._sleep_fn(s) or self._stop.is_set()
        return self._stop.wait(s)

    def _loop(self) -> None:
        while True:
            t = self._q.get()
            if t is None:
                return
            if self._stop.is_set() and t.kind == "write":
                if t.done is not None:  # 終了の途中: 書かずに預けてもらう
                    t.done(WriteResult(False, target.REASON_BUSY))
                continue
            self._run(t)

    def _run(self, t: Task) -> None:
        self._busy.set()
        try:
            res: object
            if t.kind == "write" and t.job is not None:
                res = append_line(self.api, t.job, self._framing(), sleep=self.sleep)
            elif t.kind == "undo" and t.record is not None:
                res = undo(self.api, t.record)
            elif t.kind == "verify" and t.job is not None:
                res = verify(self.api, t.job.path, t.job.line)
            elif t.kind == "call" and t.fn is not None:
                t.fn()
                res = None
            else:
                res = None
        except Exception as e:  # noqa: BLE001 - 1件の失敗でワーカーを止めない。ログは型名だけ(INV-3)
            self.log.warning("worker task %s failed: %s", t.kind, type(e).__name__)
            res = WriteResult(False, target.REASON_OPEN_FAILED) if t.kind == "write" else None
        finally:
            self._busy.clear()
        if t.done is not None:
            t.done(res)
