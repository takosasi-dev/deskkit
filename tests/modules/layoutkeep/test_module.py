# モジュール全体(偽 ctx + 偽 Win32)のテスト: DPI 検査・保存・dry_run・抑止・layout.apply(wait_s / busy / sig_mismatch)・
# 構成変化の検知(デバウンス・settle・提案・自動適用)・oplog にタイトルが無いこと。
from __future__ import annotations

from pathlib import Path
from typing import Any

from deskkit.modules.layoutkeep.fake_win32 import fake_window
from deskkit.modules.layoutkeep.selftest import MAIL, Scenario
from deskkit.modules.layoutkeep.sysevents import PBT_APMRESUMEAUTOMATIC, WM_DISPLAYCHANGE, WM_POWERBROADCAST

from .fakes import GAME_FG, FakeCtx, make_module


def _section(sc: Scenario, **kw: Any) -> dict[str, Any]:
    d: dict[str, Any] = {"targets": [{"exe": t} for t in sc.targets]}
    d.update(kw)
    return d


def _started(tmp_path: Path, sc: Scenario, *, seed: bool = True, **kw: Any) -> tuple[FakeCtx, Any]:
    ctx, mod = make_module(tmp_path, sc.api, _section(sc, **kw))
    if seed:
        mod.store.save_layout(sc.layout)
    mod.start()
    return ctx, mod


def _oplog_text(mod: Any) -> str:
    p = mod.store.oplog_path
    return p.read_text(encoding="utf-8") if p.exists() else ""


