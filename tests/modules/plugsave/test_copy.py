# 計画とコピーのテスト(AC-1〜AC-8・AC-12・AC-16・AC-17)。コピー元もバックアップ先も tmp_path の中。
from __future__ import annotations

import ctypes
import datetime
import hashlib
import json
import os
import stat as _stat
import time
from ctypes import wintypes
from pathlib import Path
from typing import IO, Any

import pytest

from deskkit.modules.plugsave import copier, planner, staging
from deskkit.modules.plugsave.copier import BackupRequest, BackupRun, RealIO
from deskkit.modules.plugsave.fakes import FakeDriveApi
from deskkit.modules.plugsave.planner import Source

from .conftest import LETTER, SECRET_PC, SECRET_SRC, Env, ready_module, write


def _snapshot(root: Path) -> dict[str, tuple[str, int, int]]:
    out: dict[str, tuple[str, int, int]] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p.relative_to(root))] = (hashlib.sha256(p.read_bytes()).hexdigest(), st.st_size, st.st_mtime_ns)
    return out


def _run(e: Env, *, fs: str = "NTFS", io: Any = None, max_files: int = planner.MAX_PLAN_FILES, fit: bool = False,
         plan_only: bool = False, api: FakeDriveApi | None = None, reserve: int = copier.RESERVE_BYTES,
         sources: list[Source] | None = None) -> copier.BackupOutcome:
    req = BackupRequest(root=str(e.drive) + os.sep, fs=fs, pc=SECRET_PC, sources=sources or [Source(str(e.src), SECRET_SRC)],
                        fit=fit, plan_only=plan_only, max_files=max_files, reserve=reserve)
    return BackupRun(req, api or e.api, io=io).run()


def _work_files(e: Env) -> list[Path]:
    w = e.pc_dir() / staging.WORK_DIR
    return [p for p in w.rglob("*")] if w.exists() else []


# ---------------------------------------------------------------- AC-1・AC-2・AC-3・AC-4
def test_new_changed_deleted_and_source_untouched(env: Env) -> None:
    e = env
    t0 = time.time() - 3600
    write(e.src / "a.txt", b"alpha", t0)
    write(e.src / "sub" / "b.txt", b"bravo" * 1000, t0)
    write(e.src / "sub" / "deep" / "c.bin", os.urandom(3 * 1024 * 1024 + 7), t0)
    before = _snapshot(e.src)
    out = _run(e)
    assert out.result == "ok" and out.new == 3 and out.changed == 0
    for rel in ("a.txt", "sub/b.txt", "sub/deep/c.bin"):
        s, d = e.src / rel, e.dest() / rel
        assert d.read_bytes() == s.read_bytes()
        assert abs(d.stat().st_mtime_ns - s.stat().st_mtime_ns) <= planner.TOL_OTHER_NS
    assert _work_files(e) == []                      # AC-1: _作業中 は空
    old_b = (e.dest() / "sub" / "b.txt").read_bytes()
    # AC-2: 書き換えて再び
    write(e.src / "sub" / "b.txt", b"BRAVO-2", t0 + 100)
    before = _snapshot(e.src)
    out2 = _run(e)
    assert out2.result == "ok" and out2.changed == 1 and out2.new == 0 and out2.unchanged == 2 and out2.moved_old == 1
    olds = list((e.pc_dir() / "_以前の版").glob("*"))
    assert len(olds) == 1
    moved = olds[0] / SECRET_SRC / "sub" / "b.txt"
    assert hashlib.sha256(moved.read_bytes()).digest() == hashlib.sha256(old_b).digest()
    assert (e.dest() / "sub" / "b.txt").read_bytes() == b"BRAVO-2"
    assert _snapshot(e.src) == before                # AC-4
    # AC-3: コピー元で消しても先は残る
    (e.src / "a.txt").unlink()
    out3 = _run(e)
    assert out3.result == "ok"
    assert (e.dest() / "a.txt").read_bytes() == b"alpha"


