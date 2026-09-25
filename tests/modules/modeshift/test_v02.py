# v0.2 の追加分: マイク・テーマのアクション(undo 含む)、layout_apply の preset、modeshift.reverted の送信、
# 電源(AC ⇄ バッテリー)の自動切替と一時停止中の抑止、diagnostics()。偽の OS 実装だけで確かめる(実機は変えない)。
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deskkit.modules.modeshift import cli
from deskkit.modules.modeshift.autoswitch import PBT_APMPOWERSTATUSCHANGE, WM_POWERBROADCAST, PowerSourceWatcher
from deskkit.modules.modeshift.config import AutoRule, normalize_action, validate_section
from deskkit.modules.modeshift.fakes import GUID_B, FakeCtx, FakeForeground, fake_system, populate, sample_section
from deskkit.modules.modeshift.module import ModeShiftModule
from deskkit.modules.modeshift.system import MasterState, ThemeState

from .conftest import Env

FOCUS = {"name": "focus", "label": "集中", "actions": [
    {"type": "mic_volume", "level": None, "mute": True},
    {"type": "theme", "apps": "dark", "system": None},
    {"type": "power_plan", "guid": GUID_B},
]}


def _add_focus(sec: dict[str, Any]) -> None:
    sec["modes"].append(dict(FOCUS))


@pytest.fixture
def fenv(tmp_path: Path) -> Env:
    return Env(tmp_path, confirm=True, edit=_add_focus)


def _reverted(e: Env) -> list[dict[str, Any]]:
    return [p for ev, p in e.ctx.emitted if ev == "modeshift.reverted"]


# ---------------------------------------------------------------- M2: マイク・テーマ
def test_mic_and_theme_execute_and_undo(fenv: Env) -> None:
    e = fenv
    code, text = cli.handle(e.svc, ["focus"])
    assert code == 0, text
    assert e.sys.audio.capture is not None and e.sys.audio.capture.mute is True
    assert abs(e.sys.audio.capture.level - 0.8) < 1e-9          # 音量は変えない指定
    assert e.sys.theme.state == ThemeState("dark", "dark") and e.sys.theme.broadcasts == 1
    snap = e.svc.snapshots.load()
    assert snap is not None
    assert snap["mic"]["before"] == {"level": 0.8, "mute": False} and snap["mic"]["written"]["mute"] is True
    assert snap["theme"]["before"] == {"apps": "light", "system": "dark"} and snap["theme"]["keys"] == ["apps"]
    code, text = cli.handle(e.svc, ["--undo"])
    assert code == 0, text
    assert e.sys.audio.capture is not None and e.sys.audio.capture.mute is False
    assert e.sys.theme.state == ThemeState("light", "dark")
    assert e.sys.theme.set_calls[-1] == ("light", None)            # 書いた項目(アプリ)だけ戻す


def test_mic_and_theme_manual_change_not_undone(fenv: Env) -> None:
    e = fenv
    cli.handle(e.svc, ["focus"])
    e.sys.audio.set_capture(None, False)                          # 手でミュート解除
    e.sys.theme.state = ThemeState("light", "dark")               # 手でライトに戻した
    code, text = cli.handle(e.svc, ["--undo", "--dry-run"])
    assert code == 0 and text.count("手動で変更済み") == 2
    calls = len(e.sys.theme.set_calls)
    cli.handle(e.svc, ["--undo"])
    assert len(e.sys.theme.set_calls) == calls and e.sys.audio.capture is not None and e.sys.audio.capture.mute is False


def test_mic_undo_skips_when_capture_device_changed(fenv: Env) -> None:
    e = fenv
    cli.handle(e.svc, ["focus"])
    c = e.sys.audio.capture
    assert c is not None
    e.sys.audio.capture = MasterState(c.level, c.mute, "{other-mic}")
    code, text = cli.handle(e.svc, ["--undo"])
    assert "既定の録音デバイスが変わった" in text
    assert e.sys.audio.capture.mute is True


