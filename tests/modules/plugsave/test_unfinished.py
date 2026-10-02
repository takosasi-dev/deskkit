# 途中で止まった回の手当て(v0.4.1。docs/v0.4.1/plugsave.md)。偽のドライブ(tmp_path の中のフォルダ)だけを使う。
# 止まった回のあとの回で、中身の欠けた小さいファイルが直ること・印が壊れている/無いときも落ちないことを確かめる。
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import IO, Any

import pytest

from deskkit.modules.plugsave import copier, staging, unfinished
from deskkit.modules.plugsave.copier import BackupRequest, BackupRun, RealIO
from deskkit.modules.plugsave.fakes import FakeDriveApi
from deskkit.modules.plugsave.planner import Source

from .conftest import SECRET_FILE, SECRET_LABEL, SECRET_PC, SECRET_SRC, Env, ready_module, write

HOUR_NS = 3600 * 10**9


def _req(e: Env, **kw: Any) -> BackupRequest:
    return BackupRequest(root=str(e.drive) + os.sep, fs=kw.pop("fs", "NTFS"), pc=SECRET_PC,
                         sources=[Source(str(e.src), SECRET_SRC)], **kw)


def _run(e: Env, *, io: Any = None, api: FakeDriveApi | None = None, **kw: Any) -> copier.BackupOutcome:
    return BackupRun(_req(e, **kw), api or e.api, io=io).run()


def _work(e: Env) -> Path:
    return e.pc_dir() / staging.WORK_DIR


def _run_dirs(e: Env) -> list[Path]:
    w = _work(e)
    return sorted(p for p in w.iterdir() if p.is_dir()) if w.exists() else []


def _birth(p: Path) -> int:
    return unfinished.birth_of(os.stat(p))


def _break(p: Path) -> None:
    """中身を同じ大きさの 0 にし、更新日時・作成日時はそのまま(抜かれて中身が書かれなかったファイルのまね)。"""
    st = p.stat()
    with open(p, "r+b") as f:
        f.write(b"\0" * st.st_size)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))


def _backdate(p: Path) -> None:
    """作成日時を 1 時間前にする(前の、終わった回が書いたファイルのまね)。"""
    assert unfinished.set_birth_ns(str(p), _birth(p) - HOUR_NS)


class UnplugAt(RealIO):
    """name のコピー元を読み始めたところで、先のドライブを外す(名前を変えて退避し、winerror 1167)。"""

    def __init__(self, drive: Path, away: Path, name: str) -> None:
        self.drive, self.away, self.name = drive, away, name
        self._cur = ""

    def open_src(self, path: str) -> IO[bytes]:
        self._cur = os.path.basename(path)
        return super().open_src(path)

    def open_part(self, path: str) -> IO[bytes]:
        if self._cur == self.name:
            os.rename(self.drive, self.away)
            raise OSError(22, "device not connected", None, 1167)
        return super().open_part(path)


class CancelAfter(RealIO):
    """n 個目のコピー元を開いたら「やめる」を押す。"""

    def __init__(self, n: int) -> None:
        self.n = n
        self.count = 0
        self.job: BackupRun | None = None

    def open_src(self, path: str) -> IO[bytes]:
        self.count += 1
        if self.count == self.n and self.job is not None:
            self.job.cancel.set()
        return super().open_src(path)


def _sources(e: Env, names: list[str], size: int = 10) -> dict[str, bytes]:
    t = time.time() - 5000
    out: dict[str, bytes] = {}
    for i, n in enumerate(names):
        data = (n.encode() * size)[: size + i]
        write(e.src / n, data, t)
        out[n] = data
    return out


