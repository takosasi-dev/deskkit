# DropSort 本体(service)と CLI のテスト。偽 Win32 層(メモリ上のファイル)と一時フォルダで動かす。
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from deskkit.modules.dropsort import _win32 as W
from deskkit.modules.dropsort import selftest as st
from deskkit.modules.dropsort.cli import run_command
from deskkit.modules.dropsort.fakewin32 import FakeWin32
from deskkit.modules.dropsort.oplog import OpLock

DL = st.DL
PDF = "C:\\Docs\\PDF"


@pytest.mark.parametrize(("name", "fn"), st.TESTS, ids=[n for n, _ in st.TESTS])
def test_selftest_scenarios(name: str, fn: Callable[[Path], None], tmp_path: Path) -> None:
    fn(tmp_path)


def test_selftest_run_returns_zero() -> None:
    assert st.run(verbose=False) == 0


def test_dryrun_then_apply_moves_pending(tmp_path: Path) -> None:
    fake, svc, clock = st.started(tmp_path, [st.rule("pdf", PDF, [".pdf"], mode="dry-run")])
    fake.add_file(DL + "\\a.pdf", 10, mtime=clock.t)
    st.settle(svc, clock)
    assert [r["op"] for r in svc.dryrun.all()] == ["would_move"]  # AC-4
    assert fake.get(DL + "\\a.pdf") is not None
    svc2, _ = st.make(tmp_path, fake, [st.rule("pdf", PDF, [".pdf"], mode="apply")])
    svc2.clock = clock
    st.settle(svc2, clock)
    assert fake.names_in(PDF) == ["a.pdf"]


def test_unavailable_drive_skips_without_disabling(tmp_path: Path) -> None:
    fake, svc, clock = st.started(tmp_path, [st.rule("usb", "U:\\Sorted", [".pdf"])])
    fake.add_file(DL + "\\a.pdf", 10, mtime=clock.t)
    st.settle(svc, clock)
    assert fake.get(DL + "\\a.pdf") is not None
    assert svc.rule_checks[0].unavailable
    assert svc.oplog.all() == []  # 未接続は記録も通知もしない(次回再評価)
    fake.add_volume("U:\\", "NTFS", serial=42)
    fake.mkdirs("U:\\Sorted")
    st.settle(svc, clock)
    assert fake.names_in("U:\\Sorted") == ["a.pdf"]


def test_dest_missing_and_self_are_invalid(tmp_path: Path) -> None:
    fake, svc, clock = st.started(tmp_path, [st.rule("missing", "C:\\Nope", [".pdf"]),
                                              st.rule("self", DL, [".pdf"]),
                                              st.rule("arch", DL + "\\_archive\\x", [".pdf"])])
    fake.mkdirs(DL + "\\_archive\\x")
    probs = svc.validate_rules(DL)
    codes = [svc.rule_checks[i].code for i in range(3)]
    assert codes == ["dest_missing", "dest_is_downloads", "dest_in_archive"] and len(probs) == 3


def test_junction_dest_resolved_before_network_check(tmp_path: Path) -> None:
    fake, svc, _ = st.started(tmp_path, [st.rule("j", "C:\\Link", [".pdf"])])
    fake.add_volume("Z:\\", "NTFS", serial=9, drive_type=W.DRIVE_REMOTE)
    fake.mkdirs("Z:\\share")
    fake.mkdirs("C:\\Link")
    fake.links["c:\\link"] = "Z:\\share"
    svc.validate_rules(DL)
    assert svc.rule_checks[0].code == "network_dest"  # INV-11: 最終パスで判定


def test_downloads_unresolved(tmp_path: Path) -> None:
    fake = FakeWin32(None)
    svc, _ = st.make(tmp_path, fake, [])
    assert svc.run_cycle().skipped == "unresolved"
    code, text = run_command(svc, ["status"])
    assert code == 4
    code, _ = run_command(svc, ["sort-existing"])
    assert code == 4


def test_override_wins(tmp_path: Path) -> None:
    fake = FakeWin32(DL)
    fake.mkdirs("C:\\Other")
    svc, _ = st.make(tmp_path, fake, [], downloads_dir_override="C:\\Other")
    assert svc.resolve() == "C:\\Other"


