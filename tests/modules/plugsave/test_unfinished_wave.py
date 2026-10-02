# 途中で止まった回の手当ての修正 wave(v0.4.1 のレビューの指摘 1〜4)。偽のドライブ(tmp_path の中のフォルダ)だけを使う。
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import IO, Any

import pytest

from deskkit.modules.plugsave import copier, planner, staging, unfinished
from deskkit.modules.plugsave.copier import BackupRequest, BackupRun, RealIO
from deskkit.modules.plugsave.planner import Source

from .conftest import SECRET_PC, SECRET_SRC, Env, write

MIN_NS = 60 * 10**9


def _req(e: Env, sources: list[Source] | None = None, **kw: Any) -> BackupRequest:
    return BackupRequest(root=str(e.drive) + os.sep, fs=kw.pop("fs", "NTFS"), pc=SECRET_PC,
                         sources=sources or [Source(str(e.src), SECRET_SRC)], **kw)


def _run(e: Env, *, io: Any = None, sources: list[Source] | None = None, **kw: Any) -> copier.BackupOutcome:
    return BackupRun(_req(e, sources, **kw), e.api, io=io).run()


def _crash_run(e: Env, **kw: Any) -> copier.BackupOutcome:
    """DeskKit・PC が落ちた回のまね(回の終わりの処理が走らない)。"""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(BackupRun, "_finish_markers", lambda self: None)
        return _run(e, **kw)


def _run_dirs(e: Env) -> list[Path]:
    w = e.pc_dir() / staging.WORK_DIR
    return sorted(p for p in w.iterdir() if p.is_dir()) if w.exists() else []


def _birth(p: Path) -> int:
    return unfinished.birth_of(os.stat(p))


def _break(p: Path) -> None:
    st = p.stat()
    with open(p, "r+b") as f:
        f.write(b"\0" * st.st_size)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))


def _files(root: Path, names: list[str], t: float) -> None:
    for n in names:
        write(root / n, (n * 20).encode(), t)


class CancelAt(RealIO):
    def __init__(self) -> None:
        self.job: BackupRun | None = None

    def open_src(self, path: str) -> IO[bytes]:
        if self.job is not None:
            self.job.cancel.set()
        return super().open_src(path)


# ---------------------------------------------------------------- 1. コピー元の一部が見つからない回では印を残す
def test_missing_source_keeps_marker_until_checked(env: Env) -> None:
    e = env
    t = time.time() - 5000
    src_t = e.tmp / "source" / "T"
    _files(e.src, ["a.txt"], t)
    _files(src_t, ["b.txt"], t)
    both = [Source(str(e.src), SECRET_SRC), Source(str(src_t), "T")]
    assert _crash_run(e, sources=both).new == 2
    _break(e.pc_dir() / "T" / "b.txt")
    away = e.tmp / "T_away"
    os.rename(src_t, away)                                   # T を外した
    out2 = _run(e, sources=both)
    assert out2.result == "ok" and out2.missing_sources == 1
    assert out2.markers_left >= 1 and len(_run_dirs(e)) == 1  # 確かめられない物があるので印は残す
    os.rename(away, src_t)                                    # T を戻した
    out3 = _run(e, sources=both)
    assert out3.prev_unfinished and out3.recopied == 1
    assert (e.pc_dir() / "T" / "b.txt").read_bytes() == (src_t / "b.txt").read_bytes()
    assert _run_dirs(e) == []