def test_theme_is_skipped_in_game_but_mic_runs(fenv: Env) -> None:
    e = fenv
    e.ctx.fg = FakeForeground(exe="game.exe", is_game=True, is_fullscreen=True)
    e.svc.switch_mode("focus", dry_run=False, source="hotkey")
    rows = {r["type"]: r for r in e.ops()}
    assert rows["theme"]["result"] == "skipped" and rows["theme"]["reason"] == "ゲーム中"
    assert rows["mic_volume"]["result"] == "ok" and not e.sys.theme.set_calls
    # 本番でテーマを変えたあと、ゲーム中の undo ではテーマだけ戻さない
    e.ctx.fg = FakeForeground()
    cli.handle(e.svc, ["study"])
    cli.handle(e.svc, ["focus"])
    assert e.sys.theme.state.apps == "dark"
    e.ctx.fg = FakeForeground(exe="game.exe", is_game=True)
    code, text = cli.handle(e.svc, ["--undo"])
    assert "ゲーム中" in text and e.sys.theme.state.apps == "dark"


def test_dry_run_shows_new_types_and_changes_nothing(fenv: Env) -> None:
    e = fenv
    code, text = cli.handle(e.svc, ["focus", "--dry-run"])
    assert code == 0
    rows = [ln.split("\t") for ln in text.splitlines() if ln[:1].isdigit()]
    assert [r[1] for r in rows] == ["マイク", "アプリのテーマ", "電源プラン"]
    assert rows[0][3] == "80%・ミュートなし" and rows[0][4] == "音量は変えない・ミュート"
    assert rows[1][3] == "アプリ: ライト・Windows: ダーク" and rows[1][4] == "アプリ: ダーク"
    assert not e.sys.audio.capture_calls and not e.sys.theme.set_calls


def test_new_types_validation() -> None:
    assert normalize_action({"type": "mic_volume", "level": 0.4})[1] == []
    assert normalize_action({"type": "mic_volume"})[1]                       # level も mute も無い
    assert normalize_action({"type": "mic_volume", "level": 2})[1]
    assert normalize_action({"type": "theme", "apps": "light"})[1] == []
    assert normalize_action({"type": "theme"})[1]
    assert normalize_action({"type": "theme", "apps": "black"})[1]
    norm, errs = normalize_action({"type": "theme", "system": "dark"})
    assert errs == [] and norm == {"type": "theme", "apps": None, "system": "dark"}


def test_undo_without_theme_before_value_does_not_touch_theme(fenv: Env) -> None:
    e = fenv
    e.sys.theme.state = ThemeState(None, None)      # 値が無い(読めない)
    cli.handle(e.svc, ["focus"])
    calls = len(e.sys.theme.set_calls)
    code, text = cli.handle(e.svc, ["--undo"])
    assert code == 0 and len(e.sys.theme.set_calls) == calls


# ---------------------------------------------------------------- L1: preset
def test_layout_preset_payload(tmp_path: Path) -> None:
    def edit(s: dict[str, Any]) -> None:
        s["modes"][0]["actions"][7]["preset"] = "作業"

    e = Env(tmp_path, confirm=True, edit=edit)
    got: list[dict[str, Any]] = []
    e.ctx.on("layout.apply", lambda p: got.append(dict(p)))
    assert cli.handle(e.svc, ["game"])[0] == 0
    assert got[-1]["preset"] == "作業" and got[-1]["layout"] == "game" and got[-1]["source"] == "modeshift"
    code, text = cli.handle(e.svc, ["game", "--dry-run"])
    assert "プリセット「作業」" in text


def test_layout_without_preset_keeps_hash_and_payload_shape(env: Env) -> None:
    norm, errs = normalize_action({"type": "layout_apply", "layout": "game", "wait_s": 3})
    assert errs == [] and "preset" not in norm                   # 既存の定義ハッシュを変えない
    assert norm == {"type": "layout_apply", "layout": "game", "wait_s": 3.0}
    assert normalize_action({"type": "layout_apply", "preset": "  "})[0].get("preset") is None
    assert normalize_action({"type": "layout_apply", "preset": 3})[1]
    got: list[dict[str, Any]] = []
    env.ctx.on("layout.apply", lambda p: got.append(dict(p)))
    cli.handle(env.svc, ["game"])
    assert set(got[-1]) == {"layout", "source", "request_id", "wait_s"}


