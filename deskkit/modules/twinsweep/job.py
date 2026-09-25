# 1回のスキャンの流れ(FR-6): 「写真を数えています」→「特徴を調べています N / M」(キャッシュ+ワーカー最大 4 本)→
# 「グループにまとめています」。この関数は1本のスレッド(スキャンのスレッド)で動き、キャッシュの接続もこのスレッドだけで使う。
# 中止したら、そこまでに調べた特徴をコミットしてから返る。進捗は件数だけを渡す(パスは渡さない)。
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

from deskkit.modules.twinsweep.cache import FeatureCache
from deskkit.modules.twinsweep.grouping import Photo, exact_groups
from deskkit.modules.twinsweep.hashing import Features, FileChangedError, compute_features
from deskkit.modules.twinsweep.results import ResultModel
from deskkit.modules.twinsweep.scanner import MAX_FILES, FileEntry, enumerate_images

STAGE_COUNT = "count"
STAGE_FEATURES = "features"
STAGE_GROUP = "group"
MAX_WORKERS = 4
PROGRESS_INTERVAL_S = 0.1

Progress = Callable[[str, int, int], None]
ComputeFn = Callable[[str, int, int], Features]
FileKeyFn = Callable[[Photo], "tuple[int, int] | None"]


@dataclass
class ScanRequest:
    roots: list[str]
    recursive: bool
    level: str
    exact_only: bool
    excluded: list[str]
    cache_path: Path
    workers: int = MAX_WORKERS
    max_files: int = MAX_FILES


@dataclass
class ScanOutcome:
    status: str = "done"          # done / cancelled / too_many / cache_error
    model: ResultModel | None = None
    files: int = 0                # 数えた写真(クラウドのみを除く)
    unreadable: int = 0
    cloud_only: int = 0
    forbidden_roots: int = 0
    missing_roots: int = 0
    cache_rebuilt: bool = False
    opened: int = 0               # キャッシュに無く、開いて調べた枚数
    elapsed_ms: int = 0
    stage_ms: dict[str, int] = field(default_factory=dict)


def default_file_key(p: Photo) -> tuple[int, int] | None:
    """同じ実体かを見分けるファイル ID(ボリューム番号, ファイル番号)。開かずに stat で読む。"""
    try:
        st = os.stat(p.path)
    except OSError:
        return None
    if not st.st_ino:
        return None
    return int(st.st_dev), int(st.st_ino)


class _Throttle:
    def __init__(self, fn: Progress) -> None:
        self._fn = fn
        self._last = 0.0

    def __call__(self, stage: str, done: int, total: int, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last >= PROGRESS_INTERVAL_S:
            self._last = now
            self._fn(stage, done, total)


def run_scan(req: ScanRequest, cancel: threading.Event, progress: Progress, *, compute: ComputeFn = compute_features,
             file_key: FileKeyFn = default_file_key) -> ScanOutcome:
    t0 = time.perf_counter()
    out = ScanOutcome()
    tick = _Throttle(progress)
    tick(STAGE_COUNT, 0, 0, force=True)
    listing = enumerate_images(req.roots, recursive=req.recursive, excluded=req.excluded, cancel=cancel,
                               max_files=req.max_files, on_count=lambda n: tick(STAGE_COUNT, n, 0))
    out.cloud_only = listing.cloud_only
    out.forbidden_roots = listing.forbidden_roots
    out.missing_roots = listing.missing_roots
    out.files = len(listing.files)
    out.stage_ms[STAGE_COUNT] = int((time.perf_counter() - t0) * 1000)
    if listing.cancelled:
        out.status = "cancelled"
        return _finish(out, t0)
    if listing.too_many:
        out.status = "too_many"
        return _finish(out, t0)

    t1 = time.perf_counter()
    try:
        cache = FeatureCache(req.cache_path)
    except Exception:  # noqa: BLE001 - キャッシュが使えなくても調べる(作り直しも失敗したとき)
        cache = None
        out.status = "cache_error"
    photos: list[Photo] = []
    total = len(listing.files)
    done = 0

    def record(e: FileEntry, f: Features) -> None:
        if not f.readable or f.dhash is None:
            out.unreadable += 1
            return
        photos.append(Photo(len(photos), e.path, e.root, e.size, e.mtime_ns, f.sha256, f.dhash, f.width, f.height,
                            f.taken_at, f.sharpness))

    try:
        if cache is not None:
            out.cache_rebuilt = cache.rebuilt
        misses: list[FileEntry] = []
        for i, e in enumerate(listing.files):
            if i % 256 == 0 and cancel.is_set():
                break
            f = cache.get(e.path, e.size, e.mtime_ns) if cache is not None else None
            if f is None:
                misses.append(e)
            else:
                record(e, f)
                done += 1
                tick(STAGE_FEATURES, done, total)
        tick(STAGE_FEATURES, done, total, force=True)
        workers = max(1, min(req.workers, MAX_WORKERS, os.cpu_count() or 1))
        if misses and not cancel.is_set():
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="twinsweep-hash") as pool:
                inflight: dict[Future[Features], FileEntry] = {}
                it = iter(misses)
                exhausted = False
                while True:
                    while not exhausted and not cancel.is_set() and len(inflight) < workers * 4:
                        nxt = next(it, None)
                        if nxt is None:
                            exhausted = True
                            break
                        inflight[pool.submit(compute, nxt.path, nxt.size, nxt.mtime_ns)] = nxt
                    if not inflight:
                        break
                    finished, _ = wait(list(inflight), timeout=0.25, return_when=FIRST_COMPLETED)
                    for fut in finished:
                        e = inflight.pop(fut)
                        out.opened += 1
                        done += 1
                        try:
                            f = fut.result()
                        except FileChangedError:
                            out.unreadable += 1  # §10: 調べている間に消えた・変わった → 飛ばす。キャッシュにも書かない
                            continue
                        except Exception:  # noqa: BLE001 - 予期しない失敗も「読めない」扱いで先へ進む
                            out.unreadable += 1
                            continue
                        if cache is not None:
                            cache.put(e.path, e.size, e.mtime_ns, f)
                        record(e, f)
                    tick(STAGE_FEATURES, done, total)
        tick(STAGE_FEATURES, done, total, force=True)
    finally:
        if cache is not None:
            try:
                cache.close()  # 中止しても、そこまでに調べた特徴はコミットして残す(FR-6)
            except Exception:  # noqa: BLE001
                pass
    out.stage_ms[STAGE_FEATURES] = int((time.perf_counter() - t1) * 1000)
    if cancel.is_set():
        out.status = "cancelled"
        return _finish(out, t0)

    t2 = time.perf_counter()
    tick(STAGE_GROUP, 0, 0, force=True)
    exact, dropped = exact_groups(photos, file_key)
    live = [p for p in photos if p.pid not in dropped]
    model = ResultModel.build(live, exact, req.level, req.exact_only, cancel)
    out.stage_ms[STAGE_GROUP] = int((time.perf_counter() - t2) * 1000)
    if cancel.is_set():
        out.status = "cancelled"
        return _finish(out, t0)
    out.model = model
    if out.status != "cache_error":
        out.status = "done"
    return _finish(out, t0)


def _finish(out: ScanOutcome, t0: float) -> ScanOutcome:
    out.elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return out
