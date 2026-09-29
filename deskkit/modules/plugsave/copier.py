# 1回のバックアップの実行(B-7・B-8・B-9・FR-16〜FR-22)。前の回の残りの片づけ → 計画 → 空きの見積もり → 1ファイルずつコピー。
# 1ファイルは `_作業中\<回の ID>\<連番>.part` へ 1MiB ずつ書く → fsync(1MiB 以上だけ) → 大きさの照合 → os.utime → os.rename で最終の名前へ。
# 変わったファイルは照合の後に先の古い方を `_以前の版\<日時>\` へ名前の変更で移す。上書き・削除の経路は無い(消すのは staging の .part だけ)。
from __future__ import annotations

import datetime as _dt
import errno
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import IO, Protocol

from deskkit.modules.plugsave import drives, staging
from deskkit.modules.plugsave._win32 import DriveApi
from deskkit.modules.plugsave.planner import (
    FAILED_REASONS,
    MAX_PLAN_FILES,
    MAX_ROWS,
    R_CHANGED_DURING_COPY,
    R_IN_USE,
    R_OLD_MOVE_FAILED,
    R_UNREADABLE,
    R_WRITE_FAILED,
    Plan,
    PlanItem,
    Source,
    fit_to_space,
    make_plan,
)

CHUNK = 1024 * 1024               # B-8: 1MiB ずつ
# これより小さいファイルは fsync しない(利用者の判断 2026-09-29。小さいファイル 1 万件の初回が 171 秒 → 約 35 秒)。
# 「あとでまとめて fsync」は開き直しのぶん 1 件ずつより遅く、ボリューム全体の確定は管理者が要るため、大きさで分けた。
FSYNC_MIN_BYTES = CHUNK
RESERVE_BYTES = 200_000_000       # B-9: 予備の空き
PROGRESS_INTERVAL_S = 0.2         # FR-19: 画面の更新は 200ms に1回まで
SHARING_ERRORS = (32, 33)         # ERROR_SHARING_VIOLATION / ERROR_LOCK_VIOLATION
DISK_FULL_ERRORS = (39, 112)      # ERROR_HANDLE_DISK_FULL / ERROR_DISK_FULL

# 結果コード(§9)
OK = "ok"
CANCELLED = "cancelled"
REMOVED = "removed"
NO_SPACE = "no_space"
ERROR = "error"
# ERROR のときの細かい理由(画面用。ログ・ops には書かない)
E_READONLY = "readonly"
E_NO_SOURCES = "no_sources"
E_UNEXPECTED = "unexpected"

STAGE_CLEAN = "clean"
STAGE_PLAN = "plan"
STAGE_COPY = "copy"

Progress = Callable[[str, int, int, int, int], None]   # (段階, 済んだ件数, 全件数, 済んだバイト, 全バイト)


class CopyIO(Protocol):
    def open_src(self, path: str) -> IO[bytes]: ...
    def open_part(self, path: str) -> IO[bytes]: ...


class RealIO:
    def open_src(self, path: str) -> IO[bytes]:
        return open(path, "rb")  # INV-1: コピー元は読み取りだけで開く

    def open_part(self, path: str) -> IO[bytes]:
        return open(path, "xb")  # 同じ名前があれば失敗する(上書きしない)


@dataclass
class BackupRequest:
    root: str                     # 先のドライブの根(本物は "E:\\")
    fs: str                       # 先のファイルシステム名
    pc: str                       # <PC の名前>
    sources: list[Source]
    excluded: list[str] = field(default_factory=list)
    fit: bool = False             # 「入る分だけコピー」
    plan_only: bool = False       # 初回の計画の表示(コピーしない・片づけない)
    max_files: int = MAX_PLAN_FILES
    reserve: int = RESERVE_BYTES
    start: _dt.datetime | None = None


