# モジュールの流れのテスト(AC-9〜AC-11・AC-13〜AC-15・AC-18・FR-8・FR-11・FR-20・FR-21・B-15)。
# 抜き差しは ctypes で組んだ DEV_BROADCAST_VOLUME をハンドラへ渡して真似る。ドライブは偽の Win32 と一時フォルダ。
from __future__ import annotations

import ctypes
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from deskkit.modules.plugsave import device, drives
from deskkit.modules.plugsave.module import (
    GAME_RECHECK_MS,
    N_FIRST_TITLE,
    N_MISMATCH_TEXT,
    N_START_TITLE,
    PlugSaveModule,
    merge_defaults,
    start_text,
)

from .conftest import LETTER, SECRET_FILE, SECRET_LABEL, SECRET_PC, SECRET_SRC, Env, FakeCtx, ready_module, write


def _vol(letters: str, devtype: int = device.DBT_DEVTYP_VOLUME, flags: int = 0) -> device.DEV_BROADCAST_VOLUME:
    v = device.DEV_BROADCAST_VOLUME()
    v.dbcv_size = ctypes.sizeof(v)
    v.dbcv_devicetype = devtype
    mask = 0
    for c in letters:
        mask |= 1 << (ord(c) - ord("A"))
    v.dbcv_unitmask = mask
    v.dbcv_flags = flags
    return v


def _plug(m: Any, ctx: FakeCtx, letters: str = LETTER, arrived: bool = True) -> None:
    v = _vol(letters)
    ctx.native[device.WM_DEVICECHANGE](device.DBT_DEVICEARRIVAL if arrived else device.DBT_DEVICEREMOVECOMPLETE,
                                       ctypes.addressof(v))


def _titles(ctx: FakeCtx) -> list[str]:
    return [n[0] for n in ctx.notifications]


