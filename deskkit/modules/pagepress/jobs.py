# ジョブのスレッド(同時に1本。FR-13)。入力の確認・組み立て・書き出し・検査・後始末を1件ずつ順に行う。
# 出力は同じフォルダの一時ファイルに書き、FR-19 の検査を通ってから正式な名前に変える(P-12)。中止は書くたびに見る(FR-15)。
# ログ・ops.jsonl には件数・バイト数・理由コードだけを書き、ファイル名・パス・例外の文は書かない(INV-5)。
from __future__ import annotations

import errno
import logging
import os
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deskkit.modules.pagepress import build, compress, naming, sanitize
from deskkit.modules.pagepress.build import (
    PHASE_READ,
    PHASE_VERIFY,
    PHASE_WRITE,
    CancelledError,
    Control,
    ImageOpts,
    InputFailedError,
)
from deskkit.modules.pagepress.oplog import OpsLog
from deskkit.modules.pagepress.ranges import MSG_OVER_ALL, RangeError, Span
from deskkit.modules.pagepress.reader import Entry, ProbeError

MAX_ITEMS = 200
MAX_PAGES = 5000
MAX_TOTAL_BYTES = 1_000_000_000
SPACE_MARGIN = 50 * 1024 * 1024

MSG_CANCELLED = "中止しました"
MSG_CANCELLING = "中止しています"
MSG_VERIFY = "確認できなかったため作れませんでした"
MSG_DENIED = "保存できませんでした"
MSG_NAMES = "同じ名前のファイルが多すぎます"
MSG_TOO_MANY_PAGES = "1つの PDF にできるのは 5,000 ページまでです"
MSG_FAILED = "作れませんでした"
MSG_SAME = "元のファイルと同じ場所には書けません"
MSG_CHANGED = "ファイルが変わったため作れませんでした"
MSG_NOT_SMALLER = "これ以上軽くできませんでした。元のファイルをそのままお使いください"
MSG_BUSY = "ほかの作業が終わるまでお待ちください"
MSG_OVER_COUNT = "一度にまとめられるのは 200 件までです"
MSG_OVER_BYTES = "合計 1GB までです"

STATE_RUNNING = "running"
STATE_CANCELLING = "cancelling"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"


def msg_disk(need_mb: int) -> str:
    return f"空き容量が足りません(あと {need_mb} MB 要ります)"


def fmt_size(n: int | None) -> str:
    if n is None:
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}MB"
    return f"{max(1, round(n / 1000))}KB"


class JobError(Exception):
    def __init__(self, result: str, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.result = result      # ops の result
        self.message = message    # 画面の文
        self.code = code          # ops の code


@dataclass
class Row:
    """結果の1行(画面だけ)。entry_id は入力の行(分ける・軽くする)。"""

    entry_id: int | None
    ok: bool
    text: str
    path: Path | None = None       # 出力のファイル(分けるはフォルダ)
    folder: Path | None = None
    pages: int = 0
    size: int = 0
    fallback: bool = False
    warn: bool = False


@dataclass
class JobSpec:
    op: str                                     # merge / split / organize / compress
    entries: list[Entry]
    opts: ImageOpts = field(default_factory=ImageOpts)
    split_mode: str = "ranges"
    spans: list[Span] = field(default_factory=list)
    every: int = 10
    items: list[tuple[int, int]] = field(default_factory=list)
    level: str = "normal"


@dataclass
class JobStatus:
    op: str
    state: str = STATE_RUNNING
    phase: str = PHASE_READ
    done: int = 0
    total: int = 0
    rows: list[Row] = field(default_factory=list)
    message: str = ""
    failed_index: int | None = None
    cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def finished(self) -> bool:
        return self.state in (STATE_DONE, STATE_FAILED, STATE_CANCELLED)

    def progress_text(self) -> str:
        if self.state == STATE_CANCELLING:
            return MSG_CANCELLING
        if self.phase == build.PHASE_BUILD and self.total:
            unit = "ページ" if self.op in ("merge", "organize") else "件"
            return f"{self.phase} {self.done} / {self.total} {unit}"
        return self.phase


def _default_free(p: Path) -> int:
    return shutil.disk_usage(p).free


@dataclass
class Env:
    ops: OpsLog
    pending: naming.Pending
    log: logging.Logger
    fallback_dir: Callable[[], Path] = naming.default_fallback
    disk_free: Callable[[Path], int] = _default_free
    clean: Callable[[Any], None] = sanitize.clean              # テストで差し替えて FR-19 を落とす(AC-3)
    verify: Callable[[Any, int], str | None] = sanitize.verify
    on_write: Callable[[], None] = lambda: None                 # テスト: 書いている途中を真似る
    on_update: Callable[[JobStatus], None] = lambda _s: None


def _is_disk_full(e: OSError) -> bool:
    return e.errno == errno.ENOSPC or getattr(e, "winerror", None) in (39, 112)


def _is_denied(e: OSError) -> bool:
    return isinstance(e, PermissionError) or e.errno in (errno.EACCES, errno.EPERM, errno.EROFS) \
        or getattr(e, "winerror", None) in (5, 19, 32)


class CancelFile:
    """pypdf に渡すファイルの包み。書くたびに中止の印を見て、立っていれば CancelledError で抜ける(FR-15)。"""

    def __init__(self, f: Any, cancel: threading.Event, hook: Callable[[], None]) -> None:
        self._f = f
        self._cancel = cancel
        self._hook = hook
        self.mode = "wb"

    def write(self, b: Any) -> int:
        if self._cancel.is_set():
            raise CancelledError
        self._hook()
        return int(self._f.write(b))

    def tell(self) -> int:
        return int(self._f.tell())

    def flush(self) -> None:
        self._f.flush()


@dataclass(frozen=True)
class Written:
    path: Path
    size: int
    fallback: bool


def _make_tmp(folder: Path) -> Path:
    for _ in range(20):
        p = folder / naming.tmp_name()
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o666)
            os.close(fd)
            return p
        except FileExistsError:
            continue
    raise JobError("error", MSG_NAMES, "names_exhausted")