def test_cycle_skips_when_cli_holds_lock(tmp_path: Path) -> None:
    fake, svc, clock = st.started(tmp_path, [st.rule("pdf", PDF, [".pdf"])])
    fake.add_file(DL + "\\a.pdf", 10, mtime=clock.t)
    other = OpLock(tmp_path / "op.lock")
    assert other.try_acquire()
    try:
        st.settle(svc, clock)
        assert svc.run_cycle().skipped == "locked"
        assert fake.get(DL + "\\a.pdf") is not None
        code, _ = run_command(svc, ["undo"], lock_timeout=0.2)
        assert code == 2
    finally:
        other.release()
    st.settle(svc, clock)
    assert fake.names_in(PDF) == ["a.pdf"]


def test_cli_commands(tmp_path: Path) -> None:
    fake = FakeWin32(DL)
    fake.mkdirs(PDF)
    fake.add_volume("L:\\", "exFAT", serial=7)
    fake.mkdirs("L:\\X")
    fake.add_file(DL + "\\old.pdf", 10, mtime=st.T0 - 5000)
    fake.add_file(DL + "\\old.txt", 10, mtime=st.T0 - 5000, zone=st.ZONE3)
    svc, clock = st.make(tmp_path, fake, [st.rule("pdf", PDF, [".pdf"]), st.rule("txt", "L:\\X", [".txt"])])
    code, text = run_command(svc, ["sort-existing"])
    assert code == 0 and "移動予定" in text and "--apply" in text
    assert fake.get(DL + "\\old.pdf") is not None
    code, text = run_command(svc, ["sort-existing", "--apply"])
    assert code == 3, text  # old.txt は MOTW 付きで exFAT 宛 → 拒否
    assert fake.names_in(PDF) == ["old.pdf"] and fake.get(DL + "\\old.txt") is not None
    code, text = run_command(svc, ["status"])
    assert code == 0 and DL in text and "old.pdf" in text
    code, text = run_command(svc, ["undo", "--count", "5"])
    assert code == 0 and "元に戻した: 1 件" in text
    code, text = run_command(svc, ["undo"])
    assert code == 0 and "ありません" in text
    assert run_command(svc, ["bogus"])[0] == 1
    assert run_command(svc, [])[0] == 1
    code, text = run_command(svc, ["archive-now"])
    assert code == 0


def test_archive_month_is_last_touched(tmp_path: Path) -> None:
    import datetime as dt

    fake, svc, clock = st.started(tmp_path, [], archive={"enabled": True, "mode": "apply", "idle_days": 10,
                                                          "dir_name": "_archive", "use_atime": False})
    t = st.T0 - 100 * 86400
    fake.add_file(DL + "\\z.bin", 5, mtime=t, ctime=t)
    svc.run_cycle()
    clock.advance(11 * 86400)
    st.settle(svc, clock)
    month = dt.datetime.fromtimestamp(st.T0).strftime("%Y-%m")  # 初回観測の月(D-10/D-11)
    assert fake.get(DL + f"\\_archive\\{month}\\z.bin") is not None
    assert [r["op"] for r in svc.oplog.all()] == ["archive"]


def test_flagged_ack(tmp_path: Path) -> None:
    fake, svc, clock = st.started(tmp_path, [])
    fake.add_file(DL + "\\x.pdf.exe", 10, mtime=clock.t)
    st.settle(svc, clock)
    fl = svc.flagged_list()
    assert [f["name"] for f in fl] == ["x.pdf.exe"] and svc.get_snapshot()["flagged"] == 1
    assert svc.acknowledge("x.pdf.exe")
    assert svc.flagged_list() == []
    st.settle(svc, clock)
    assert fake.get(DL + "\\x.pdf.exe") is not None


def test_same_volume_uses_move_not_copy(tmp_path: Path) -> None:
    fake, svc, clock = st.started(tmp_path, [st.rule("pdf", PDF, [".pdf"])])
    fake.add_file(DL + "\\a.pdf", 10, mtime=clock.t, zone=st.ZONE3)
    st.settle(svc, clock)
    kinds = {c[0] for c in fake.calls}
    assert kinds == {"MoveFileExW"}
    assert fake.get(PDF + "\\a.pdf").streams[W.ZONE_STREAM] == st.ZONE3  # type: ignore[union-attr]