# ---------------------------------------------------------------- AC-9
def test_handler_reads_struct_only(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    e = env
    m = e.make()
    m.start()
    ctx = e.ctx
    assert ctx is not None
    ctx.queue_calls = True
    got: list[tuple[bool, set[str]]] = []
    monkeypatch.setattr(m, "on_device_event", lambda arrived, letters: got.append((arrived, set(letters))))
    reads: list[str] = []
    monkeypatch.setattr(drives, "read_marker", lambda root: reads.append(root) or drives.MarkerRead("none"))
    real_open = open

    def spy_open(*a: Any, **k: Any) -> Any:
        reads.append("open")
        return real_open(*a, **k)

    monkeypatch.setattr("builtins.open", spy_open)
    e.api.calls.clear()
    h = ctx.native[device.WM_DEVICECHANGE]
    v = _vol("EF")
    h(device.DBT_DEVICEARRIVAL, ctypes.addressof(v))
    assert e.api.calls == [] and reads == []          # ハンドラの中で Win32 もファイルも使わない
    assert len(ctx.queued) == 1
    ctx.flush()
    assert got == [(True, {"E", "F"})]
    got.clear()
    for wp, lp in ((device.DBT_DEVICEARRIVAL, ctypes.addressof(_keep := _vol("E", devtype=3))),
                   (device.DBT_DEVICEARRIVAL, ctypes.addressof(_keep2 := _vol("E", flags=device.DBTF_NET))),
                   (device.DBT_DEVICEARRIVAL, 0),
                   (device.DBT_DEVNODES_CHANGED, ctypes.addressof(v))):
        h(wp, lp)
    assert ctx.queued == [] and got == []
    h(device.DBT_DEVICEREMOVECOMPLETE, ctypes.addressof(v))
    ctx.flush()
    assert got == [(False, {"E", "F"})]
    assert e.api.calls == []


def test_struct_layout() -> None:
    assert ctypes.sizeof(device.DEV_BROADCAST_HDR) == 12
    assert ctypes.sizeof(device.DEV_BROADCAST_VOLUME) == 20
    assert device.DEV_BROADCAST_VOLUME.dbcv_unitmask.offset == 12
    assert device.DEV_BROADCAST_VOLUME.dbcv_flags.offset == 16


# ---------------------------------------------------------------- AC-10
def test_auto_start_after_delay(env: Env) -> None:
    e = env
    write(e.src / SECRET_FILE, b"x")
    m, did = ready_module(e)
    ctx = e.ctx
    assert ctx is not None
    m.connected.clear()
    _plug(m, ctx)
    assert _titles(ctx)[-1] == N_START_TITLE
    assert ctx.notifications[-1][1] == start_text(10)
    timers = ctx.active_timers(10_000)
    assert len(timers) == 1 and timers[0].single_shot
    assert m.status_text() == "待機中"
    assert not (e.dest() / SECRET_FILE).exists()
    timers[0].fire()
    assert m.last is not None and m.last.result == "ok"
    assert (e.dest() / SECRET_FILE).exists()
    rows = [json.loads(x) for x in (m.data_dir / "ops.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["trigger"] == "auto"


def test_click_notification_cancels(env: Env) -> None:
    e = env
    write(e.src / "a.txt", b"x")
    m, _ = ready_module(e)
    ctx = e.ctx
    assert ctx is not None
    m.connected.clear()
    _plug(m, ctx)
    on_click = ctx.notifications[-1][3]
    on_click()
    assert m.pending is None and ctx.shown == 1
    for t in ctx.timers:
        t.fire()
    assert m.last is None and not e.dest().exists()


def test_start_delay_setting(env: Env) -> None:
    e = env
    write(e.src / "a.txt", b"x")
    m, _ = ready_module(e, section={"start_delay_s": 25})
    ctx = e.ctx
    assert ctx is not None
    m.connected.clear()
    _plug(m, ctx)
    assert ctx.notifications[-1][1] == start_text(25) and ctx.active_timers(25_000)


def test_snoozed_nothing(env: Env) -> None:
    e = env
    write(e.src / "a.txt", b"x")
    m, _ = ready_module(e)
    ctx = e.ctx
    assert ctx is not None
    ctx.snoozed = True
    n0 = len(ctx.notifications)
    m.connected.clear()
    _plug(m, ctx)
    assert len(ctx.notifications) == n0 and m.pending is None
    assert not ctx.active_timers(10_000)


def test_game_defers_until_clear(env: Env) -> None:
    e = env
    write(e.src / "a.txt", b"x")
    m, _ = ready_module(e)
    ctx = e.ctx
    assert ctx is not None
    ctx.game = True
    n0 = len(ctx.notifications)
    m.connected.clear()
    _plug(m, ctx)
    assert len(ctx.notifications) == n0
    chk = ctx.active_timers(GAME_RECHECK_MS)
    assert len(chk) == 1
    chk[0].fire()
    assert len(ctx.notifications) == n0 and m.last is None       # まだゲーム中
    ctx.game = False
    ctx.fullscreen = True
    chk[0].fire()
    assert len(ctx.notifications) == n0
    ctx.fullscreen = False
    chk[0].fire()
    assert _titles(ctx)[-1] == N_START_TITLE
    assert not chk[0].active
    ctx.active_timers(10_000)[0].fire()
    assert m.last is not None and m.last.result == "ok"


def test_game_defer_dropped_when_removed(env: Env) -> None:
    e = env
    write(e.src / "a.txt", b"x")
    m, _ = ready_module(e)
    ctx = e.ctx
    assert ctx is not None
    ctx.game = True
    m.connected.clear()
    _plug(m, ctx)
    assert m.pending is not None
    _plug(m, ctx, arrived=False)
    assert m.pending is None and not ctx.active_timers(GAME_RECHECK_MS)


# ---------------------------------------------------------------- AC-11
def test_first_time_only_notifies(env: Env) -> None:
    e = env
    write(e.src / "a.txt", b"x")
    m, did = ready_module(e, first_done=False)
    ctx = e.ctx
    assert ctx is not None
    m.connected.clear()
    _plug(m, ctx)
    assert _titles(ctx)[-1] == N_FIRST_TITLE
    assert m.pending is None and not ctx.active_timers(10_000) and m.last is None
    ctx.notifications[-1][3]()
    assert ctx.shown == 1
    # 画面から: 計画 → 始める
    assert m.make_preview(did) is None
    pv = m.previews[did]
    assert pv.plan_new == 1 and not e.dest().exists()
    assert m.start_backup(did, trigger="first") is None
    assert m.last.result == "ok" and m.drive_cfg(did)["first_done"] is True


def test_serial_mismatch_does_not_start(env: Env) -> None:
    e = env
    write(e.src / "a.txt", b"x")
    m, did = ready_module(e)
    ctx = e.ctx
    assert ctx is not None
    e.api.add(LETTER, str(e.drive), label=SECRET_LABEL, serial=0x9999)
    m.connected.clear()
    _plug(m, ctx)
    assert ctx.notifications[-1][1] == N_MISMATCH_TEXT
    assert m.pending is None and did in m.mismatched and did not in m.connected
    assert m.use_this_drive(did) is None
    assert m.drive_cfg(did)["serial"] == 0x9999 and did in m.connected


def test_unregistered_drive_nothing(env: Env) -> None:
    e = env
    write(e.src / "a.txt", b"x")
    m, _ = ready_module(e)
    ctx = e.ctx
    assert ctx is not None
    other = e.tmp / "other_drive"
    other.mkdir()
    e.api.add("R", str(other))
    n0 = len(ctx.notifications)
    _plug(m, ctx, "R")
    assert len(ctx.notifications) == n0 and m.pending is None
    assert not (other / drives.BACKUP_DIR).exists()


# ---------------------------------------------------------------- FR-8
def test_duplicate_signals_ignored_and_retry(env: Env) -> None:
    e = env
    write(e.src / "a.txt", b"x")
    m, _ = ready_module(e)
    ctx = e.ctx
    assert ctx is not None
    m.connected.clear()
    e.api.volume_fail_left[LETTER] = 2           # 2 回失敗してから読める(最大 3 回やり直す)
    _plug(m, ctx)
    assert m.pending is not None
    n = len(ctx.notifications)
    _plug(m, ctx)                                 # 待ち時間中の同じドライブ
    assert len(ctx.notifications) == n
    m.cancel_pending()
    e.clock.advance(10)
    _plug(m, ctx)                                 # 30 秒以内
    assert m.pending is None
    e.clock.advance(25)
    _plug(m, ctx)
    assert m.pending is not None


def test_retry_gives_up(env: Env) -> None:
    e = env
    write(e.src / "a.txt", b"x")
    m, _ = ready_module(e)
    ctx = e.ctx
    assert ctx is not None
    m.connected.clear()
    e.api.volume_fail_left[LETTER] = 4
    _plug(m, ctx)
    assert m.pending is None and not m.connected


def test_two_drives_one_at_a_time(env: Env) -> None:
    e = env
    write(e.src / "a.txt", b"x")
    m, d1 = ready_module(e)
    ctx = e.ctx
    assert ctx is not None
    d2root = e.tmp / "drive_r"
    d2root.mkdir()
    e.api.add("R", str(d2root), serial=0x2222)
    errs: list[str | None] = []
    m.register_drive("R", done=errs.append)
    assert errs == [None]
    for d in m.config["drives"]:
        d["first_done"] = True
    m.connected.clear()
    _plug(m, ctx, LETTER + "R")
    assert m.pending is not None and m.pending.letter == LETTER and m.queue == ["R"]
    ctx.active_timers(10_000)[0].fire()
    assert m.pending is not None and m.pending.letter == "R"
    ctx.active_timers(10_000)[0].fire()
    assert (d2root / drives.BACKUP_DIR / SECRET_PC / SECRET_SRC / "a.txt").exists()
    assert (e.dest() / "a.txt").exists()


# ---------------------------------------------------------------- B-15・FR-11
def test_start_does_not_auto_backup(env: Env) -> None:
    e = env
    write(e.src / "a.txt", b"x")
    m, did = ready_module(e)
    m.stop()
    m2 = e.make(e.ctx.settings_dict() if e.ctx else None)
    m2.start()
    assert did in m2.connected
    assert m2.pending is None and m2.last is None


def test_backup_now_manual(env: Env) -> None:
    e = env
    write(e.src / "a.txt", b"x")
    m, did = ready_module(e)
    ctx = e.ctx
    assert ctx is not None
    ctx.snoozed = True
    ctx.game = True
    assert m.backup_now() is None
    assert m.last is not None and m.last.result == "ok"
    rows = [json.loads(x) for x in (m.data_dir / "ops.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["trigger"] == "manual"


def test_backup_now_not_connected(env: Env) -> None:
    e = env
    write(e.src / "a.txt", b"x")
    m, _ = ready_module(e)
    ctx = e.ctx
    assert ctx is not None
    e.api.remove(LETTER)
    m.backup_now(from_tray=True)
    assert ctx.notifications[-1][1] == "登録したドライブがつながっていません"
    cb, _t = ctx.tray["今すぐバックアップ"]
    cb()
    assert ctx.notifications[-1][1] == "登録したドライブがつながっていません"


def test_backup_now_requires_setup(env: Env) -> None:
    m = env.make()
    m.start()
    assert m.backup_now() == "ドライブを登録してください"
    assert m.notices[-1].text == "ドライブを登録してください"


# ---------------------------------------------------------------- FR-20・FR-21
def test_stop_while_running(env: Env) -> None:
    e = env
    for i in range(30):
        write(e.src / f"f{i:02}.bin", os.urandom(200_000))
    gate = threading.Event()

    class SlowIO:
        def open_src(self, path: str) -> Any:
            gate.wait(0.05)
            return open(path, "rb")

        def open_part(self, path: str) -> Any:
            return open(path, "xb")

    m, did = ready_module(e, io=SlowIO())
    ctx = e.ctx
    assert ctx is not None
    m.threaded = True
    ctx.queue_calls = True
    assert m.start_backup(did, trigger="manual") is None
    time.sleep(0.2)
    t0 = time.perf_counter()
    m.stop()
    assert time.perf_counter() - t0 < 5.5
    parts = list((e.pc_dir() / "_作業中").rglob("*.part")) if (e.pc_dir() / "_作業中").exists() else []
    assert parts == []


def test_removal_during_run_sets_removed(env: Env) -> None:
    e = env
    write(e.src / "a.txt", b"x")
    m, did = ready_module(e)
    ctx = e.ctx
    assert ctx is not None
    started: list[Any] = []
    orig = m._spawn

    def spawn(fn: Any, name: str) -> Any:
        if name == "plugsave-run":
            started.append(fn)
            return None
        return orig(fn, name)

    m._spawn = spawn
    m.start_backup(did, trigger="manual")
    assert m.run is not None
    _plug(m, ctx, arrived=False)
    assert m.run.removed.is_set()
    started[0]()
    assert m.last.result == "removed"
    assert ctx.notifications[-1][1] == "バックアップの途中でドライブが外れました。次に挿したときに続きをコピーします。"


def test_cancel_timeout_marks_unresponsive(env: Env) -> None:
    e = env
    write(e.src / "a.txt", b"x")
    m, did = ready_module(e)
    ctx = e.ctx
    assert ctx is not None
    stuck = threading.Event()
    th = threading.Thread(target=stuck.wait, daemon=True)
    th.start()
    m._spawn = lambda fn, name: th if name == "plugsave-run" else fn()
    m.start_backup(did, trigger="manual")
    m.cancel_run()
    ctx.active_timers(5000)[0].fire()
    assert m.run is None and m.notices[-1].text == "ドライブの応答がありません"
    assert m.start_backup(did, trigger="manual") == "ドライブの応答がありません"
    stuck.set()
    th.join(2)
    assert m.start_backup(did, trigger="manual") is None


# ---------------------------------------------------------------- AC-13
def test_reminder(env: Env) -> None:
    e = env
    m, did = ready_module(e)
    ctx = e.ctx
    assert ctx is not None
    m.config["drives"][0]["last_success_at"] = e.clock.now().isoformat()
    assert ctx.active_timers(60_000) and ctx.active_timers(6 * 3600 * 1000)
    e.clock.advance(8 * 86400)
    n0 = len(ctx.notifications)
    ctx.snoozed = True
    m.check_reminder()
    assert len(ctx.notifications) == n0
    ctx.snoozed = False
    m.check_reminder()
    assert ctx.notifications[-1][:2] == ("8 日バックアップしていません", "バックアップ用のドライブを挿してください。")
    e.clock.advance(3600)
    m.check_reminder()
    assert len(ctx.notifications) == n0 + 1
    e.clock.advance(24 * 3600)
    m.check_reminder()
    assert len(ctx.notifications) == n0 + 2
    # 押すと: つながっていて初回が済んでいれば始める
    write(e.src / "a.txt", b"x")
    ctx.notifications[-1][3]()
    assert m.last is not None and m.last.result == "ok"


def test_reminder_from_registration_and_off(env: Env) -> None:
    e = env
    m, _ = ready_module(e, first_done=False)
    ctx = e.ctx
    assert ctx is not None
    e.clock.advance(7 * 86400 + 60)
    m.check_reminder()
    assert ctx.notifications[-1][0] == "7 日バックアップしていません"
    ctx.notifications[-1][3]()
    assert ctx.shown >= 1
    m.set_int("remind_days", 0)
    e.clock.advance(3 * 86400)
    n = len(ctx.notifications)
    m.check_reminder()
    assert len(ctx.notifications) == n


# ---------------------------------------------------------------- AC-14
def test_source_rules(env: Env) -> None:
    e = env
    m, _ = ready_module(e)
    appdata = Path(e.env["APPDATA"]) / "x"
    appdata.mkdir(parents=True)
    assert m.check_source(str(appdata)).status == "forbidden"
    assert m.check_source("C:\\").status == "forbidden"
    inside_drive = e.drive / "docs"
    inside_drive.mkdir()
    assert m.check_source(str(inside_drive)).status == "forbidden"
    (e.src / "sub").mkdir()
    assert m.check_source(str(e.src / "sub")).status == "nested"
    assert m.check_source(str(e.src.parent)).status == "nested"
    p1 = e.tmp / "a" / "写真"
    p2 = e.tmp / "b" / "写真"
    p1.mkdir(parents=True)
    p2.mkdir(parents=True)
    assert m.add_source(str(p1)) is None
    assert m.add_source(str(p2)) is None
    assert [s["name"] for s in m.config["sources"]][-2:] == ["写真", "写真 (2)"]
    p3 = e.tmp / "c" / "_以前の版"
    p3.mkdir(parents=True)
    assert m.add_source(str(p3)) is None
    assert m.config["sources"][-1]["name"] == "_以前の版 (2)"
    assert m.source_name_for(str(e.tmp / "zz"), "documents") == "ドキュメント"
    assert m.source_name_for("D:\\") == "Dドライブ"
    for i in range(10):
        (e.tmp / f"many{i}").mkdir()
    for i in range(10):
        m.add_source(str(e.tmp / f"many{i}"))
    assert len(m.config["sources"]) == 10
    (e.tmp / "eleven").mkdir()
    assert m.check_source(str(e.tmp / "eleven")).status == "full"


def test_remote_source_forbidden(env: Env) -> None:
    e = env
    m, _ = ready_module(e)
    assert m.check_source(r"\\server\share").status in ("missing", "remote")
    net = e.tmp / "net"
    net.mkdir()
    e.api.add("N", str(net), drive_type=4)
    assert drives.is_remote_path(e.api, "N:\\x") is True


# ---------------------------------------------------------------- AC-15
def test_register_marker_readme_and_unregister(env: Env) -> None:
    e = env
    m, did = ready_module(e)
    bdir = e.drive / drives.BACKUP_DIR
    marker = json.loads((bdir / drives.MARKER).read_text(encoding="utf-8"))
    assert marker == {"app": "DeskKit PlugSave", "format": 1, "id": did}
    assert (bdir / drives.README).is_file()
    readme_before = (bdir / drives.README).read_bytes()
    listing = sorted(str(p) for p in e.drive.rglob("*"))
    assert m.unregister_drive(did) is None
    assert sorted(str(p) for p in e.drive.rglob("*")) == listing
    errs: list[str | None] = []
    m.register_drive(LETTER, done=errs.append)
    assert errs == [None] and m.config["drives"][0]["id"] == did
    assert (bdir / drives.README).read_bytes() == readme_before
    errs.clear()
    m.register_drive(LETTER, done=errs.append)
    assert errs == ["このドライブは登録済みです"]
    assert not [p for p in bdir.iterdir() if p.name.endswith(".part")]


def test_broken_marker(env: Env) -> None:
    e = env
    m = e.make()
    m.start()
    bdir = e.drive / drives.BACKUP_DIR
    bdir.mkdir()
    (bdir / drives.MARKER).write_text("{broken", encoding="utf-8")
    got: list[Any] = []
    m.list_candidates(got.append)
    assert [c.state for c in got[0]] == ["broken"]
    errs: list[str | None] = []
    m.register_drive(LETTER, done=errs.append)
    assert errs == ["印を読めません"] and not m.config["drives"]
    m.register_drive(LETTER, replace_broken=True, done=errs.append)
    assert errs[-1] is None
    assert drives.read_marker(str(e.drive)).status == "ok"
    moved = list((bdir / drives.OLD_DIR).rglob(drives.MARKER))
    assert len(moved) == 1 and moved[0].read_text(encoding="utf-8") == "{broken"


def test_candidates_exclude_system_and_source_drives(env: Env) -> None:
    e = env
    m = e.make()
    m.start()
    sysroot = e.tmp / "sys_c"
    sysroot.mkdir()
    e.api.add("C", str(sysroot))
    cd = e.tmp / "cdrom"
    cd.mkdir()
    e.api.add("S", str(cd), drive_type=5)
    got: list[Any] = []
    m.list_candidates(got.append)
    assert [c.letter for c in got[0]] == [LETTER]
    e.api.calls.clear()
    m.list_candidates(got.append)
    assert e.api.calls.count("volume_info") == 1       # 種類が 2・3 以外(光学)には呼ばない
    assert got[-1][0].state == "new" and got[-1][0].label == SECRET_LABEL


def test_max_three_drives(env: Env) -> None:
    e = env
    m = e.make()
    m.start()
    for i, lt in enumerate("QRST"):
        root = e.tmp / f"d{lt}"
        root.mkdir(exist_ok=True)
        e.api.add(lt, str(root), serial=100 + i)
    errs: list[str | None] = []
    for lt in "QRST":
        m.register_drive(lt, done=errs.append)
    assert errs[:3] == [None, None, None] and errs[3] == "登録できるドライブは 3 台までです"


# ---------------------------------------------------------------- 設定
def test_merge_defaults() -> None:
    out, changed = merge_defaults({"start_delay_s": 99, "remind_days": -1, "auto_start": "yes", "sources": "x",
                                   "drives": [{"id": "bad"}], "last_reminded_at": 5})
    assert changed
    assert out["start_delay_s"] == 10 and out["remind_days"] == 7 and out["auto_start"] is True
    assert out["sources"] == [] and out["drives"] == [] and out["last_reminded_at"] is None
    good, ch2 = merge_defaults(out)
    assert not ch2 and good == out


def test_handle_cli_unsupported(env: Env) -> None:
    assert env.make().handle_cli(["open", "x"]) == (2, "unsupported")


# ---------------------------------------------------------------- AC-18
def test_no_names_in_logs_ops_notifications(env: Env) -> None:
    e = env
    write(e.src / SECRET_FILE, b"x")
    write(e.src / "~$lock.docx", b"x")
    m, did = ready_module(e)
    ctx = e.ctx
    assert ctx is not None
    m.connected.clear()
    _plug(m, ctx)
    ctx.active_timers(10_000)[0].fire()
    write(e.src / SECRET_FILE, b"changed!")
    m.backup_now()
    e.clock.advance(9 * 86400)
    m.check_reminder()
    e.api.remove(LETTER)
    m.backup_now(from_tray=True)
    texts = [ctx.handler.text(), (m.data_dir / "ops.jsonl").read_text(encoding="utf-8"), repr(m.diagnostics()),
             repr(m.usage(30)), "\n".join(f"{a} {b}" for a, b, _c, _d in ctx.notifications), "\n".join(ctx.statuses)]
    blob = "\n".join(texts)
    for secret in (SECRET_FILE, SECRET_LABEL, SECRET_PC, SECRET_SRC, str(e.tmp), f"{LETTER}:", "lock.docx"):
        assert secret not in blob, secret
    diag = m.diagnostics()
    assert set(diag) == {"state", "drives", "connected", "sources", "last_result", "days_since_success", "auto_start",
                         "remind_days", "start_delay_s"}


def test_usage_counts(env: Env) -> None:
    e = env
    write(e.src / "a.txt", b"x")
    write(e.src / "b.txt", b"x")
    m, did = ready_module(e)
    m.start_backup(did, trigger="manual")
    m.start_backup(did, trigger="manual")
    u = m.usage(7)
    assert u[0].key == "backups" and u[0].primary and u[0].per_day[-1] == 2
    assert u[1].per_day[-1] == 2
    assert "R-2" in (u[0].hint or "")


def test_status_texts(env: Env) -> None:
    e = env
    write(e.src / "a.txt", b"x")
    m, did = ready_module(e)
    assert m.status_text() == "まだバックアップしていません"
    m.start_backup(did, trigger="manual")
    assert m.status_text() == "最後のバックアップ: 今日"
    e.clock.advance(3 * 86400)
    assert m.status_text() == "最後のバックアップ: 3 日前"


def test_done_notification_levels(env: Env) -> None:
    e = env
    m, did = ready_module(e)
    ctx = e.ctx
    assert ctx is not None
    write(e.src / "~$x.docx", b"x")
    m.start_backup(did, trigger="manual")
    assert ctx.notifications[-1][:3] == ("バックアップが終わりました", "変わったファイルはありませんでした・飛ばした 1 件", "ok")
    write(e.src / "a.txt", b"x")
    m.start_backup(did, trigger="manual")
    assert ctx.notifications[-1][:3] == ("バックアップが終わりました", "コピー 1 件・飛ばした 1 件", "ok")


def test_open_backup_folder(env: Env) -> None:
    e = env
    write(e.src / "a.txt", b"x")
    m, did = ready_module(e)
    m.start_backup(did, trigger="manual")
    assert m.open_backup_folder() is None
    assert Path(m.opened[-1]) == e.pc_dir()


def test_selftest_runs() -> None:
    from deskkit.modules.plugsave.selftest import run

    assert run() == 0


def test_create_via_package(env: Env) -> None:
    from deskkit.modules.plugsave import create

    c = FakeCtx(env.tmp / "pkg")
    mod = create(c)
    assert isinstance(mod, PlugSaveModule)