def _pick_folder(folder: Path, stem: str, tail: str, env: Env) -> tuple[Path, str, Path, bool]:
    """(フォルダ, 収めた名前, 一時ファイル, 保存先を替えたか)。書けない・長すぎるならドキュメント\\PagePress\\。"""
    fitted = naming.fit_stem(folder, stem, tail)
    if fitted is not None:
        try:
            return folder, fitted, _make_tmp(folder), False
        except OSError as e:
            if _is_disk_full(e):
                raise JobError("error", msg_disk(1), "disk_full") from None
            if not _is_denied(e) and not isinstance(e, FileNotFoundError):
                raise JobError("error", MSG_DENIED, "denied") from None
    fb = env.fallback_dir()
    fitted = naming.fit_stem(fb, stem, tail)
    if fitted is None:
        raise JobError("error", MSG_DENIED, "denied")
    try:
        fb.mkdir(parents=True, exist_ok=True)
        return fb, fitted, _make_tmp(fb), True
    except OSError as e:
        if _is_disk_full(e):
            raise JobError("error", msg_disk(1), "disk_full") from None
        raise JobError("error", MSG_DENIED, "denied") from None


def write_output(writer: Any, folder: Path, stem: str, tail: str, pages: int, inputs: Sequence[Path], need: int,
                 st: JobStatus, env: Env) -> Written:
    """掃除 → 一時ファイルへ書く → 開き直して検査 → 正式な名前へ。失敗・中止では一時ファイルを消す。"""
    out_dir, name, tmp, fb = _pick_folder(folder, stem, tail, env)
    env.pending.add(tmp)
    try:
        for inp in inputs:
            if naming.same_file(tmp, inp):
                raise JobError("error", MSG_SAME, "denied")
        try:
            free = env.disk_free(out_dir)
        except OSError:
            free = None
        if free is not None and free < need + SPACE_MARGIN:
            lack = need + SPACE_MARGIN - free
            raise JobError("error", msg_disk(max(1, -(-lack // 1_000_000))), "disk_full")
        env.clean(writer)
        _progress(st, env, PHASE_WRITE)
        try:
            with open(tmp, "wb") as f:
                writer.write(CancelFile(f, st.cancel, env.on_write))
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            if _is_disk_full(e):
                try:
                    free = env.disk_free(out_dir)
                except OSError:
                    free = 0
                lack = need + SPACE_MARGIN - free
                raise JobError("error", msg_disk(max(1, -(-lack // 1_000_000))), "disk_full") from None
            if _is_denied(e):
                raise JobError("error", MSG_DENIED, "denied") from None
            raise
        if st.cancel.is_set():
            raise CancelledError
        _progress(st, env, PHASE_VERIFY)
        with open(tmp, "rb") as f:
            bad = env.verify(f, pages)
        if bad is not None:
            env.log.warning("verify failed reason=%s", bad)
            raise JobError("verify_failed", MSG_VERIFY)
        if st.cancel.is_set():
            raise CancelledError
        size = tmp.stat().st_size
        try:
            final = naming.rename_unique(tmp, out_dir, name, tail)
        except naming.NamesExhaustedError:
            raise JobError("error", MSG_NAMES, "names_exhausted") from None
        except OSError:
            raise JobError("error", MSG_DENIED, "denied") from None
    except BaseException:
        naming.remove_own(tmp)
        env.pending.discard(tmp)
        raise
    env.pending.discard(tmp)
    return Written(final, size, fb)


def _progress(st: JobStatus, env: Env, phase: str, done: int = 0, total: int = 0) -> None:
    st.phase, st.done, st.total = phase, done, total
    env.on_update(st)


def _control(st: JobStatus, env: Env) -> Control:
    last = [0.0]

    def on(phase: str, done: int, total: int) -> None:
        st.phase, st.done, st.total = phase, done, total
        now = time.monotonic()
        if now - last[0] >= 0.1 or done >= total:
            last[0] = now
            env.on_update(st)

    return Control(st.cancel, on)


# ------------------------------------------------------------------ 各操作
def run_merge(spec: JobSpec, st: JobStatus, env: Env) -> tuple[int, int, int]:
    """(ページ数, 出力の数, 出力の大きさ)。"""
    entries = spec.entries
    total = sum(e.pages for e in entries)
    if total > MAX_PAGES:
        raise JobError("error", MSG_TOO_MANY_PAGES, "too_many_pages")
    need = sum(e.size for e in entries)
    ctl = _control(st, env)
    with ExitStack() as stack:
        try:
            w = build.merge(entries, spec.opts, ctl, stack)
        except InputFailedError as e:
            st.failed_index = e.index
            raise JobError("error", e.message, e.code) from None
        n = len(w.pages)
        if n > MAX_PAGES:
            raise JobError("error", MSG_TOO_MANY_PAGES, "too_many_pages")
        first = entries[0].path
        res = write_output(w, first.parent, f"{first.stem}_まとめ", ".pdf", n, [e.path for e in entries], need, st, env)
    st.rows.append(Row(None, True, f"{n} ページ・{fmt_size(res.size)}", res.path, res.path.parent, n, res.size, res.fallback))
    return n, 1, res.size


def run_organize(spec: JobSpec, st: JobStatus, env: Env) -> tuple[int, int, int]:
    e = spec.entries[0]
    ctl = _control(st, env)
    with ExitStack() as stack:
        try:
            r = build.open_reader(e.path, stack)
        except ProbeError as ex:
            st.failed_index = 0
            raise JobError("error", ex.message, "encrypted" if ex.code == "encrypted" else "unreadable") from None
        if len(r.pages) != e.pages:
            raise JobError("error", MSG_CHANGED, "unreadable")
        try:
            w = build.organize(r, spec.items, ctl)
        except (CancelledError, MemoryError):
            raise
        except Exception:  # noqa: BLE001
            raise JobError("error", MSG_FAILED, "unreadable") from None
        n = len(w.pages)
        res = write_output(w, e.path.parent, f"{e.path.stem}_整理", ".pdf", n, [e.path], e.size, st, env)
    st.rows.append(Row(None, True, f"{n} ページ・{fmt_size(res.size)}", res.path, res.path.parent, n, res.size, res.fallback))
    return n, 1, res.size


def _spans_for(spec: JobSpec, pages: int) -> list[tuple[int, int]]:
    from deskkit.modules.pagepress import ranges

    if spec.split_mode == "single":
        return ranges.single(pages)
    if spec.split_mode == "every":
        return ranges.every(pages, spec.every)
    return [s.resolve(pages) for s in spec.spans]


def _split_one(e: Entry, spec: JobSpec, st: JobStatus, env: Env) -> Row:
    made: list[Path] = []
    folder: Path | None = None
    try:
        with ExitStack() as stack:
            r = build.open_reader(e.path, stack)
            n = len(r.pages)
            try:
                spans = _spans_for(spec, n)
            except RangeError:
                raise JobError("error", MSG_OVER_ALL, "range_out") from None
            dir_name = f"{e.path.stem}_分割"
            parent = e.path.parent
            fitted = naming.fit_stem(parent, dir_name, "\\" + "x" * 40)  # 中のファイル名に 40 文字は残す
            fb = False
            try:
                if fitted is None:
                    raise PermissionError
                folder = naming.make_unique_dir(parent, fitted)
            except naming.NamesExhaustedError:
                raise JobError("error", MSG_NAMES, "names_exhausted") from None
            except OSError:
                base = env.fallback_dir()
                try:
                    base.mkdir(parents=True, exist_ok=True)
                    folder = naming.make_unique_dir(base, dir_name)
                    fb = True
                except naming.NamesExhaustedError:
                    raise JobError("error", MSG_NAMES, "names_exhausted") from None
                except OSError:
                    raise JobError("error", MSG_DENIED, "denied") from None
            ctl = _control(st, env)
            total_size = 0
            for a, b, w in build.split_spans(r, spans, ctl):
                stem = f"{e.path.stem}_{a}" if a == b else f"{e.path.stem}_{a}-{b}"
                res = write_output(w, folder, stem, ".pdf", b - a + 1, [e.path], e.size, st, env)
                made.append(res.path)
                total_size += res.size
                _progress(st, env, build.PHASE_BUILD, len(made), len(spans))
    except BaseException:
        for p in made:  # 中止・失敗した入力の途中までの出力は自分で消す(INV-8)
            naming.remove_own(p)
        naming.remove_own(folder)
        raise
    assert folder is not None
    return Row(e.id, True, f"{len(made)} 個のファイルを作りました", folder, folder, len(made), total_size, fb)


def run_split(spec: JobSpec, st: JobStatus, env: Env) -> tuple[int, int, int, str | None]:
    """(出力のページの合計, 出力の数, 出力の大きさ, 最後の失敗の理由コード)。1件の失敗で残りを止めない(FR-22)。"""
    outputs = size = pages = 0
    code: str | None = None
    for e in spec.entries:
        if st.cancel.is_set():
            raise CancelledError
        try:
            row = _split_one(e, spec, st, env)
        except ProbeError as ex:
            code = "encrypted" if ex.code == "encrypted" else "unreadable"
            st.rows.append(Row(e.id, False, ex.message))
            continue
        except JobError as ex:
            code = ex.code
            st.rows.append(Row(e.id, False, ex.message))
            continue
        except (CancelledError, MemoryError):
            raise
        except Exception as ex:  # noqa: BLE001
            env.log.error("split failed: %s", type(ex).__name__)
            code = "unreadable"
            st.rows.append(Row(e.id, False, MSG_FAILED))
            continue
        st.rows.append(row)
        outputs += row.pages
        size += row.size
        pages += e.pages
    return pages, outputs, size, code


def _compress_one(e: Entry, spec: JobSpec, st: JobStatus, env: Env) -> tuple[Row, bool]:
    """(行, 出力が残ったか)。"""
    ctl = _control(st, env)
    with ExitStack() as stack:
        r = build.open_reader(e.path, stack)
        w = build.new_writer()
        n = len(r.pages)
        w.append(r)
        sanitize.resolve_named(w, r, 0, list(range(n)))
        compress.shrink_images(w, spec.level, ctl)
        compress.finish(w)
        res = write_output(w, e.path.parent, f"{e.path.stem}_軽量", ".pdf", n, [e.path], e.size, st, env)
    if res.size >= e.size:
        naming.remove_own(res.path)  # 大きくなった物を「軽くした物」として渡さない(P-11)
        return Row(e.id, False, MSG_NOT_SMALLER, warn=True), False
    pct = int((1 - res.size / e.size) * 100) if e.size else 0
    if res.size >= e.size * compress.REPLACE_RATIO:
        return Row(e.id, True, f"あまり軽くなりませんでした({pct}% 減)", res.path, res.path.parent, n, res.size,
                   res.fallback, warn=True), True
    return Row(e.id, True, f"{fmt_size(e.size)} → {fmt_size(res.size)}({pct}% 減)", res.path, res.path.parent, n,
               res.size, res.fallback), True


def run_compress(spec: JobSpec, st: JobStatus, env: Env) -> tuple[int, int, int, str | None, bool]:
    """(ページの合計, 出力の数, 出力の大きさ, 最後の失敗の理由コード, 全部が P-11 で残らなかったか)。"""
    outputs = size = pages = 0
    code: str | None = None
    not_smaller = 0
    for e in spec.entries:
        if st.cancel.is_set():
            raise CancelledError
        try:
            row, kept = _compress_one(e, spec, st, env)
        except ProbeError as ex:
            code = "encrypted" if ex.code == "encrypted" else "unreadable"
            st.rows.append(Row(e.id, False, ex.message))
            continue
        except JobError as ex:
            code = ex.code
            st.rows.append(Row(e.id, False, ex.message))
            continue
        except (CancelledError, MemoryError):
            raise
        except Exception as ex:  # noqa: BLE001
            env.log.error("compress failed: %s", type(ex).__name__)
            code = "unreadable"
            st.rows.append(Row(e.id, False, MSG_FAILED))
            continue
        st.rows.append(row)
        if kept:
            outputs += 1
            size += row.size
            pages += row.pages
        else:
            not_smaller += 1
    return pages, outputs, size, code, not_smaller > 0 and outputs == 0 and code is None


def run(spec: JobSpec, st: JobStatus, env: Env) -> None:
    """1つのジョブを最後まで行い、状態と操作記録を残す。"""
    t0 = time.monotonic()
    images = sum(1 for e in spec.entries if e.kind == "image")
    in_bytes = sum(e.size for e in spec.entries)
    pages = outputs = 0
    out_bytes: int | None = None
    result, code = "ok", None
    _progress(st, env, PHASE_READ)
    try:
        if spec.op == "merge":
            pages, outputs, size = run_merge(spec, st, env)
            out_bytes = size
        elif spec.op == "organize":
            pages, outputs, size = run_organize(spec, st, env)
            out_bytes = size
        elif spec.op == "split":
            pages, outputs, size, code = run_split(spec, st, env)
            out_bytes = size
            if outputs == 0:
                result = "error"
        else:
            pages, outputs, size, code, all_not_smaller = run_compress(spec, st, env)
            out_bytes = size if outputs else None
            if all_not_smaller:
                result = "not_smaller"
            elif outputs == 0:
                result = "error"
        st.state = STATE_DONE if result == "ok" else STATE_FAILED
        if spec.op in ("split", "compress") and result != "ok" and not st.rows:
            st.message = MSG_FAILED
    except CancelledError:
        result, code, out_bytes = "cancelled", "cancelled", None
        st.state, st.message = STATE_CANCELLED, MSG_CANCELLED
    except JobError as e:
        result, code, out_bytes = e.result, e.code, None
        st.state, st.message = STATE_FAILED, e.message
    except MemoryError:
        result, code, out_bytes = "error", "too_large", None
        st.state, st.message = STATE_FAILED, MSG_FAILED
    except Exception as e:  # noqa: BLE001 - 想定外でもジョブのスレッドは止めない
        env.log.error("job failed op=%s: %s", spec.op, type(e).__name__)
        result, code, out_bytes = "error", None, None
        st.state, st.message = STATE_FAILED, MSG_FAILED
    ms = int((time.monotonic() - t0) * 1000)
    env.ops.write(op=spec.op, result=result, inputs=len(spec.entries), images=images, pages=pages, outputs=outputs,
                  in_bytes=in_bytes, out_bytes=out_bytes, level=spec.level if spec.op == "compress" else None, ms=ms,
                  code=code)
    env.log.info("job op=%s result=%s code=%s inputs=%d pages=%d outputs=%d in=%d out=%s ms=%d", spec.op, result,
                 code or "-", len(spec.entries), pages, outputs, in_bytes, out_bytes if out_bytes is not None else "-", ms)
    env.on_update(st)


# ------------------------------------------------------------------ ジョブのスレッド
class Worker:
    """確認とジョブを1件ずつ順に行うスレッド(同時に1本)。しばらく何も無ければ終わり、次に積まれたら作り直す。"""

    def __init__(self, log: logging.Logger) -> None:
        self._log = log
        self._q: deque[Callable[[], None]] = deque()
        self._cv = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._running = 0

    def submit(self, fn: Callable[[], None]) -> bool:
        with self._cv:
            if self._stopping:
                return False
            self._q.append(fn)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="pagepress-job", daemon=True)
                self._thread.start()
            self._cv.notify_all()
            return True

    def idle(self) -> bool:
        with self._cv:
            return not self._q and self._running == 0

    def _loop(self) -> None:
        while True:
            with self._cv:
                if not self._q and not self._stopping:
                    self._cv.wait(30.0)
                if self._stopping or not self._q:
                    self._thread = None
                    return
                fn = self._q.popleft()
                self._running += 1
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - ここまで漏れてもスレッドは止めない
                self._log.error("worker task failed: %s", type(e).__name__)
            finally:
                with self._cv:
                    self._running -= 1
                    self._cv.notify_all()

    def stop(self, timeout: float = 5.0) -> bool:
        """待ちを捨てて止まる。動いている物の中止は呼ぶ側が先に印を立てる。止まれば True。"""
        with self._cv:
            self._stopping = True
            self._q.clear()
            th = self._thread
            self._cv.notify_all()
        if th is not None and th is not threading.current_thread():
            th.join(timeout)
            return not th.is_alive()
        return True

    def wait_idle(self, timeout: float) -> bool:
        end = time.monotonic() + timeout
        with self._cv:
            while self._q or self._running:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(left)
            return True