# ---------------------------------------------------------------- 抜いた回のあとで直る
def test_removed_run_then_next_run_fixes_broken_small_files(env: Env) -> None:
    e = env
    _sources(e, ["a.txt", "b.txt", "c.txt"])
    write(e.src / "big.bin", b"B" * copier.FSYNC_MIN_BYTES, time.time() - 5000)      # fsync する大きさ
    write(e.src / "z.txt", b"not yet copied", time.time() - 5000)
    # 前の、最後まで終わった回(a.txt だけ)
    first = _run(e, max_files=1)
    assert first.result == "ok" and first.new == 1
    done_before = list(e.dest().iterdir())
    for p in done_before:
        _backdate(p)
    # 止まる回: b.txt・big.bin・c.txt を書いたあと、z.txt を書き始めるところで抜く
    away = e.tmp / "away"
    out1 = _run(e, io=UnplugAt(e.drive, away, "z.txt"))
    assert out1.result == "removed" and not out1.prev_unfinished
    os.rename(away, e.drive)                               # 挿し直した
    assert len(_run_dirs(e)) == 1                          # 止まった回の印が残る(名前は乱数だけ)
    assert staging.RUN_ID_RE.match(_run_dirs(e)[0].name)
    written = {p.name for p in e.dest().iterdir()} - {p.name for p in done_before}
    assert written == {"b.txt", "big.bin", "c.txt"}         # 止まる前に書いたファイル
    small_written = sorted(n for n in written if n != "big.bin")
    for n in small_written:
        _break(e.dest() / n)                                # 抜いたので中身が書かれなかった
    for p in done_before:
        if p.suffix == ".txt":
            _break(p)                                       # 前の終わった回のファイルは対象外(直さない)
    out2 = _run(e)
    assert out2.result == "ok", out2
    assert out2.prev_unfinished and not out2.check_all_small
    assert out2.recopied == len(small_written) and out2.verified == 0 and out2.new == 1   # z.txt は続きとして新しく
    assert out2.verify_left == 0 and out2.markers_left == 0
    for n in small_written:
        assert (e.dest() / n).read_bytes() == (e.src / n).read_bytes()
    olds = list((e.pc_dir() / "_以前の版").rglob("*.txt"))
    assert sorted(p.name for p in olds) == small_written   # 欠けた方は消さずに _以前の版 へ
    assert all(set(p.read_bytes()) == {0} for p in olds)
    for p in done_before:
        if p.suffix == ".txt":
            assert set(p.read_bytes()) == {0}               # 前の終わった回に書いた物は見ない
    assert _run_dirs(e) == []                               # 印を片づけた
    out3 = _run(e)
    assert out3.result == "ok" and not out3.prev_unfinished and out3.checked == 0