def test_fsync_only_for_large_files(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    e = env
    t0 = time.time() - 3600
    write(e.src / "small.txt", b"s" * (copier.FSYNC_MIN_BYTES - 1), t0)
    write(e.src / "large.bin", b"L" * copier.FSYNC_MIN_BYTES, t0)
    sizes: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(copier.os, "fsync", lambda fd: (sizes.append(os.fstat(fd).st_size), real_fsync(fd))[1])
    out = _run(e)
    assert out.result == "ok" and out.new == 2
    assert sizes == [copier.FSYNC_MIN_BYTES]
    assert (e.dest() / "small.txt").stat().st_size == copier.FSYNC_MIN_BYTES - 1


def test_old_version_dir_gets_number_when_same_second(env: Env) -> None:
    e = env
    write(e.src / "x.txt", b"1", time.time() - 100)
    assert _run(e).new == 1
    for i in range(2):
        write(e.src / "x.txt", f"v{i + 2}".encode(), time.time() - 50 + i)
        req = BackupRequest(root=str(e.drive) + os.sep, fs="NTFS", pc=SECRET_PC, sources=[Source(str(e.src), SECRET_SRC)],
                            start=datetime.datetime(2026, 9, 28, 10, 15, 0))
        assert BackupRun(req, e.api).run().changed == 1
    names = sorted(p.name for p in (e.pc_dir() / "_以前の版").iterdir())
    assert names == ["2026-09-28_101500", "2026-09-28_101500 (2)"]


def test_dest_folder_with_file_name_and_file_with_folder_name_move_to_old(env: Env) -> None:
    e = env
    write(e.src / "doc", b"file now", time.time() - 10)                # 元はフォルダだった名前が今はファイル
    write(e.src / "dir" / "in.txt", b"inside", time.time() - 10)       # 元はファイルだった名前が今はフォルダ
    write(e.dest() / "doc" / "old.txt", b"old folder content")
    write(e.dest() / "dir", b"old file content")
    out = _run(e)
    assert out.result == "ok", out
    assert (e.dest() / "doc").read_bytes() == b"file now"
    assert (e.dest() / "dir" / "in.txt").read_bytes() == b"inside"
    old = next((e.pc_dir() / "_以前の版").iterdir()) / SECRET_SRC
    assert (old / "doc" / "old.txt").read_bytes() == b"old folder content"
    assert (old / "dir").read_bytes() == b"old file content"


# ---------------------------------------------------------------- AC-5
def _make_sparse(path: Path, size: int) -> None:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    k32.DeviceIoControl.restype = wintypes.BOOL
    import msvcrt

    with open(path, "wb") as f:
        h = msvcrt.get_osfhandle(f.fileno())
        ret = wintypes.DWORD()
        if not k32.DeviceIoControl(h, 0x000900C4, None, 0, None, 0, ctypes.byref(ret), None):   # FSCTL_SET_SPARSE
            pytest.skip("スパースのファイルを作れない")
        f.truncate(size)


def test_fat32_rules(env: Env) -> None:
    e = env
    big = e.src / "big.bin"
    _make_sparse(big, planner.FAT32_LIMIT)
    t = time.time() - 1000
    write(e.src / "same.txt", b"12345", t)
    write(e.dest() / "same.txt", b"12345", t + 1.5)
    pc = str(e.pc_dir())
    fat = planner.make_plan([Source(str(e.src), SECRET_SRC)], pc, "FAT32")
    assert fat.reasons == {"too_large_fat32": 1}
    assert fat.unchanged == 1 and fat.items == []
    ntfs = planner.make_plan([Source(str(e.src), SECRET_SRC)], pc, "NTFS")
    kinds = {i.rel: i.kind for i in ntfs.items}
    assert kinds == {"big.bin": "new", "same.txt": "changed"}
    exfat = planner.make_plan([Source(str(e.src), SECRET_SRC)], pc, "exFAT")
    assert exfat.unchanged == 1 and "too_large_fat32" not in exfat.reasons


def test_fat_before_1980_compares_size_only() -> None:
    old = planner.fat_min_ns() - 10**12
    assert planner.same_file(5, old, 5, old + 5 * 10**9, "FAT32")
    assert not planner.same_file(5, old, 5, old + 5 * 10**9, "NTFS")
    assert not planner.same_file(5, old, 6, old, "exFAT")


# ---------------------------------------------------------------- AC-6
class _FakeStat:
    def __init__(self, st: os.stat_result, attrs: int) -> None:
        self._st = st
        self.st_file_attributes = attrs
        self.st_reparse_tag = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._st, name)


