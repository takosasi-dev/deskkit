# StartupWatch の受け入れ基準(AC-1〜AC-10・AC-13)と、FR-4・FR-7・FR-17・FR-18・§9・§10 の振る舞い。偽の Win32 と偽 ctx で確かめる。
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from deskkit.modules.startupwatch import sources
from deskkit.modules.startupwatch.fakes import FakeApi, Signal, reg_key_of, sample_api
from deskkit.modules.startupwatch.module import NOTIFY_TITLE, validate

from .conftest import SECRET_CMD, SECRET_NAME, FakeCtx


def _baseline(make_module: Any, api: FakeApi, section: dict[str, Any] | None = None) -> FakeCtx:
    m, ctx, _ = make_module(api, section=section)
    m.start()
    m.stop()
    return ctx


def _events(ctx: FakeCtx) -> list[dict[str, Any]]:
    p = ctx.data_dir / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------- AC-1・FR-1
def test_ac1_first_start_only_remembers(make_module: Any) -> None:
    api = sample_api()
    m, ctx, _ = make_module(api)
    m.start()
    assert ctx.notifications == []
    ev = _events(ctx)
    assert [e["event"] for e in ev] == ["baseline"]
    assert ev[0]["n"] == 2
    assert m.banner == ("baseline", 2)
    rec = json.loads((ctx.data_dir / "known.json").read_text(encoding="utf-8"))
    assert rec["version"] == 1 and len(rec["items"]) == 2
    assert all(v["flag"] == "known" for v in rec["items"].values())


# ---------------------------------------------------------------- AC-2・FR-2・FR-5・INV-5
def test_ac2_added_while_stopped_is_notified_once_without_names(make_module: Any) -> None:
    api = sample_api()
    ctx = _baseline(make_module, api)
    api.add_value("hkcu_run", SECRET_NAME, SECRET_CMD)
    api.add_file(api.folders["startup"], "ZzSecondApp.lnk", r"C:\Apps\zz-second.exe")
    m, _, _ = make_module(api, ctx)
    m.start()
    assert len(ctx.notifications) == 1
    title, text, level, on_click = ctx.notifications[0]
    assert title == NOTIFY_TITLE and level == "info"
    assert text == "2 個増えました。押すと、何が増えたかを確かめられます。"
    for s in (SECRET_NAME, "zz-secret", "ZzSecondApp", "zz-second", "Users"):
        assert s.lower() not in (title + text).lower()
    on_click()
    assert ctx.shown == 1
    assert m.new_count() == 2
    assert ctx.status == "新しい物 2 件"


# ---------------------------------------------------------------- AC-3・FR-3
def test_ac3_burst_of_signals_rescans_once(make_module: Any) -> None:
    api = sample_api()
    ctx = _baseline(make_module, api)
    api.script = [Signal(1.0, "hkcu_run", lambda: api.add_value("hkcu_run", SECRET_NAME, SECRET_CMD))]
    api.script += [Signal(1.0 + 0.5 * i, "hkcu_run") for i in range(1, 5)]
    m, _, _ = make_module(api, ctx)
    m.start()
    assert m.scans == 2  # 起動時の1回 + 合図のまとまりで1回
    assert len(ctx.notifications) == 1
    assert api.now >= 3.0 + 2.0  # 最後の合図から settle_ms(2 秒)待った


def test_fr3_settle_is_capped_at_10_seconds(make_module: Any) -> None:
    api = sample_api()
    ctx = _baseline(make_module, api)
    api.script = [Signal(1.0 + 1.0 * i, "hkcu_run") for i in range(20)]  # 1 秒おきに 20 回
    m, _, _ = make_module(api, ctx)
    m.start()
    # 最初の合図から 10 秒で1回読み直し、その後の合図のまとまりでもう1回
    assert m.scans == 3


def test_signal_from_folder_triggers_rescan(make_module: Any) -> None:
    api = sample_api()
    ctx = _baseline(make_module, api)
    folder = api.folders["startup"]
    api.script = [Signal(5.0, "startup_user", lambda: api.add_file(folder, "Zz.lnk", r"C:\Apps\zz.exe"))]
    m, _, _ = make_module(api, ctx)
    m.start()
    assert len(ctx.notifications) == 1
    v = m.new_items()[0]
    assert v.item.display_name == "Zz" and v.item.command == r"C:\Apps\zz.exe" and v.item.folder_path == folder