@dataclass
class BackupOutcome:
    result: str = OK
    error: str | None = None       # ERROR のときの細かい理由(画面用)
    new: int = 0
    changed: int = 0
    moved_old: int = 0
    unchanged: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    rows: list[tuple[str, str]] = field(default_factory=list)
    utime_failed: list[str] = field(default_factory=list)
    utime_failed_count: int = 0
    bytes: int = 0
    left_out: int = 0              # 入る分だけコピーで今回コピーしなかった数
    need_more: int = 0             # no_space のとき、あと何バイト要るか
    free: int = 0
    total: int = 0
    truncated: bool = False
    missing_sources: int = 0
    total_sources: int = 0
    cleaned_parts: int = 0
    plan_new: int = 0
    plan_changed: int = 0
    plan_bytes: int = 0
    ms: int = 0
    exc_type: str = ""             # 想定外の例外の型名(ログ用。例外の文は持たない)

    @property
    def copied(self) -> int:
        return self.new + self.changed

    @property
    def failed(self) -> int:
        return sum(v for k, v in self.reasons.items() if k in FAILED_REASONS)

    @property
    def skipped(self) -> dict[str, int]:
        return {k: v for k, v in self.reasons.items() if k not in FAILED_REASONS and v}

    @property
    def skipped_total(self) -> int:
        return sum(self.skipped.values())


class _Stop(Exception):  # noqa: N818 - 中で止めるための合図
    def __init__(self, result: str) -> None:
        super().__init__(result)
        self.result = result


def _winerror(e: OSError) -> int:
    return int(getattr(e, "winerror", 0) or 0)