def test_crashed_run_leaves_marker_and_same_content_is_not_copied_again(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    e = env
    _sources(e, ["a.txt", "b.txt", "c.txt"])
    # DeskKit・PC が落ちた: 回の終わりの処理が走らない
    with monkeypatch.context() as mp:
        mp.setattr(BackupRun, "_finish_markers", lambda self: None)
        out1 = _run(e)
    assert out1.result == "ok" and out1.new == 3
    assert len(_run_dirs(e)) == 1
    _break(e.dest() / "b.txt")
    out2 = _run(e)
    assert out2.prev_unfinished and out2.verified == 2 and out2.recopied == 1 and out2.changed == 0 and out2.new == 0
    assert (e.dest() / "b.txt").read_bytes() == (e.src / "b.txt").read_bytes()
    assert out2.moved_old == 1                              # 同じ中身の2つは _以前の版 へ移さない
    assert _run_dirs(e) == []


def test_cancelled_run_is_unfinished(env: Env) -> None:
    e = env
    _sources(e, ["a.txt", "b.txt", "c.txt", "d.txt"])
    io = CancelAfter(3)
    job = BackupRun(_req(e), e.api, io=io)
    io.job = job
    out1 = job.run()
    assert out1.result == "cancelled" and out1.markers_left == 1
    assert len(_run_dirs(e)) == 1
    out2 = _run(e)
    assert out2.result == "ok" and out2.prev_unfinished and out2.checked == out1.new and out2.recopied == 0
    assert _run_dirs(e) == []


def test_marker_kept_until_checked_and_closed_by_next_run(env: Env) -> None:
    e = env
    _sources(e, ["a.txt", "b.txt"])
    # 1回目: 落ちた(印が残る)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(BackupRun, "_finish_markers", lambda self: None)
        assert _run(e).new == 2
    [m1] = _run_dirs(e)
    _break(e.dest() / "a.txt")
    # 2回目: 確かめて直し、新しいファイルも書いたが、コピー元の1つが見つからなかった(確かめ終えたと言えないので印を残す)
    time.sleep(0.05)
    write(e.src / "n1.txt", b"new one", time.time() - 100)
    req = BackupRequest(root=str(e.drive) + os.sep, fs="NTFS", pc=SECRET_PC,
                        sources=[Source(str(e.src), SECRET_SRC), Source(str(e.tmp / "gone"), "gone")])
    out2 = BackupRun(req, e.api).run()
    assert out2.result == "ok" and out2.prev_unfinished and out2.missing_sources == 1 and out2.prev_pending
    assert (out2.recopied, out2.verified, out2.new, out2.verify_left) == (1, 1, 1, 0)
    assert (m1 / staging.END_NAME).is_dir()                 # 2回目が印を閉じた
    assert _run_dirs(e) == [m1]                             # 印は残す。2回目自身は止まっていないので残さない
    # 3回目: 1回目が書いた物だけを確かめる。2回目が書いた物(直した a・n1)は閉じた印の窓の外なので見ない
    out3 = _run(e)
    assert out3.result == "ok" and out3.prev_unfinished and not out3.prev_pending
    assert (out3.checked, out3.recopied, out3.new) == (1, 0, 0)   # b だけを確かめる
    assert _run_dirs(e) == []


def test_birth_is_restored_after_tunneling(env: Env) -> None:
    e = env
    write(e.src / "x.txt", b"one", time.time() - 500)
    assert _run(e).new == 1
    dest = e.dest() / "x.txt"
    old_birth = _birth(dest)
    time.sleep(0.05)
    before = time.time_ns() - 50_000_000
    write(e.src / "x.txt", b"two!", time.time() - 400)
    out = _run(e)
    assert out.changed == 1 and out.birth_fix_failed == 0
    assert _birth(dest) != old_birth and _birth(dest) >= before    # 古い版の作成日時が戻っていない
    moved = next((e.pc_dir() / "_以前の版").rglob("x.txt"))
    assert _birth(moved) == old_birth


# ---------------------------------------------------------------- 印が壊れている・無い
def test_broken_or_foreign_markers_do_not_break_the_run(env: Env) -> None:
    e = env
    _sources(e, ["a.txt"])
    w = _work(e)
    w.mkdir(parents=True)
    write(w / ("f" * 32), b"a file with a run-id name")                    # 回の名前のファイル
    write(w / ("e" * 32) / "note.txt", b"someone else's")                  # ほかの物が入った回のフォルダ
    write(w / ("d" * 32) / staging.END_NAME, b"end is a file")            # end がファイル
    (w / "not-a-run").mkdir()
    out = _run(e)
    assert out.result == "ok" and not out.prev_unfinished and out.new == 1
    assert (w / ("f" * 32)).is_file() and (w / ("e" * 32) / "note.txt").exists() and (w / ("d" * 32) / staging.END_NAME).exists()
    assert (w / "not-a-run").is_dir()


def test_work_dir_is_a_file_is_error_not_crash(env: Env) -> None:
    e = env
    _sources(e, ["a.txt"])
    write(_work(e), b"not a folder")
    out = _run(e)
    assert out.result == "error" and not out.prev_unfinished


def test_unreadable_birth_checks_all_small_files(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    e = env
    _sources(e, ["a.txt", "b.txt"])
    write(e.src / "big.bin", b"B" * copier.FSYNC_MIN_BYTES, time.time() - 5000)
    assert _run(e).new == 3
    for p in e.dest().iterdir():
        _backdate(p)                                       # 前の回の物でも
    _break(e.dest() / "b.txt")
    (_work(e) / ("c" * 32)).mkdir(parents=True)
    real = unfinished.find_markers
    monkeypatch.setattr(unfinished, "find_markers",
                        lambda pc: [unfinished.Marker(m.path, 0, m.end_ns) for m in real(pc)])   # 作成日時が読めない
    out = _run(e)
    assert out.result == "ok" and out.prev_unfinished and out.check_all_small
    assert out.checked == 2 and out.recopied == 1            # 小さいファイルはすべて確かめる。大きいファイルは見ない
    assert (e.dest() / "b.txt").read_bytes() == (e.src / "b.txt").read_bytes()


def test_plan_only_reads_markers_without_touching(env: Env) -> None:
    e = env
    _sources(e, ["a.txt"])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(BackupRun, "_finish_markers", lambda self: None)
        _run(e)
    [m1] = _run_dirs(e)
    out = _run(e, plan_only=True)
    assert out.prev_unfinished and out.plan_verify == 1 and out.plan_new == 0 and out.need_more == 0
    assert not (m1 / staging.END_NAME).exists()             # 計画だけの回は印を閉じない


def test_suspect_windows() -> None:
    m = [unfinished.Marker("x", 100 * 10**9, 200 * 10**9), unfinished.Marker("y", 500 * 10**9, 0)]
    s = unfinished.build_suspect(m, "exFAT", 1000, 600 * 10**9)
    assert s is not None and not s.all_small
    assert s(10, 99 * 10**9) and s(10, 150 * 10**9) and not s(10, 200 * 10**9) and not s(10, 300 * 10**9)
    assert s(10, 550 * 10**9) and s(10, 600 * 10**9) and s(10, 900 * 10**9)   # この回より後の作成日時は確かめる(時計が戻った)
    assert not s(1000, 150 * 10**9)                           # fsync した大きさは見ない
    assert s(10, 0)                                           # そのファイルの作成日時が読めない → 確かめる
    assert unfinished.build_suspect([], "NTFS", 1000, 1) is None

    def all_small(markers: list[unfinished.Marker], fs: str, now: int) -> bool:
        got = unfinished.build_suspect(markers, fs, 1000, now)
        assert got is not None
        return got.all_small

    assert all_small(m, "UDF", 600 * 10**9)                                  # 作成日時を持つか分からないファイルシステム
    assert all_small(m, "NTFS", 50 * 10**9)                                  # 時計が戻った(印がこの回より後)
    assert all_small([unfinished.Marker("z", 5, -1)], "NTFS", 10)            # end の作成日時が読めない
    assert all_small([unfinished.Marker("z", 0, 0)], "NTFS", 10)             # 印の作成日時が読めない
    plan_only = unfinished.build_suspect([unfinished.Marker("y", 500 * 10**9, 0)], "NTFS", 1000, 0)
    assert plan_only is not None and plan_only(10, 10**18)


# ---------------------------------------------------------------- 画面・通知・ops・ログ
def test_module_shows_one_line_and_writes_counts_only(env: Env, qapp: Any) -> None:
    from deskkit.modules.plugsave.page import PlugSavePage

    e = env
    write(e.src / SECRET_FILE, b"secret content", time.time() - 5000)
    write(e.src / "b.txt", b"bravo", time.time() - 5000)
    m, did = ready_module(e)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(BackupRun, "_finish_markers", lambda self: None)
        assert m.start_backup(did, trigger="manual") is None
    assert m.last.new == 2
    _break(e.dest() / SECRET_FILE)
    m.start_backup(did, trigger="manual")
    out = m.last
    assert out.result == "ok" and out.recopied == 1 and out.verified == 1
    p = PlugSavePage(m)
    first = p.res_summary.text().splitlines()[0]
    assert first == "前回は途中で止まったので、その回に書いた小さいファイルを確かめ、中身の違った 1 件をもう一度コピーしました(確かめた数: 2 件)。"
    title, text, _level, _cb = e.ctx.notifications[-1]
    assert "前回の途中で欠けていた 1 件を直しました" in text
    row = json.loads((m.data_dir / "ops.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert (row["prev_unfinished"], row["verified"], row["recopied"], row["verify_left"]) == (True, 1, 1, 0)
    assert row["new"] == 0 and row["changed"] == 0 and row["unchanged"] == 1   # 確かめて同じだった物は「そのまま」にも数える
    assert "中身が欠けていたのでコピーし直した: 1 件" in p.res_summary.text()
    assert m.usage(7)[1].per_day[-1] == 2 + 1            # 1回目の新しい 2 件 + コピーし直した 1 件
    logs = e.ctx.handler.text()
    assert "unfinished runs prev=True" in logs and "recopied=1" in logs
    blob = logs + json.dumps(row, ensure_ascii=False) + text + title
    for secret in (SECRET_FILE, SECRET_PC, SECRET_SRC, SECRET_LABEL, str(e.drive)):
        assert secret not in blob
    p.deleteLater()


def test_unfinished_text_variants() -> None:
    from deskkit.modules.plugsave.module import unfinished_text

    o = copier.BackupOutcome()
    assert unfinished_text(o) is None
    o.prev_unfinished = True
    assert unfinished_text(o) == "前回は途中で止まりましたが、その回に書き終えた小さいファイルはありませんでした。"
    o.verified = 3
    assert unfinished_text(o) == "前回は途中で止まったので、その回に書いた小さいファイルを確かめました(3 件。中身はすべて合っていました)。"
    o.prev_pending = True
    assert unfinished_text(o) == ("前回は途中で止まったので、その回に書いた小さいファイルを確かめました(確かめた数: 3 件・もう一度コピーした数: "
                                  "0 件)。確かめ終えていない分は次の回に確かめます。")
    o2 = copier.BackupOutcome(result="no_space", prev_unfinished=True, prev_pending=True)
    assert unfinished_text(o2) == "前回は途中で止まりました。その回に書いた小さいファイルは、次の回に確かめます。"
    o3 = copier.BackupOutcome(prev_unfinished=True, check_all_small=True, recopied=1, verified=4)
    assert (unfinished_text(o3) or "").startswith("前回は途中で止まったので、作成日時で見分けられないため、小さいファイルをすべて確かめ")