# ---------------------------------------------------------------- AC-4・W-4
def test_ac4_changed_command_marks_but_does_not_notify_and_removed_is_dropped(make_module: Any) -> None:
    api = sample_api()
    api.add_value("hkcu_run", "Updater", r"C:\App\v1\up.exe")
    api.add_value("hkcu_run", "Gone", r"C:\App\gone.exe")
    ctx = _baseline(make_module, api)
    api.remove_value("hkcu_run", "Updater")
    api.add_value("hkcu_run", "UPDATER", r"C:\App\v2\up.exe")  # 名前の大文字小文字は同じ物
    api.remove_value("hkcu_run", "Gone")
    m, _, _ = make_module(api, ctx)
    m.start()
    assert ctx.notifications == []
    marks = {v.item.name: v.marks for v in m.view_items()}
    assert marks["UPDATER"] == ["中身が変わりました"]
    assert "Gone" not in marks
    rec = json.loads((ctx.data_dir / "known.json").read_text(encoding="utf-8"))
    assert len(rec["items"]) == 3
    kinds = [e["event"] for e in _events(ctx)]
    assert "changed" in kinds and "removed" in kinds and "added" not in kinds


# ---------------------------------------------------------------- AC-5・W-9
def test_ac5_runonce_is_not_notified_by_default(make_module: Any) -> None:
    api = sample_api()
    ctx = _baseline(make_module, api)
    api.add_value("hkcu_runonce", "Finish", r"C:\Temp\finish.exe")
    m, _, _ = make_module(api, ctx)
    m.start()
    assert ctx.notifications == []
    v = next(v for v in m.view_items() if v.item.name == "Finish")
    assert v.marks == ["1回だけ"]
    ev = [e for e in _events(ctx) if e["event"] == "added"]
    assert ev and ev[0]["notified"] is False


def test_ac5_runonce_notified_when_enabled(make_module: Any) -> None:
    api = sample_api()
    ctx = _baseline(make_module, api, {"notify_runonce": True})
    api.add_value("hklm_runonce32", "Finish", r"C:\Temp\finish.exe")
    m, _, _ = make_module(api, ctx)
    m.start()
    assert len(ctx.notifications) == 1
    assert "1 個増えました" in ctx.notifications[0][1]


# ---------------------------------------------------------------- AC-6・W-10
def test_ac6_deskkit_itself_is_marked_and_not_notified(make_module: Any) -> None:
    api = sample_api()
    ctx = _baseline(make_module, api)
    api.add_value("hkcu_run", "DeskKit", '"c:\\TOOLS\\deskkit\\deskkit.EXE" --autostart')
    api.add_value("hkcu_run", "DeskKitNoQuote", r"C:\Tools\DeskKit\DeskKit.exe --autostart")
    m, _, _ = make_module(api, ctx)
    m.start()
    assert ctx.notifications == []
    marks = {v.item.name: v.marks for v in m.view_items()}
    assert marks["DeskKit"] == ["DeskKit"] and marks["DeskKitNoQuote"] == ["DeskKit"]


def test_w10_source_run_counts_pythonw_as_deskkit() -> None:
    from deskkit.modules.startupwatch.module import deskkit_programs

    progs = deskkit_programs(r"D:\x\.venv\Scripts\python.exe")
    assert r"d:\x\.venv\scripts\pythonw.exe" in progs and r"d:\x\.venv\scripts\python.exe" in progs


# ---------------------------------------------------------------- AC-7・W-11・FR-14
def test_ac7_snoozed_does_not_read_and_resume_notifies_once(make_module: Any) -> None:
    api = sample_api()
    ctx = _baseline(make_module, api)
    api.add_value("hkcu_run", SECRET_NAME, SECRET_CMD)
    ctx.snoozed = True
    reads = api.reads
    m, _, _ = make_module(api, ctx)
    m.start()
    assert api.reads == reads  # 読まない
    assert ctx.notifications == []
    assert ctx.status == "一時停止中"
    before = (ctx.data_dir / "known.json").read_text(encoding="utf-8")
    assert "1 個" not in before
    ctx.snoozed = False
    ctx.emit_event("host.snooze_changed", {"snoozed": False, "until": None})
    assert len(ctx.notifications) == 1
    assert ctx.status == "新しい物 1 件"