class BackupRun:
    """1回分。run() は別スレッドで呼ぶ(待ちと I/O がある)。"""

    def __init__(self, req: BackupRequest, api: DriveApi, *, cancel: threading.Event | None = None,
                 removed: threading.Event | None = None, progress: Progress | None = None, io: CopyIO | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.req = req
        self.api = api
        self.cancel = cancel or threading.Event()
        self.removed = removed or threading.Event()
        self.progress = progress
        self.io: CopyIO = io or RealIO()
        self.clock = clock
        self.pc_dir = drives.pc_dir(req.root, req.pc)
        self.start = req.start or _dt.datetime.now()
        self.out = BackupOutcome(total_sources=len(req.sources))
        self.plan: Plan | None = None
        self._old_dir: str | None = None
        self._last_progress = 0.0
        self._done_bytes = 0
        self._total_bytes = 0
        self._done_files = 0
        self._total_files = 0
        self._staging: staging.RunStaging | None = None

    # ------------------------------------------------------------ 全体
    def run(self) -> BackupOutcome:
        t0 = time.perf_counter()
        try:
            self._run()
        except _Stop as s:
            self.out.result = s.result
        except Exception as e:  # noqa: BLE001 - 想定外も結果として返す(例外の文は持ち出さない。INV-4)
            self.out.result = ERROR
            self.out.error = E_UNEXPECTED
            self.out.exc_type = type(e).__name__
        finally:
            if self._staging is not None:
                self._staging.finish()
        self.out.ms = int((time.perf_counter() - t0) * 1000)
        return self.out

    def _root_gone(self) -> bool:
        return self.removed.is_set() or not os.path.isdir(self.req.root)

    def _check_stop(self) -> None:
        if self.removed.is_set():
            raise _Stop(REMOVED)
        if self.cancel.is_set():
            raise _Stop(CANCELLED)

    def _run(self) -> None:
        req = self.req
        if self._root_gone():
            raise _Stop(REMOVED)
        if not req.plan_only:
            self._emit(STAGE_CLEAN, 0, 0, force=True)
            self.out.cleaned_parts = staging.cleanup_leftovers(self.pc_dir)   # FR-22
            st = staging.RunStaging(self.pc_dir)
            try:
                st.create()
            except OSError as e:
                if self._root_gone():
                    raise _Stop(REMOVED) from e
                self.out.error = E_READONLY   # §10: 書き込み禁止のドライブ
                raise _Stop(ERROR) from e
            self._staging = st
        self._emit(STAGE_PLAN, 0, 0, force=True)
        plan = make_plan(req.sources, self.pc_dir, req.fs, excluded=req.excluded, cancel=self.cancel,
                         on_count=lambda n: self._emit(STAGE_PLAN, n, 0), max_files=req.max_files)
        self.plan = plan
        out = self.out
        out.unchanged = plan.unchanged
        out.reasons = dict(plan.reasons)
        out.rows = list(plan.rows)
        out.truncated = plan.truncated
        out.missing_sources = plan.missing_sources
        out.plan_new = plan.new_count
        out.plan_changed = plan.changed_count
        out.plan_bytes = plan.copy_bytes
        self._check_stop()
        if plan.cancelled:
            raise _Stop(CANCELLED)
        if plan.total_sources and plan.missing_sources >= plan.total_sources:
            out.error = E_NO_SOURCES      # §10: コピー元が全部見つからない
            raise _Stop(ERROR)
        try:
            out.total, out.free = self.api.disk_usage(req.root)
        except OSError as e:
            if self._root_gone():
                raise _Stop(REMOVED) from e
            raise
        items = plan.items
        need = plan.copy_bytes + req.reserve
        if req.plan_only:
            out.need_more = max(0, need - out.free) if plan.items else 0
            return
        if req.fit:
            items, out.left_out = fit_to_space(plan.items, max(0, out.free - req.reserve))
        elif plan.items and out.free < need:
            out.need_more = need - out.free
            raise _Stop(NO_SPACE)       # B-9: 始めない
        self._total_files = len(items)
        self._total_bytes = sum(i.size for i in items)
        self._emit(STAGE_COPY, 0, self._total_files, force=True)
        for it in items:
            self._check_stop()
            reason = self._copy_one(it)
            self._done_files += 1
            if reason is not None:
                self._record(it.src, reason)
            self._emit(STAGE_COPY, self._done_files, self._total_files)
        self._emit(STAGE_COPY, self._done_files, self._total_files, force=True)
        if req.fit and out.left_out:
            raise _Stop(NO_SPACE)       # 入らなかった分が残った(成功として数えない。docs/v0.4/plugsave.md)

    # ------------------------------------------------------------ 1ファイル
    def _record(self, path: str, reason: str) -> None:
        self.out.reasons[reason] = self.out.reasons.get(reason, 0) + 1
        if len(self.out.rows) < MAX_ROWS:
            self.out.rows.append((drives.display_path(path), reason))

    def _emit(self, stage: str, done: int, total: int, *, force: bool = False) -> None:
        if self.progress is None:
            return
        now = self.clock()
        if not force and now - self._last_progress < PROGRESS_INTERVAL_S:
            return
        self._last_progress = now
        self.progress(stage, done, total, self._done_bytes, self._total_bytes)

    def _old_run_dir(self) -> str:
        if self._old_dir is None:
            self._old_dir = drives.unique_dir(os.path.join(self.pc_dir, drives.OLD_DIR), self.start.strftime("%Y-%m-%d_%H%M%S"))
        return self._old_dir

    def _move_to_old(self, rel_from_pc: str) -> bool:
        """先の既存の物(ファイルかフォルダ)を `_以前の版\\<日時>\\` の同じ相対パスへ名前の変更で移す(B-7・FR-17)。"""
        src = os.path.join(self.pc_dir, rel_from_pc)
        dst = os.path.join(self._old_run_dir(), rel_from_pc)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(src, dst)   # 同じ名前があれば失敗する(上書きしない)
        except OSError as e:
            if self._root_gone():
                raise _Stop(REMOVED) from e
            return False
        self.out.moved_old += 1
        return True

    def _write_error(self, e: OSError, part: str | None) -> str:
        if part is not None:
            staging.discard_part(part)
        if self._root_gone():
            raise _Stop(REMOVED) from e
        if _winerror(e) in DISK_FULL_ERRORS or e.errno == errno.ENOSPC:
            raise _Stop(NO_SPACE) from e
        return R_WRITE_FAILED

    def _ensure_parent(self, item: PlanItem) -> str | None:
        """先の親フォルダを作る。途中に同じ名前のファイルがあれば `_以前の版` へ移す(FR-17)。失敗は理由コード。"""
        parts = [item.source_name, *[p for p in os.path.dirname(item.rel).split(os.sep) if p]]
        for i in range(len(parts)):
            rel = os.path.join(*parts[: i + 1])
            p = os.path.join(self.pc_dir, rel)
            if os.path.isdir(p):
                continue
            if os.path.lexists(p) and not self._move_to_old(rel):
                return R_OLD_MOVE_FAILED
            try:
                os.mkdir(p)
            except FileExistsError:
                if not os.path.isdir(p):
                    return R_WRITE_FAILED
            except OSError as e:
                return self._write_error(e, None)
        return None

    def _copy_one(self, item: PlanItem) -> str | None:
        """1ファイルをコピーする。成功は None、飛ばした・失敗は理由コード。止めるときは _Stop。"""
        assert self._staging is not None
        try:
            free = self.api.disk_usage(self.req.root)[1]
        except OSError as e:
            if self._root_gone():
                raise _Stop(REMOVED) from e
            free = item.size
        if free < item.size:
            raise _Stop(NO_SPACE)      # B-9: コピー中も1ファイルごとに確かめる
        bad = self._ensure_parent(item)
        if bad is not None:
            return bad
        final = os.path.join(self.pc_dir, item.source_name, item.rel)
        try:
            fsrc = self.io.open_src(item.src)
        except OSError as e:
            return R_IN_USE if _winerror(e) in SHARING_ERRORS else R_UNREADABLE
        part = self._staging.part()
        written = 0
        with fsrc:
            try:
                fdst = self.io.open_part(part)
            except OSError as e:
                return self._write_error(e, None)
            try:
                with fdst:
                    while True:
                        if self.removed.is_set() or self.cancel.is_set():
                            fdst.close()
                            staging.discard_part(part)
                            self._check_stop()
                        try:
                            buf = fsrc.read(CHUNK)
                        except OSError as e:
                            fdst.close()
                            staging.discard_part(part)
                            return R_IN_USE if _winerror(e) in SHARING_ERRORS else R_UNREADABLE
                        if not buf:
                            break
                        fdst.write(buf)
                        written += len(buf)
                        self._done_bytes += len(buf)
                        self._emit(STAGE_COPY, self._done_files, self._total_files)
                    fdst.flush()
                    if written >= FSYNC_MIN_BYTES:
                        os.fsync(fdst.fileno())
            except OSError as e:
                return self._write_error(e, part)
        # 照合(FR-16): コピー元が変わっていないか・書いた大きさが合うか
        try:
            st = os.stat(item.src)
            changed = int(st.st_size) != item.size or int(st.st_mtime_ns) != item.mtime_ns
        except OSError:
            changed = True
        if changed or written != item.size:
            staging.discard_part(part)
            return R_CHANGED_DURING_COPY
        try:
            if os.stat(part).st_size != written:
                return self._write_error(OSError(errno.EIO, "size mismatch"), part)
        except OSError as e:
            return self._write_error(e, part)
        try:
            os.utime(part, ns=(item.atime_ns, item.mtime_ns))
        except OSError:
            self.out.utime_failed_count += 1   # §10: 日時を写せなくてもコピーは成功
            if len(self.out.utime_failed) < MAX_ROWS:
                self.out.utime_failed.append(drives.display_path(item.src))
        if item.kind == "changed" and os.path.lexists(final):
            rel = os.path.join(item.source_name, item.rel)
            if not self._move_to_old(rel):
                staging.discard_part(part)
                return R_OLD_MOVE_FAILED
        try:
            os.rename(part, final)   # 同じ名前があれば失敗する(上書きしない。B-8)
        except OSError as e:
            return self._write_error(e, part)
        self.out.bytes += written
        if item.kind == "new":
            self.out.new += 1
        else:
            self.out.changed += 1
        return None