def test_unreadable_folder_during_plan_keeps_marker(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    e = env
    t = time.time() - 5000
    _files(e.src, ["a.txt"], t)
    _files(e.src / "sub", ["b.txt"], t)
    assert _crash_run(e).new == 2
    real = planner.scan_source

    def deny(p: str) -> Any:
        if os.path.basename(p) == "sub":
            raise PermissionError(5, "denied", p)
        return real(p)

    monkeypatch.setattr(planner, "scan_source", deny)
    out = _run(e)
    assert out.result == "ok" and out.markers_left == 1 and len(_run_dirs(e)) == 1
    monkeypatch.setattr(planner, "scan_source", real)
    out2 = _run(e)
    assert out2.checked == 2 and _run_dirs(e) == []


# ---------------------------------------------------------------- 2. 確かめる数が上限でも進む
def test_verify_does_not_starve_new_files(env: Env) -> None:
    e = env
    t = time.time() - 5000
    _files(e.src, ["a.txt", "b.txt"], t)
    assert _crash_run(e).new == 2
    _files(e.src, ["n1.txt", "n2.txt"], t)
    out = _run(e, max_files=2)
    assert out.new == 2 and out.checked == 2 and not out.truncated     # 確かめる物は上限に数えない
    assert out.result == "ok" and _run_dirs(e) == []


def test_verify_cap_makes_progress_and_does_not_repeat(env: Env) -> None:
    e = env
    t = time.time() - 5000
    names = ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"]
    _files(e.src, names, t)
    assert _crash_run(e).new == 5
    _break(e.pc_dir() / SECRET_SRC / "e.txt")                # 作成日時がいちばん新しい物
    seen: list[int] = []
    for _ in range(3):
        out = _run(e, max_verify=2)
        seen.append(out.checked)
        if not _run_dirs(e):
            break
    assert seen == [2, 2, 1]                                  # 同じ物を確かめ直さず、2・2・1 と進む
    assert (e.pc_dir() / SECRET_SRC / "e.txt").read_bytes() == (e.src / "e.txt").read_bytes()
    assert _run_dirs(e) == []


def test_verify_done_even_when_copy_runs_out_of_space_retires_marker(env: Env) -> None:
    from deskkit.modules.plugsave.fakes import FakeDriveApi

    from .conftest import LETTER

    e = env
    t = time.time() - 5000
    _files(e.src, ["a.txt", "b.txt"], t)
    assert _crash_run(e).new == 2
    _files(e.src, ["n1.txt"], t)
    api = FakeDriveApi()
    api.add(LETTER, str(e.drive), free=lambda: 1)            # 新しいファイルは入らない(確かめるのは書かない)
    out = BackupRun(_req(e, reserve=0), api).run()
    assert out.result == "no_space" and out.checked == 2 and out.new == 0
    assert out.markers_left == 0 and _run_dirs(e) == []       # 確かめ終えたので、止まった回の印は片づける


# ---------------------------------------------------------------- 3. 時計が戻った
def test_clock_went_back_between_runs_checks_future_files(env: Env) -> None:
    e = env
    t = time.time() - 5000
    _files(e.src, ["a.txt", "b.txt"], t)
    assert _crash_run(e).new == 2
    [m1] = _run_dirs(e)
    late = e.pc_dir() / SECRET_SRC / "b.txt"
    assert unfinished.set_birth_ns(str(late), time.time_ns() + 10 * MIN_NS)   # 前の回の後半(今より先の時刻)
    _break(late)
    alive = m1 / staging.ALIVE_NAME
    os.utime(alive, ns=(time.time_ns() + 10 * MIN_NS, time.time_ns() + 10 * MIN_NS))   # 前の回の終わり近く
    out = _run(e)
    assert out.check_all_small and out.check_reason == "clock"
    assert out.recopied == 1 and late.read_bytes() == (e.src / "b.txt").read_bytes()


def test_clock_went_back_during_run_is_all_small_and_remembered(env: Env) -> None:
    e = env
    t = time.time() - 5000
    _files(e.src, ["a.txt", "b.txt"], t)
    assert _crash_run(e).new == 2
    [m1] = _run_dirs(e)
    early = e.pc_dir() / SECRET_SRC / "b.txt"
    assert unfinished.set_birth_ns(str(early), _birth(m1) - 10 * MIN_NS)      # 回の途中で時計が戻った
    _break(early)
    past = _birth(m1) - 5 * MIN_NS
    os.utime(m1 / staging.ALIVE_NAME, ns=(past, past))
    io = CancelAt()
    job = BackupRun(_req(e), e.api, io=io)
    io.job = job
    out = job.run()                                           # 確かめる前にやめた → 印は残る
    assert out.result == "cancelled" and out.check_all_small and out.check_reason == "clock"
    out2 = _run(e)                                            # 次の回も「すべて確かめる」を覚えている
    assert out2.check_all_small and out2.check_reason == "clock" and out2.recopied == 1
    assert early.read_bytes() == (e.src / "b.txt").read_bytes()
    assert _run_dirs(e) == []


# ---------------------------------------------------------------- 4. v0.4.0 が残した印
def test_legacy_marker_checks_all_small_files(env: Env) -> None:
    from deskkit.modules.plugsave.module import unfinished_text

    e = env
    t = time.time() - 5000
    write(e.src / "x.txt", b"version one", t)
    assert _run(e).new == 1
    dest = e.pc_dir() / SECRET_SRC / "x.txt"
    old_birth = _birth(dest) - 3600 * 10**9
    assert unfinished.set_birth_ns(str(dest), old_birth)
    legacy = e.pc_dir() / staging.WORK_DIR / ("a1" * 16)
    legacy.mkdir(parents=True)                                # v0.4.0 の回のフォルダ(目印なし)
    # v0.4.0 の回が「変わった」ファイルを書いた: トンネリングで古い作成日時のまま、中身は欠けた
    write(e.src / "x.txt", b"version TWO", t + 100)
    dest.write_bytes(b"\0" * len(b"version TWO"))
    os.utime(dest, (t + 100, t + 100))
    assert unfinished.set_birth_ns(str(dest), old_birth)
    out = _run(e)
    assert out.prev_unfinished and out.check_all_small and out.check_reason == "legacy"
    assert out.recopied == 1 and dest.read_bytes() == b"version TWO"
    assert "前の版の DeskKit" in (unfinished_text(out) or "")
    assert _run_dirs(e) == []


def test_v041_run_folder_has_version_mark_and_it_is_cleaned(env: Env) -> None:
    e = env
    _files(e.src, ["a.txt"], time.time() - 5000)
    _crash_run(e)
    [m1] = _run_dirs(e)
    assert (m1 / staging.ALIVE_NAME).is_file() and (m1 / staging.ALIVE_NAME).stat().st_size == 0
    [mk] = unfinished.find_markers(str(e.pc_dir()))
    assert mk.versioned
    out = _run(e)
    assert out.result == "ok" and not out.check_all_small and _run_dirs(e) == []
    assert not (e.pc_dir() / staging.WORK_DIR).exists() or not any((e.pc_dir() / staging.WORK_DIR).iterdir())