def test_snooze_event_while_still_snoozed_does_nothing(make_module: Any) -> None:
    api = sample_api()
    ctx = _baseline(make_module, api)
    ctx.snoozed = True
    m, _, _ = make_module(api, ctx)
    m.start()
    reads = api.reads
    ctx.emit_event("host.snooze_changed", {"snoozed": True, "until": None})
    assert api.reads == reads


# ---------------------------------------------------------------- AC-8・AC-9・FR-4
def test_ac8_access_denied_location_is_unreadable_others_read(make_module: Any) -> None:
    api = sample_api()
    api.reg_errors[reg_key_of("hklm_run64")] = 5
    m, ctx, _ = make_module(api)
    m.start()
    assert m.location_mode("hklm_run64") == "unreadable"
    assert m.last_scan is not None
    assert all(r.status == "ok" for k, r in m.last_scan.locs.items() if k != "hklm_run64")
    assert m.diagnostics()["watch.hklm_run64"] == "unreadable"
    assert "arm loc=hklm_run64 rc=5" in ctx.handler.text()


def test_ac9_64_and_32_bit_views_are_separate(make_module: Any) -> None:
    api = sample_api()
    api.set_values("hklm_run64", [("Only64", r"C:\A\sixty.exe")])
    api.set_values("hklm_run32", [("Only32", r"C:\A\thirty.exe")])
    res = sources.read_all(api, set())
    assert res is not None
    assert [i.name for i in res.locs["hklm_run64"].items] == ["Only64"]
    assert [i.name for i in res.locs["hklm_run32"].items] == ["Only32"]


def test_fr4_missing_keys_and_network_folder_are_polled(make_module: Any) -> None:
    api = sample_api()
    del api.reg[reg_key_of("hkcu_runonce")]
    api.remote.add(api.folders["common_startup"])
    api.idle_timeouts = 1
    m, ctx, _ = make_module(api)
    m.start()
    assert m.location_mode("hkcu_runonce") == "poll"
    assert m.location_mode("startup_common") == "poll"
    assert m.location_mode("hkcu_run") == "notify"
    assert 15 * 60_000 in api.waits  # poll_minutes ごとに起きる
    assert m.scans == 2  # 起動時 + 1 回の読み直し
    assert m.last_scan is not None and m.last_scan.locs["hkcu_runonce"].status == "missing"


def test_inv7_waits_forever_when_everything_is_notified(make_module: Any) -> None:
    from deskkit.modules.startupwatch import _win32

    api = sample_api()
    m, _, _ = make_module(api)
    m.start()
    assert api.waits and all(w == _win32.INFINITE for w in api.waits)


def test_key_created_later_is_armed_after_poll(make_module: Any) -> None:
    api = sample_api()
    del api.reg[reg_key_of("hkcu_runonce")]
    api.idle_timeouts = 1
    orig = api.wait_any
    modes_at_first_wait: list[str] = []

    def wait_any(handles: list[int], timeout_ms: int) -> int:
        if timeout_ms == 15 * 60_000:  # 鍵が無い間は poll_minutes ごとに起きる。その間に鍵が作られた
            modes_at_first_wait.append(m.watcher.modes["hkcu_runonce"])
            api.set_values("hkcu_runonce", [])
        return orig(handles, timeout_ms)

    api.wait_any = wait_any  # type: ignore[method-assign]
    m, _, _ = make_module(api)
    m.start()
    assert modes_at_first_wait[0] == "poll"
    assert m.watcher.modes["hkcu_runonce"] == "notify"