# ---------------------------------------------------------------- M3: modeshift.reverted
def test_reverted_emitted_after_cli_undo_only_when_real(env: Env) -> None:
    cli.handle(env.svc, ["game"])
    cli.handle(env.svc, ["--undo", "--dry-run"])
    assert _reverted(env) == []
    cli.handle(env.svc, ["--undo"])
    rows = [r for r in env.ops() if r["source"] == "undo"]
    assert _reverted(env) == [{"mode": "game", "run_id": rows[-1]["run_id"]}]
    assert cli.handle(env.svc, ["--undo"])[0] == 7 and len(_reverted(env)) == 1


def test_reverted_emitted_after_gui_undo(env: Env) -> None:
    cli.handle(env.svc, ["study"])
    env.svc.request_undo(dry_run=False, source="gui")
    assert [p["mode"] for p in _reverted(env)] == ["study"]


# ---------------------------------------------------------------- M1: 電源のきっかけ
def test_power_rules_config() -> None:
    sec: dict[str, Any] = {"modes": [{"name": "eco", "actions": [{"type": "power_plan", "guid": GUID_B}]}],
                           "auto_switch": {"enabled": True, "poll_interval_s": None, "rules": [
                               {"trigger": "on_battery", "mode": "eco", "on_exit": "undo"},
                               {"trigger": "on_battery", "mode": "eco"},
                               {"trigger": "on_lid", "mode": "eco"},
                               {"trigger": "on_ac", "mode": "nope"},
                           ]}}
    cfg = validate_section(sec)
    assert [r.error is None for r in cfg.rules] == [True, False, False, False]
    assert cfg.rules[0].trigger == "on_battery" and cfg.rules[0].is_power and cfg.rules[0].exe == ""
    assert cfg.auto_error is None and cfg.auto_power_active and not cfg.auto_poll_active and cfg.auto_active
    # exe のルールがあって間隔が未設定 → exe 側だけ設定エラー(電源は動く)
    sec["auto_switch"]["rules"].append({"exe": "game.exe", "mode": "eco"})
    cfg = validate_section(sec)
    assert cfg.auto_error is not None and cfg.auto_power_active and not cfg.auto_poll_active
    # 無効なら何も動かない
    sec["auto_switch"]["enabled"] = False
    assert not validate_section(sec).auto_active
    # 従来(ルールなし・間隔なしで有効)は従来どおり設定エラー
    assert validate_section({"modes": [], "auto_switch": {"enabled": True, "poll_interval_s": None, "rules": []}}).auto_error


def test_power_watcher_edges() -> None:
    state: dict[str, bool | None] = {"ac": True}
    appeared: list[str] = []
    vanished: list[str] = []
    rules = [AutoRule("", "eco", "undo", trigger="on_battery"), AutoRule("game.exe", "game", "undo")]
    w = PowerSourceWatcher(lambda: state["ac"], rules, lambda r: appeared.append(r.mode), lambda r: vanished.append(r.mode))
    w.check()                                       # 変化なし・起動時の状態はきっかけにしない
    w.handle(0x0004, 0)                             # 電源と関係の無い通知(PBT_APMSUSPEND)は無視
    state["ac"] = False
    w.handle(0x0004, 0)
    assert appeared == [] and vanished == []
    w.handle(PBT_APMPOWERSTATUSCHANGE, 0)
    assert appeared == ["eco"]
    state["ac"] = None                              # 不明は無視(直前の状態を保つ)
    w.check()
    state["ac"] = True
    w.check()
    assert vanished == ["eco"]
    w.check()
    assert appeared == ["eco"] and vanished == ["eco"]


def test_power_watcher_entering_rule_wins_over_undo() -> None:
    state = {"ac": True}
    calls: list[str] = []
    rules = [AutoRule("", "eco", "undo", trigger="on_battery"), AutoRule("", "perf", "undo", trigger="on_ac")]
    w = PowerSourceWatcher(lambda: state["ac"], rules, lambda r: calls.append("apply " + r.mode),
                           lambda r: calls.append("undo " + r.mode))
    state["ac"] = False
    w.check()
    state["ac"] = True
    w.check()
    assert calls == ["apply eco", "apply perf"]     # 入る側の適用を優先し、1段の undo を続けて動かさない