def test_defaults_written_back(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = make_module(tmp_path, scenario.api, {})
    assert ctx.section["mode"] == "dry_run" and ctx.section["debounce_ms"] == 2500
    assert ctx.section["signature"]["id_source"] == "device_interface"
    assert ctx.section["hotkeys"] == {"save": "", "apply": ""}
    assert mod.cfg.max_wait_s == 20


def test_not_pmv2_stays_disabled(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = make_module(tmp_path, scenario.api, _section(scenario), awareness="system_aware")
    mod.start()
    assert mod.disabled_reason and ctx.notes and ctx.notes[0]["level"] == "warn"
    assert ctx.tray == [] and ctx.native == {} and "layout.apply" not in ctx.handlers
    assert mod.apply_current("tray") == "error"
    assert scenario.api.set_calls == []


def test_save_writes_layout_and_default_name(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario, seed=False)
    assert mod.save("tray")
    sig = mod.last_sig.signature
    lay = mod.store.load_layout(sig)
    # 対象9件のうち、除外(最小化・cloaked・自分自身)を除いた数
    assert lay is not None and len(lay.windows) == 7
    assert ctx.section["names"][sig] == f"構成-{sig[:6]}"
    text = _oplog_text(mod)
    assert '"action":"save"' in text and '"title' not in text
    assert "alpha doc" not in text


def test_save_without_targets(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = make_module(tmp_path, scenario.api, {})
    mod.start()
    assert not mod.save("tray")
    assert "targets" in ctx.notes[-1]["text"]


def test_dry_run_apply_logs_plan_and_never_sets(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario)
    assert mod.apply_current("hotkey") == "dry_run"
    assert scenario.api.set_calls == []
    assert '"action":"plan"' in _oplog_text(mod)
    assert "試運転" in ctx.notes[-1]["title"]


def test_live_apply_and_undo(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario, mode="live")
    assert mod.apply_current("tray") == "applied"
    moved = {h for h, _ in scenario.api.set_calls}
    assert moved and not moved & {401, 402}
    assert "戻しました" in ctx.notes[-1]["text"]
    assert mod.undo("tray") == "applied"
    assert '"action":"undo"' in _oplog_text(mod)


def test_live_apply_aborts_when_undo_readonly(tmp_path: Path, scenario: Scenario) -> None:
    import os
    import stat

    ctx, mod = _started(tmp_path, scenario, mode="live")
    undo = mod.store.undo_path
    undo.write_text("{}", encoding="utf-8")
    os.chmod(undo, stat.S_IREAD)
    try:
        assert mod.apply_current("tray") == "error"
        assert scenario.api.set_calls == []
        assert ctx.notes[-1]["level"] == "error"
        assert '"result":"undo_failed"' in _oplog_text(mod)
    finally:
        os.chmod(undo, stat.S_IWRITE | stat.S_IREAD)


def test_suppressed_when_game_foreground(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario, mode="live")
    ctx.fg = GAME_FG
    assert mod.apply_current("hotkey") == "suppressed"
    assert mod.undo("hotkey") == "suppressed"
    assert scenario.api.set_calls == []
    text = _oplog_text(mod)
    assert '"action":"suppressed"' in text and '"reason":"game"' in text


def test_no_layout_notice(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario, seed=False)
    assert mod.apply_current("tray") == "no_layout"
    assert ctx.notes[-1]["text"] == "この構成のレイアウトはありません"


# ---------------------------------------------------------------- layout.apply
def _apply_event(ctx: FakeCtx, **payload: Any) -> None:
    for h in ctx.handlers["layout.apply"]:
        h(payload)


def _replies(ctx: FakeCtx) -> list[dict[str, Any]]:
    return [p for e, p in ctx.emitted if e == "layout.applied"]


def test_event_dry_run_returns_request_id(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario)
    _apply_event(ctx, layout=None, source="modeshift", request_id="t1", wait_s=0)
    r = _replies(ctx)[-1]
    assert r["request_id"] == "t1" and r["result"] == "dry_run" and scenario.api.set_calls == []


def test_event_sig_mismatch_by_name(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario, mode="live", names={"aaaaaaaaaaaa": "自宅2画面"})
    _apply_event(ctx, layout="自宅2画面", request_id="t2")
    assert _replies(ctx)[-1]["result"] == "sig_mismatch"
    _apply_event(ctx, layout="存在しない名前", request_id="t3")
    assert _replies(ctx)[-1]["result"] == "no_layout"
    assert scenario.api.set_calls == []


def test_event_live_with_wait_and_busy(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario, mode="live", max_wait_s=5)
    _apply_event(ctx, layout=None, request_id="w1", wait_s=30)
    assert _replies(ctx) == []  # mail.exe(未起動)を待っている
    _apply_event(ctx, layout=None, request_id="w2")
    busy = _replies(ctx)[-1]
    assert (busy["request_id"], busy["result"], busy["reason"]) == ("w2", "error", "busy")
    # mail.exe が起動した
    scenario.api.windows.append(fake_window(1201, 120, MAIL, "MailWnd", "inbox", rect=(1, 1, 2, 2)))
    timer = ctx.timers[-1]
    timer.fire()
    r = _replies(ctx)[-1]
    assert r["request_id"] == "w1" and r["result"] == "applied"
    assert r["skipped"].get("not_running", 0) == 0
    assert 1201 in {h for h, _ in scenario.api.set_calls}
    assert not timer.active and mod._wait is None


def test_event_wait_is_bounded(tmp_path: Path, scenario: Scenario, monkeypatch: Any) -> None:
    import types

    import deskkit.modules.layoutkeep.module as m

    ctx, mod = _started(tmp_path, scenario, mode="live", max_wait_s=2)
    now = [1000.0]
    monkeypatch.setattr(m, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
    _apply_event(ctx, layout=None, request_id="w3", wait_s=60)
    assert mod._wait is not None and mod._wait.deadline == 1002.0  # max_wait_s で頭打ち
    now[0] = 1003.0
    ctx.timers[-1].fire()
    r = _replies(ctx)[-1]
    assert r["request_id"] == "w3" and r["skipped"].get("not_running") == 1


# ---------------------------------------------------------------- 構成変化
def test_display_change_debounce_settle_and_propose(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario)
    for h in ctx.native[WM_DISPLAYCHANGE]:
        h(0, 0)
    t = ctx.timers[-1]
    assert t.interval == 2500 and t.single_shot
    t.fire()  # 1回目の確認
    assert not any("配置を戻しますか" in n["title"] for n in ctx.notes)
    t.fire()  # 2回目で同じシグネチャ → 落ち着いた
    prop = [n for n in ctx.notes if "配置を戻しますか" in n["title"]]
    assert len(prop) == 1 and prop[0]["on_click"] is not None
    assert "(提案中)" in ctx.tray[1].text
    assert '"action":"propose"' in _oplog_text(mod)
    assert scenario.api.set_calls == []


def test_power_resume_filter(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario)
    for h in ctx.native[WM_POWERBROADCAST]:
        h(0x4, 0)  # PBT_APMSUSPEND は無視
    assert ctx.timers == []
    for h in ctx.native[WM_POWERBROADCAST]:
        h(PBT_APMRESUMEAUTOMATIC, 0)
    assert len(ctx.timers) == 1


def test_auto_apply_in_live(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario, mode="live", auto_apply=True, settle_checks=1)
    for h in ctx.native[WM_DISPLAYCHANGE]:
        h(0, 0)
    ctx.timers[-1].fire()
    assert scenario.api.set_calls
    assert '"source":"auto"' in _oplog_text(mod)
    assert not any("配置を戻しますか" in n["title"] for n in ctx.notes)


def test_proposal_dropped_when_game(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario, settle_checks=1)
    ctx.fg = GAME_FG
    for h in ctx.native[WM_DISPLAYCHANGE]:
        h(0, 0)
    ctx.timers[-1].fire()
    assert mod.proposal_sig is None
    assert not any("配置を戻しますか" in n["title"] for n in ctx.notes)
    assert '"action":"suppressed"' in _oplog_text(mod)


def test_hotkeys_registered_only_when_set(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario, hotkeys={"save": "Ctrl+Alt+S", "apply": ""})
    assert ctx.hotkeys.registered == {"save": "Ctrl+Alt+S"}
    assert "save" in ctx.hotkeys.callbacks


def test_cli(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario)
    code, text = mod.handle_cli(["status"])
    assert code == 0 and "mode=dry_run" in text
    assert mod.handle_cli(["plan"])[0] == 0
    assert mod.handle_cli(["bogus"])[0] == 2


def test_oplog_never_contains_titles(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario, mode="live")
    mod.apply_current("tray")
    mod.undo("tray")
    mod.save("tray")
    mod.preview_plan()
    text = _oplog_text(mod)
    assert text.count("\n") >= 4
    assert '"title' not in text
    for w in scenario.api.windows:
        if len(w.title) >= 6:
            assert w.title not in text