# ---------------------------------------------------------------- AC-10・INV-4
def test_ac10_no_names_commands_paths_in_files_logs_diagnostics_usage(make_module: Any) -> None:
    api = sample_api()
    ctx = _baseline(make_module, api)
    api.add_value("hkcu_run", SECRET_NAME, SECRET_CMD)
    api.add_file(api.folders["startup"], "ZzLinkSecret.lnk", r"C:\Users\zz-user\zz-link.exe")
    api.reg_errors[reg_key_of("hklm_run32")] = 5
    m, _, _ = make_module(api, ctx)
    m.start()
    m.ack_all()
    m.page_opened()
    blob = "".join(p.read_text(encoding="utf-8") for p in ctx.data_dir.iterdir() if p.is_file())
    blob += ctx.handler.text()
    blob += json.dumps(m.diagnostics(), ensure_ascii=False)
    blob += repr(m.usage(7))
    for s in (SECRET_NAME, "zz-secret", "ZzLinkSecret", "zz-link", "zz-user", "OneDriveSample", "od.exe", "sample",
              "SecurityHealth", "C:\\", "Start Menu", "Roaming"):
        assert s.lower() not in blob.lower(), s


# ---------------------------------------------------------------- AC-13・FR-15
def test_ac13_stop_closes_everything(make_module: Any) -> None:
    api = sample_api()
    m, _, _ = make_module(api)
    m.start()
    m.stop()
    assert api.open == set()


def test_ac13_threaded_stop_joins_and_closes(make_module: Any) -> None:
    api = sample_api()
    api.block_when_idle = True
    m, ctx, _ = make_module(api, threaded=True)
    m.start()
    deadline = time.monotonic() + 5
    while m.scans < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert m.scans == 1
    api.script.append(Signal(api.now, "hkcu_run", lambda: api.add_value("hkcu_run", SECRET_NAME, SECRET_CMD)))
    with api._cv:  # noqa: SLF001 - 待っている偽の wait_any を起こす
        api._cv.notify_all()  # noqa: SLF001
    deadline = time.monotonic() + 5
    while not ctx.notifications and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(ctx.notifications) == 1
    t0 = time.monotonic()
    m.stop()
    assert time.monotonic() - t0 < 2.5
    assert m.watcher._thread is not None and not m.watcher._thread.is_alive()  # noqa: SLF001
    assert api.open == set()


def test_manual_rescan_kicks_threaded_watcher(make_module: Any) -> None:
    api = sample_api()
    api.block_when_idle = True
    m, _ctx, _ = make_module(api, threaded=True)
    m.start()
    deadline = time.monotonic() + 5
    while m.scans < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    m.rescan()
    deadline = time.monotonic() + 5
    while m.scans < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert m.scans == 2
    assert api.proc_reads >= 1


# ---------------------------------------------------------------- §10: 見張りが例外で止まった
def test_watcher_restarts_once_then_degrades_to_polling(make_module: Any) -> None:
    api = sample_api()
    api.wait_fail_times = 2
    m, ctx, _ = make_module(api, threaded=True)
    m.start()
    deadline = time.monotonic() + 5
    while not m.watcher.degraded and time.monotonic() < deadline:
        time.sleep(0.01)
    assert m.watcher.degraded
    assert all(v in ("poll", "unreadable") for v in m.watcher.modes.values())
    assert "watcher failed: WatcherError (n=2)" in ctx.handler.text()
    m.stop()
    assert api.open == set()


def test_watcher_restarts_once_and_keeps_watching(make_module: Any) -> None:
    api = sample_api()
    api.wait_fail_times = 1
    m, _ctx, _ = make_module(api)
    m.start()
    assert not m.watcher.degraded and m.watcher.failures == 1
    assert m.scans == 2  # 起動時と作り直した時
    m.stop()
    assert api.open == set()


# ---------------------------------------------------------------- FR-7・FR-17・ack
def test_fr7_ack_persists_and_new_stays_new_across_restart(make_module: Any) -> None:
    api = sample_api()
    ctx = _baseline(make_module, api)
    api.add_value("hkcu_run", "A1", r"C:\A\a1.exe")
    api.add_value("hkcu_run", "A2", r"C:\A\a2.exe")
    m, _, _ = make_module(api, ctx)
    m.start()
    m.stop()
    m2, _, _ = make_module(api, ctx)
    m2.start()
    assert len(ctx.notifications) == 1  # 起動し直しても知らせ直さない
    assert m2.new_count() == 2
    first = m2.new_items()[0]
    m2.ack(first.key)
    assert m2.new_count() == 1
    m2.ack_all()
    assert m2.new_count() == 0 and ctx.status == "見張っています"
    acks = [e for e in _events(ctx) if e["event"] == "ack"]
    assert [a["n"] for a in acks] == [1, 1]