def _power_module(tmp_path: Path, *, confirm: bool = True) -> tuple[ModeShiftModule, FakeCtx, Any]:
    sysm = fake_system()
    populate(sysm)
    sec = sample_section(tmp_path)
    sec["auto_switch"] = {"enabled": True, "poll_interval_s": None,
                          "rules": [{"trigger": "on_battery", "mode": "study", "on_exit": "undo"}]}
    if confirm:
        for m, md in zip(sec["modes"], validate_section(sec, {"game.exe"}).modes, strict=True):
            m["confirmed_hash"] = md.def_hash
    ctx = FakeCtx(tmp_path / "d", sec, games=frozenset({"game.exe"}))
    mod = ModeShiftModule(ctx, backends=sysm.backends())
    mod.start()
    assert mod.service is not None
    mod.service.worker.threaded = False
    return mod, ctx, sysm


def test_power_trigger_switches_and_undoes(tmp_path: Path) -> None:
    mod, ctx, sysm = _power_module(tmp_path)
    assert WM_POWERBROADCAST in ctx.native
    assert sysm.audio.master is not None and abs(sysm.audio.master.level - 0.62) < 1e-9
    sysm.power.ac = False
    ctx.fire_native(WM_POWERBROADCAST, PBT_APMPOWERSTATUSCHANGE)
    assert sysm.audio.master is not None and abs(sysm.audio.master.level - 0.1) < 1e-9   # study: マスター 10%
    assert mod.service is not None and mod.service.state.data["current_mode"] == "study"
    rows = [r for r in (ctx.data_dir / "ops.jsonl").read_text(encoding="utf-8").splitlines() if r]
    assert '"source":"auto"' in rows[-1]
    sysm.power.ac = True
    ctx.fire_native(WM_POWERBROADCAST, PBT_APMPOWERSTATUSCHANGE)
    assert abs(sysm.audio.master.level - 0.62) < 1e-9
    assert [p["mode"] for ev, p in ctx.emitted if ev == "modeshift.reverted"] == ["study"]
    mod.stop()


def test_power_trigger_respects_snooze(tmp_path: Path) -> None:
    mod, ctx, sysm = _power_module(tmp_path)
    ctx.snoozed = True
    sysm.power.ac = False
    ctx.fire_native(WM_POWERBROADCAST, PBT_APMPOWERSTATUSCHANGE)
    assert not sysm.audio.master_calls and not (ctx.data_dir / "ops.jsonl").exists()
    # 手で押した操作は止めない
    assert mod.service is not None
    assert cli.handle(mod.service, ["study"])[0] == 0 and sysm.audio.master_calls
    # 一時停止中は自動の「元に戻す」もしない
    ctx.snoozed = True
    sysm.power.ac = True
    ctx.fire_native(WM_POWERBROADCAST, PBT_APMPOWERSTATUSCHANGE)
    assert mod.service.undo_info() is not None
    mod.stop()


def test_power_trigger_unconfirmed_only_notifies(tmp_path: Path) -> None:
    mod, ctx, sysm = _power_module(tmp_path, confirm=False)
    sysm.power.ac = False
    ctx.fire_native(WM_POWERBROADCAST, PBT_APMPOWERSTATUSCHANGE)
    assert not sysm.audio.master_calls
    assert any("未確認" in n[1] for n in ctx.notifications)
    mod.stop()


def test_power_trigger_in_game_skips_focus_steps(tmp_path: Path) -> None:
    mod, ctx, sysm = _power_module(tmp_path)
    sec = ctx.settings_dict()
    sec["auto_switch"]["rules"] = [{"trigger": "on_battery", "mode": "game", "on_exit": "none"}]
    mod.save_section(sec)
    ctx.fg = FakeForeground(exe="game.exe", is_game=True, is_fullscreen=True)
    sysm.power.ac = False
    ctx.fire_native(WM_POWERBROADCAST, PBT_APMPOWERSTATUSCHANGE)
    assert sysm.power.active == GUID_B and not sysm.launcher.launched and not sysm.opener.opened
    mod.stop()