class _Entry:
    def __init__(self, real: os.DirEntry[str], attrs: int | None) -> None:
        self._real = real
        self.name = real.name
        self.path = real.path
        self._attrs = attrs

    def stat(self, *, follow_symlinks: bool = True) -> Any:
        st = self._real.stat(follow_symlinks=follow_symlinks)
        if self._attrs is None:
            return st
        return _FakeStat(st, st.st_file_attributes | self._attrs)

    def is_symlink(self) -> bool:
        return self._real.is_symlink()


class _Scan:
    def __init__(self, path: str, marks: dict[str, int]) -> None:
        self._it = os.scandir(path)
        self._marks = marks

    def __enter__(self) -> _Scan:
        return self

    def __exit__(self, *a: Any) -> None:
        self._it.close()

    def __iter__(self) -> Any:
        for e in self._it:
            yield _Entry(e, self._marks.get(e.name))


class RecordingIO(RealIO):
    def __init__(self) -> None:
        self.opened: list[str] = []

    def open_src(self, path: str) -> IO[bytes]:
        self.opened.append(os.path.basename(path))
        return super().open_src(path)


def test_cloud_only_not_opened_and_junction_not_followed(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    import _winapi

    e = env
    write(e.src / "cloud.docx", b"placeholder")
    write(e.src / "local.txt", b"here")
    outside = e.tmp / "outside"
    write(outside / "far.txt", b"far away")
    _winapi.CreateJunction(str(outside), str(e.src / "jlink"))    # type: ignore[attr-defined]
    monkeypatch.setattr(planner, "scan_source",
                        lambda p: _Scan(p, {"cloud.docx": planner.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS}))
    io = RecordingIO()
    out = _run(e, io=io)
    assert out.result == "ok"
    assert out.reasons == {"cloud_only": 1}
    assert io.opened == ["local.txt"]
    assert out.new == 1
    assert not (e.dest() / "jlink").exists()
    assert not (e.dest() / "cloud.docx").exists()


def test_skips_names_links_hidden_system_dirs(env: Env) -> None:
    e = env
    write(e.src / "~$book.xlsx", b"lock")
    write(e.src / "Thumbs.db", b"t")
    write(e.src / "desktop.ini", b"d")
    hs = e.src / "hs"
    write(hs / "inside.txt", b"i")
    os.system(f'attrib +h +s "{hs}"')  # noqa: S605 - テスト用の一時フォルダに属性を付けるだけ
    write(e.src / "honly" / "x.txt", b"h")
    os.system(f'attrib +h "{e.src / "honly"}"')  # noqa: S605
    p = planner.make_plan([Source(str(e.src), SECRET_SRC)], str(e.pc_dir()), "NTFS")
    assert p.reasons == {"excluded_name": 3}
    assert [i.rel for i in p.items] == [os.path.join("honly", "x.txt")]   # 隠しだけ(システムでない)のフォルダには入る
    os.system(f'attrib -h -s "{hs}"')  # noqa: S605


# ---------------------------------------------------------------- AC-7
class LockedIO(RealIO):
    def __init__(self, locked: str) -> None:
        self.locked = locked

    def open_src(self, path: str) -> IO[bytes]:
        if os.path.basename(path) == self.locked:
            raise PermissionError(13, "The process cannot access the file", path, 32)
        return super().open_src(path)


def test_sharing_violation_is_in_use_and_continues(env: Env) -> None:
    e = env
    write(e.src / "a.txt", b"a")
    write(e.src / "locked.xlsx", b"l")
    write(e.src / "z.txt", b"z")
    out = _run(e, io=LockedIO("locked.xlsx"))
    assert out.result == "ok"
    assert out.reasons == {"in_use": 1}
    assert out.new == 2
    assert not (e.dest() / "locked.xlsx").exists()
    assert _work_files(e) == []


def test_unreadable_and_changed_during_copy(env: Env) -> None:
    e = env
    write(e.src / "bad.txt", b"b")
    write(e.src / "grow.txt", b"g" * 10)

    class IOx(RealIO):
        def open_src(self, path: str) -> IO[bytes]:
            name = os.path.basename(path)
            if name == "bad.txt":
                raise OSError(5, "denied", path, 5)
            f = super().open_src(path)
            if name == "grow.txt":
                with open(path, "ab") as g:     # コピーの途中で変わった(テストの一時ファイル)
                    g.write(b"more")
            return f

    out = _run(e, io=IOx())
    assert out.reasons == {"unreadable": 1, "changed_during_copy": 1}
    assert out.new == 0
    assert _work_files(e) == []


# ---------------------------------------------------------------- AC-8
class UnplugIO(RealIO):
    """書き込みの途中で先のルートを消し(名前を変えて退避)、winerror 1167 を出す。"""

    def __init__(self, drive: Path, away: Path) -> None:
        self.drive = drive
        self.away = away

    def open_part(self, path: str) -> IO[bytes]:
        real = super().open_part(path)
        io = self

        class W:
            def write(self, b: bytes) -> int:
                real.write(b[:100])
                real.close()
                os.rename(io.drive, io.away)
                raise OSError(22, "device not connected", None, 1167)

            def flush(self) -> None: ...

            def fileno(self) -> int:
                return real.fileno()

            def close(self) -> None:
                real.close()

            def __enter__(self) -> W:
                return self

            def __exit__(self, *a: Any) -> None:
                self.close()

        return W()  # type: ignore[return-value]


def test_unplug_midway_is_removed_and_next_run_cleans(env: Env) -> None:
    e = env
    write(e.src / "first.bin", os.urandom(5000))
    away = e.tmp / "unplugged"
    out = _run(e, io=UnplugIO(e.drive, away))
    assert out.result == "removed"
    assert not e.drive.exists()
    os.rename(away, e.drive)                          # 挿し直した
    assert not (e.dest() / "first.bin").exists()      # 最終の場所に壊れたファイルが無い
    parts = list((e.pc_dir() / staging.WORK_DIR).rglob("*.part"))
    assert len(parts) == 1
    keep1 = write(e.pc_dir() / staging.WORK_DIR / ("a" * 32) / "note.txt", b"keep")
    keep2 = write(e.pc_dir() / staging.WORK_DIR / "other" / "1.part", b"keep")
    out2 = _run(e)
    assert out2.result == "ok" and out2.new == 1 and out2.cleaned_parts == 1
    assert not parts[0].exists()
    assert keep1.exists() and keep2.exists()
    assert (e.dest() / "first.bin").read_bytes() == (e.src / "first.bin").read_bytes()


def test_removed_event_stops(env: Env) -> None:
    e = env
    for i in range(3):
        write(e.src / f"f{i}.txt", b"x")
    req = BackupRequest(root=str(e.drive) + os.sep, fs="NTFS", pc=SECRET_PC, sources=[Source(str(e.src), SECRET_SRC)])
    job = BackupRun(req, e.api)
    job.removed.set()
    assert job.run().result == "removed"


def test_readonly_drive_is_error(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    e = env
    write(e.src / "a.txt", b"a")

    def deny(self: staging.RunStaging) -> None:
        raise PermissionError(13, "write protected", None, 19)

    monkeypatch.setattr(staging.RunStaging, "create", deny)
    out = _run(e)
    assert out.result == "error" and out.error == "readonly"


def test_all_sources_missing_is_error(env: Env) -> None:
    out = _run(env, sources=[Source(str(env.tmp / "nope"), "nope")])
    assert out.result == "error" and out.error == "no_sources" and out.missing_sources == 1


# ---------------------------------------------------------------- AC-12
def test_no_space_and_fit(env: Env) -> None:
    e = env
    now = time.time()
    write(e.src / "newest.bin", b"n" * 1000, now - 10)
    write(e.src / "middle.bin", b"m" * 1000, now - 20)
    write(e.src / "oldest.bin", b"o" * 1000, now - 30)
    api = FakeDriveApi()
    api.add(LETTER, str(e.drive), free=copier.RESERVE_BYTES + 2500)
    out = _run(e, api=api)
    assert out.result == "no_space" and out.new == 0 and out.need_more == 3000 - 2500
    assert not e.dest().exists() or not any(e.dest().rglob("*.bin"))
    out2 = _run(e, api=api, fit=True)
    assert out2.result == "no_space" and out2.new == 2 and out2.left_out == 1
    assert sorted(p.name for p in e.dest().iterdir()) == ["middle.bin", "newest.bin"]
    assert _work_files(e) == []


def test_space_runs_out_during_copy(env: Env) -> None:
    e = env
    write(e.src / "a.bin", b"a" * 100)
    write(e.src / "b.bin", b"b" * 100)
    calls = {"n": 0}

    def free() -> int:
        calls["n"] += 1
        return copier.RESERVE_BYTES + 1000 if calls["n"] <= 2 else 50
    api = FakeDriveApi()
    api.add(LETTER, str(e.drive), free=free)
    out = _run(e, api=api)
    assert out.result == "no_space" and out.new == 1
    assert _work_files(e) == []


# ---------------------------------------------------------------- AC-16
def test_plan_limit_and_long_path(env: Env) -> None:
    e = env
    for i in range(8):
        write(e.src / f"f{i}.txt", str(i).encode())
    out = _run(e, max_files=5)
    assert out.result == "ok" and out.truncated and out.new == 5
    out2 = _run(e, max_files=5)
    assert out2.new == 3 and not out2.truncated          # 残りは次の回に(S-3)
    # 300 文字のパス
    deep = e.src
    while len(str(deep)) < 260:
        deep = deep / ("d" * 40)
    long_file = Path("\\\\?\\" + str(deep / ("L" * 20 + ".txt")))
    os.makedirs("\\\\?\\" + str(deep), exist_ok=True)
    long_file.write_bytes(b"long path")
    assert len(str(long_file)) - 4 >= 300 or len(str(e.dest()) + str(long_file)[len(str(e.src)) + 4:]) >= 300
    out3 = _run(e)
    assert out3.result == "ok" and out3.new == 1, out3
    rel = str(long_file)[4 + len(str(e.src)) + 1:]
    dst = "\\\\?\\" + str(e.dest() / rel)
    assert len(dst) - 4 > 260
    with open(dst, "rb") as f:
        assert f.read() == b"long path"


def test_case_only_duplicate_is_write_failed(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    e = env
    write(e.src / "a.txt", b"a")

    class Twice(_Scan):
        def __iter__(self) -> Any:
            for x in self._it:
                yield _Entry(x, None)
                y = _Entry(x, None)
                y.name = x.name.upper()
                yield y

    monkeypatch.setattr(planner, "scan_source", lambda p: Twice(p, {}))
    p = planner.make_plan([Source(str(e.src), SECRET_SRC)], str(e.pc_dir()), "NTFS")
    assert p.new_count == 1 and p.reasons == {"write_failed": 1}   # §10: 大文字・小文字だけ違う2つ目


# ---------------------------------------------------------------- AC-17
def test_counts_match_ops(env: Env) -> None:
    e = env
    t = time.time() - 500
    for i in range(4):
        write(e.src / f"n{i}.txt", b"n", t)
    write(e.src / "~$tmp.docx", b"x")
    m, did = ready_module(e)
    assert m.start_backup(did, trigger="manual") is None
    assert m.last.result == "ok" and m.last.new == 4
    write(e.src / "n0.txt", b"changed", t + 50)
    write(e.src / "new.txt", b"new")
    m.start_backup(did, trigger="manual")
    out = m.last
    assert (out.new, out.changed, out.unchanged, out.skipped) == (1, 1, 3, {"excluded_name": 1})
    rows = [json.loads(x) for x in (m.data_dir / "ops.jsonl").read_text(encoding="utf-8").splitlines()]
    last = rows[-1]
    assert (last["new"], last["changed"], last["unchanged"], last["skipped"], last["failed"]) == (1, 1, 3, {"excluded_name": 1}, 0)
    assert last["result"] == "ok" and last["trigger"] == "manual" and last["drive_slot"] == 1 and last["fs"] == "NTFS"
    assert set(last) == {"ts", "trigger", "result", "drive_slot", "fs", "new", "changed", "unchanged", "skipped", "failed",
                         "bytes", "ms", "prev_unfinished", "verified", "recopied", "verify_left"}   # 後ろの4つは v0.4.1
    assert (last["prev_unfinished"], last["verified"], last["recopied"], last["verify_left"]) == (False, 0, 0, 0)


def test_mode_bits_of_dest_are_regular_files(env: Env) -> None:
    write(env.src / "a.txt", b"a")
    _run(env)
    assert _stat.S_ISREG((env.dest() / "a.txt").stat().st_mode)