def test_fr17_broken_record_rebaselines_without_notifying(make_module: Any) -> None:
    api = sample_api()
    ctx = _baseline(make_module, api)
    (ctx.data_dir / "known.json").write_text("{broken", encoding="utf-8")
    api.add_value("hkcu_run", SECRET_NAME, SECRET_CMD)
    m, _, _ = make_module(api, ctx)
    m.start()
    assert ctx.notifications == []
    assert m.banner == ("rebaseline", 3)
    assert _events(ctx)[-1]["event"] == "rebaseline"


# ---------------------------------------------------------------- §10 の値の扱い
def test_binary_values_and_empty_values_and_desktop_ini() -> None:
    api = sample_api()
    api.add_value("hkcu_run", "", "", 1)
    api.add_value("hkcu_run", "Bin", b"\x01\x02", 3)  # type: ignore[arg-type]
    api.add_value("hkcu_run", "Exp", r"%ProgramFiles%\E\e.exe", 2)
    folder = api.folders["startup"]
    api.files[folder].append(("sub", True))
    api.add_file(folder, "NoTarget.lnk")
    api.add_file(folder, "run.bat")
    res = sources.read_all(api, set())
    assert res is not None
    names = {i.name: i for i in res.locs["hkcu_run"].items}
    assert "" not in names
    assert names["Bin"].command == sources.BINARY_TEXT and names["Bin"].compare == b"\x01\x02"
    assert names["Exp"].command == r"%ProgramFiles%\E\e.exe"  # 書かれたまま
    f = {i.name: i for i in res.locs["startup_user"].items}
    assert set(f) == {"NoTarget.lnk", "run.bat"}
    assert f["NoTarget.lnk"].command == sources.LNK_UNKNOWN_TEXT and f["NoTarget.lnk"].compare == b""
    assert f["run.bat"].command.endswith("run.bat")


def test_folder_access_denied_is_unreadable() -> None:
    api = sample_api()
    api.folder_errors[api.folders["common_startup"]] = PermissionError(13, "denied")
    res = sources.read_all(api, set())
    assert res is not None and res.locs["startup_common"].status == "unreadable"


def test_read_all_cancelled_returns_none() -> None:
    assert sources.read_all(sample_api(), set(), cancelled=lambda: True) is None


def test_more_than_20_added_notifies_once(make_module: Any) -> None:
    api = sample_api()
    ctx = _baseline(make_module, api)
    for i in range(25):
        api.add_value("hkcu_run", f"Many{i}", rf"C:\M\m{i}.exe")
    m, _, _ = make_module(api, ctx)
    m.start()
    assert len(ctx.notifications) == 1 and "25 個" in ctx.notifications[0][1]
    assert len(m.new_items()) == 25


# ---------------------------------------------------------------- W-13・FR-13
def test_running_mark_compares_file_names(make_module: Any) -> None:
    api = sample_api()
    api.procs = {"od.exe", "securityhealthsample.exe"}
    m, _, _ = make_module(api)
    m.start()
    m.page_opened()
    run = {v.item.name: v.running for v in m.view_items()}
    assert run == {"OneDriveSample": True, "SecurityHealthSample": True}
    m.set_option("show_running", False)
    assert all(v.running is None for v in m.view_items())


def test_running_mark_not_read_when_disabled(make_module: Any) -> None:
    api = sample_api()
    m, _, _ = make_module(api, section={"show_running": False})
    m.start()
    m.page_opened()
    assert api.proc_reads == 0


# ---------------------------------------------------------------- FR-10・コピー
def test_open_actions_and_copy(make_module: Any, monkeypatch: Any) -> None:
    api = sample_api()
    m, _, opened = make_module(api)
    m.start()
    m.open_settings()
    m.open_taskmgr()
    folder_item = sources.StartupItem("startup_user", "x.lnk", r"C:\A\x.exe", False, False, b"", folder_path=r"C:\F")
    reg_item = sources.StartupItem("hkcu_run", "x", r"C:\A\x.exe", False, False, b"")
    m.open_folder(folder_item)
    m.open_folder(reg_item)
    assert opened == ["ms-settings:startupapps", "taskmgr.exe", r"C:\F"]
    monkeypatch.setenv("USERPROFILE", r"C:\Users\Taro")
    it = sources.StartupItem("hkcu_run", "x", r'"c:\users\taro\App\x.exe" /min C:\Users\Taro\y', False, False, b"")
    assert m.copy_text(it) == r'"%USERPROFILE%\App\x.exe" /min %USERPROFILE%\y'