def test_exe_trigger_respects_snooze(tmp_path: Path) -> None:
    mod, ctx, sysm = _power_module(tmp_path)
    ctx.snoozed = True
    mod.auto_appear(AutoRule("x.exe", "study", "undo"))
    assert not sysm.audio.master_calls
    ctx.snoozed = False
    mod.auto_appear(AutoRule("x.exe", "study", "undo"))
    assert sysm.audio.master_calls
    mod.stop()


# ---------------------------------------------------------------- diagnostics
def test_diagnostics_has_counts_only(tmp_path: Path) -> None:
    mod, ctx, sysm = _power_module(tmp_path)
    assert mod.service is not None
    cli.handle(mod.service, ["game"])
    d = mod.diagnostics()
    assert d["running"] is True and d["modes"] == 5 and d["modes_valid"] == 2 and d["modes_unconfirmed"] == 0
    assert d["current_mode"] == "game" and d["auto_switch_enabled"] is True and d["auto_switch_active"] is True
    assert d["auto_rules_power"] == 1 and d["auto_rules_exe"] == 0 and d["last_result"] == "ok" and d["undo_available"] is True
    assert all(isinstance(v, str | int | bool) for v in d.values())
    text = " ".join(str(v) for v in d.values())
    for bad in (".exe", "\\", "http", "example.com", str(tmp_path)):
        assert bad not in text
    mod.stop()
    assert isinstance(mod.diagnostics(), dict)          # 停止後も例外にならない


def test_fake_ctx_is_snoozed_default_false(tmp_path: Path) -> None:
    ctx = FakeCtx(tmp_path, {})
    assert ctx.is_snoozed() is False


# ---------------------------------------------------------------- 再起動後の状態の知らせ(restored)
def _restart(tmp_path: Path, sysm: Any, ctx: FakeCtx) -> tuple[ModeShiftModule, FakeCtx]:
    ctx2 = FakeCtx(tmp_path / "d", ctx.settings_dict(), games=frozenset({"game.exe"}))
    mod2 = ModeShiftModule(ctx2, backends=sysm.backends())
    mod2.start()
    assert not [e for e, _p in ctx2.emitted if e == "modeshift.switched"]   # call_soon で後から送る
    ctx2.pump()
    return mod2, ctx2


def test_restored_switched_emitted_once_after_restart(tmp_path: Path) -> None:
    mod, ctx, sysm = _power_module(tmp_path)
    assert mod.service is not None
    cli.handle(mod.service, ["study"])
    run_id = mod.service.state.data["last_run_id"]
    mod.stop()
    ops_before = (tmp_path / "d" / "ops.jsonl").read_text(encoding="utf-8")
    mod2, ctx2 = _restart(tmp_path, sysm, ctx)
    sw = [p for e, p in ctx2.emitted if e == "modeshift.switched"]
    assert sw == [{"mode": "study", "run_id": run_id, "failed": 0, "restored": True}]
    assert (tmp_path / "d" / "ops.jsonl").read_text(encoding="utf-8") == ops_before     # ops に書かない
    assert len(sysm.audio.master_calls) == 1                                          # アクションを実行しない
    assert not any("切り替え" in n[1] for n in ctx2.notifications)
    mod2.stop()


def test_restored_not_emitted_after_undo_or_when_mode_removed(tmp_path: Path) -> None:
    mod, ctx, sysm = _power_module(tmp_path)
    assert mod.service is not None
    cli.handle(mod.service, ["study"])
    cli.handle(mod.service, ["--undo"])
    mod.stop()
    mod2, ctx2 = _restart(tmp_path, sysm, ctx)
    assert not [e for e, _p in ctx2.emitted if e == "modeshift.switched"]
    assert mod2.service is not None
    cli.handle(mod2.service, ["study"])
    mod2.stop()
    sec = ctx2.settings_dict()
    sec["modes"] = [m for m in sec["modes"] if m.get("name") != "study"]
    sec["auto_switch"]["rules"] = []
    ctx2.write_settings(sec)
    mod3, ctx3 = _restart(tmp_path, sysm, ctx2)
    assert not [e for e, _p in ctx3.emitted if e == "modeshift.switched"]
    mod3.stop()