# ---------------------------------------------------------------- FR-18 CLI
def test_fr18_cli_list_and_rescan(make_module: Any) -> None:
    api = sample_api()
    m, ctx, _ = make_module(api)
    m.start()
    rc, out = m.handle_cli(["list"])
    assert rc == 0
    assert out.splitlines()[0].split("\t")[0] == "hkcu_run" and "OneDriveSample" in out
    api.add_value("hkcu_run", "Cli", r"C:\C\c.exe")
    api.remove_value("hkcu_run", "OneDriveSample")
    assert m.handle_cli(["rescan"]) == (0, "増えた 1・変わった 0・消えた 1")
    assert len(ctx.notifications) == 1
    assert m.handle_cli(["bogus"])[0] == 2
    assert m.handle_cli([])[0] == 2


# ---------------------------------------------------------------- §9 設定
def test_settings_out_of_range_are_reset_and_written_back(make_module: Any) -> None:
    api = sample_api()
    m, ctx, _ = make_module(api, section={"poll_minutes": 1, "settle_ms": "x", "notify_runonce": 1, "show_running": True})
    assert m.cfg["poll_minutes"] == 15 and m.cfg["settle_ms"] == 2000 and m.cfg["notify_runonce"] is False
    assert ctx.writes and ctx.writes[0][0]["poll_minutes"] == 15
    text = ctx.handler.text()
    assert "settings_fixed key=poll_minutes" in text and "settings_fixed key=settle_ms" in text


def test_validate_accepts_bounds() -> None:
    sec, fixed = validate({"poll_minutes": 240, "settle_ms": 500, "notify_runonce": True, "show_running": False})
    assert fixed == [] and sec["poll_minutes"] == 240
    _sec, fixed = validate({"poll_minutes": True, "settle_ms": 10001})
    assert set(fixed) == {"poll_minutes", "settle_ms", "notify_runonce", "show_running"}


def test_poll_minutes_change_requests_restart(make_module: Any) -> None:
    api = sample_api()
    m, ctx, _ = make_module(api)
    m.set_option("poll_minutes", 30)
    assert ctx.writes[-1] == ({"notify_runonce": False, "show_running": True, "poll_minutes": 30, "settle_ms": 2000}, True)
    m.set_option("notify_runonce", True)
    assert ctx.writes[-1][1] is False


# ---------------------------------------------------------------- usage・diagnostics・トレイ
def test_usage_counts_notifications_and_opens(make_module: Any) -> None:
    api = sample_api()
    ctx = _baseline(make_module, api)
    api.add_value("hkcu_run", "U1", r"C:\U\u1.exe")
    m, _, _ = make_module(api, ctx)
    m.start()
    m.page_opened()
    m.page_opened()
    u = {s.key: s for s in m.usage(7)}
    assert u["notified"].per_day[-1] == 1 and u["notified"].primary and "R-3" in (u["notified"].hint or "")
    assert u["opened"].per_day[-1] == 2


def test_diagnostics_shape(make_module: Any) -> None:
    api = sample_api()
    m, ctx, _ = make_module(api)
    m.start()
    d = m.diagnostics()
    assert d["record"] is True and d["known"] == 2 and d["new"] == 0
    assert d["watch.hkcu_run"] == "notify" and d["count.hkcu_run"] == 1 and d["count.startup_user"] == 0
    assert isinstance(d["last_scan_ms"], int)
    assert all(isinstance(v, (str, int, bool)) for v in d.values())
    assert ctx.tray and ctx.tray[0][0] == "自動起動の一覧を見る"


def test_events_trimmed_to_2000(tmp_path: Path) -> None:
    from deskkit.modules.startupwatch.store import EventLog

    log = EventLog(tmp_path / "events.jsonl")
    for _ in range(2005):
        log.append("open", None, 0)
    assert len((tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()) == 2000
