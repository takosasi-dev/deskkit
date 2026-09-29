# EyeBreak の受け入れ基準(AC-1〜AC-14)と FR-1・FR-8・FR-14・FR-17〜FR-22・§9・§10・契約 §2.2(replace_key)。偽の時計・偽の入力・偽の前面で。
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from deskkit.modules.eyebreak import _win32
from deskkit.modules.eyebreak.config import EyeBreakSettingsError, parse
from deskkit.modules.eyebreak.module import QUICK_MUTE, QUICK_RESUME, REPLACE_KEY, WM_POWERBROADCAST

from .conftest import SECRET_EXE, SECRET_LABEL, SECRET_MODE, FakeCtx, OldNotifyCtx


# ---------------------------------------------------------------- AC-1・FR-3・FR-4
def test_ac1_eye_prompt_after_20_minutes(make: Any) -> None:
    h = make()
    h.run(19 * 60 + 55)
    assert h.ctx.notifications == []
    h.run(5)
    assert len(h.ctx.notifications) == 1
    n = h.ctx.notifications[0]
    assert n["title"] == "目を休めませんか" and n["level"] == "info"
    assert n["text"] == "20分つづけて画面を見ています。20秒ほど、窓の外など遠くを見てみましょう。"
    assert n["replace_key"] == REPLACE_KEY
    assert json.loads((h.ctx.data_dir / "daily.json").read_text(encoding="utf-8"))["2026-09-28"]["eye"] == 1


# ---------------------------------------------------------------- AC-2・E-4
def test_ac2_body_at_60_replaces_eye_then_eye_20_later(make: Any) -> None:
    h = make()
    h.run(60 * 60)
    assert h.titles() == ["目を休めませんか", "目を休めませんか", "ひと休みしませんか"]
    assert h.ctx.notifications[-1]["text"].startswith("1時間つづけて使っています。")
    h.run(19 * 60 + 55)
    assert len(h.ctx.notifications) == 3
    h.run(5)
    assert h.titles()[-1] == "目を休めませんか"


# ---------------------------------------------------------------- AC-3・FR-2
def test_ac3_five_minutes_idle_resets(make: Any) -> None:
    h = make()
    h.run(15 * 60)
    h.run(5 * 60, active=False)
    assert h.ctx.notifications == []
    h.run(20 * 60 - 5)
    assert h.ctx.notifications == []
    h.run(5)
    assert len(h.ctx.notifications) == 1
    assert "break" in h.ctx.handler.text()


# ---------------------------------------------------------------- AC-4・FR-9
def test_ac4_game_holds_then_after_game_body_once(make: Any) -> None:
    h = make()
    h.ctx.game = True
    h.run(120 * 60)
    assert h.ctx.notifications == []
    assert h.ctx.status == "ゲームのあとで出します" or h.m.status_text() == "ゲームのあとで出します"
    h.ctx.game = False
    h.run(25)
    assert h.ctx.notifications == []
    h.run(5)
    assert len(h.ctx.notifications) == 1
    n = h.ctx.notifications[0]
    assert n["title"] == "おつかれさまでした"
    assert n["text"] == "ゲームを含めて2時間つづけて使っています。少し立って、ひと息つきませんか。"
    h.run(60)
    assert len(h.ctx.notifications) == 1


def test_ac4_back_to_game_within_29s_does_not_show(make: Any) -> None:
    h = make()
    h.ctx.game = True
    h.run(120 * 60)
    h.ctx.game = False
    h.run(25)
    h.ctx.game = True  # 29 秒目にゲームへ戻った(次の読み取りでゲーム)
    h.run(5)
    assert h.ctx.notifications == []
    h.ctx.game = False
    h.run(25)
    assert h.ctx.notifications == []
    h.run(5)
    assert len(h.ctx.notifications) == 1


def test_fullscreen_true_holds_like_game(make: Any) -> None:
    h = make()
    h.ctx.fullscreen = True
    h.run(20 * 60)
    assert h.ctx.notifications == []
    h.ctx.fullscreen = False
    h.run(30)
    assert h.titles() == ["おつかれさまでした"]
    assert h.ctx.notifications[0]["text"].startswith("ゲームを含めて20分つづけて画面を見ています。")


def test_foreground_is_read_only_when_needed(make: Any) -> None:
    h = make()
    h.run(19 * 60)
    assert h.ctx.fg_reads == 0  # "after" では声かけを出す直前だけ読む
    h.run(60)
    assert h.ctx.fg_reads == 1


# ---------------------------------------------------------------- AC-5・FR-10
def test_ac5_pause_mode_stops_counting_during_game(make: Any) -> None:
    h = make({"game": "pause"})
    h.run(10 * 60)
    h.ctx.game = True
    h.run(30 * 60)
    assert h.ctx.notifications == []
    assert int(h.m.engine.tracker.eye_s) == 10 * 60
    h.ctx.game = False
    h.run(10 * 60)
    assert h.titles() == ["目を休めませんか"]


# ---------------------------------------------------------------- AC-6・E-7
def test_ac6_fullscreen_none_is_not_fullscreen(make: Any) -> None:
    h = make()
    h.ctx.fullscreen = None
    h.run(20 * 60)
    assert h.titles() == ["目を休めませんか"]


# ---------------------------------------------------------------- AC-7・FR-11
def test_ac7_snooze_stops_and_resume_restarts_from_zero(make: Any) -> None:
    h = make()
    h.run(10 * 60)
    h.ctx.snoozed = True
    h.ctx.emit_event("host.snooze_changed", {"snoozed": True, "until": None})
    h.run(120 * 60)
    assert h.ctx.notifications == []
    assert h.m.status_text() == "一時停止中"
    h.ctx.snoozed = False
    h.ctx.emit_event("host.snooze_changed", {"snoozed": False, "until": None})
    h.run(20 * 60 - 5)
    assert h.ctx.notifications == []
    h.run(5)
    assert h.titles() == ["目を休めませんか"]


def test_snooze_without_event_is_noticed_by_tick(make: Any) -> None:
    h = make()
    h.run(15 * 60)
    h.ctx.snoozed = True
    h.run(60)
    h.ctx.snoozed = False
    h.run(19 * 60)
    assert h.ctx.notifications == []  # 解除で 0 から数え直した
    h.run(60)
    assert len(h.ctx.notifications) == 1


# ---------------------------------------------------------------- AC-8・FR-12・E-9
def test_ac8_quiet_mode_holds_and_other_mode_releases_after_30s(make: Any) -> None:
    h = make({"quiet_modes": [SECRET_MODE]})
    h.ctx.emit_event("modeshift.switched", {"mode": SECRET_MODE, "run_id": "r1", "failed": 0})
    h.run(20 * 60)
    assert h.ctx.notifications == []
    assert h.m.status_text() == "静かにするモードの間は待っています"
    h.ctx.emit_event("modeshift.switched", {"mode": "work", "run_id": "r2", "failed": 0})
    h.run(25)
    assert h.ctx.notifications == []
    h.run(5)
    assert h.titles() == ["目を休めませんか"]
    assert h.ctx.notifications[0]["text"].startswith("20分")


def test_quiet_mode_reverted_releases(make: Any) -> None:
    h = make({"quiet_modes": [SECRET_MODE]})
    h.ctx.emit_event("modeshift.switched", {"mode": SECRET_MODE, "run_id": "r1", "failed": 0, "restored": True})
    h.run(20 * 60)
    h.ctx.emit_event("modeshift.reverted", {"mode": SECRET_MODE, "run_id": "r3"})
    h.run(30)
    assert len(h.ctx.notifications) == 1


def test_quiet_mode_name_not_in_modeshift_is_ignored(make: Any) -> None:
    h = make({"quiet_modes": ["gone"]})
    h.ctx.emit_event("modeshift.switched", {"mode": "gone", "run_id": "r1", "failed": 0})
    h.run(20 * 60)
    assert len(h.ctx.notifications) == 1


def test_start_is_not_quiet(make: Any) -> None:
    h = make({"quiet_modes": [SECRET_MODE]})  # E-9: 起動時は「静かにするモードではない」
    h.run(20 * 60)
    assert len(h.ctx.notifications) == 1


# ---------------------------------------------------------------- AC-9・FR-6
def test_ac9_later_prompts_again_after_10_minutes(make: Any) -> None:
    h = make()
    h.run(20 * 60)
    h.m.later("eye", None)  # 窓の「10分あとで」と同じ
    h.run(10 * 60 - 5)
    assert len(h.ctx.notifications) == 1
    h.run(5)
    assert h.titles() == ["目を休めませんか", "目を休めませんか"]


def test_ac9_later_cancelled_by_break(make: Any) -> None:
    h = make()
    h.run(20 * 60)
    h.m.later("eye", None)
    h.run(5 * 60, active=False)
    h.run(5 * 60)
    assert len(h.ctx.notifications) == 1


def test_later_during_game_is_held(make: Any) -> None:
    h = make({"game": "pause"})
    h.m.later("body", 1)
    h.ctx.game = True
    h.run(120)
    assert h.ctx.notifications == []
    h.ctx.game = False
    h.run(30)
    assert h.titles() == ["ひと休みしませんか"]  # pause のときは「ゲームのあと」ではない


# ---------------------------------------------------------------- AC-10・FR-7
def test_ac10_mute_until_4am_survives_recreate(make: Any) -> None:
    clock = [0.0]
    base = datetime(2026, 9, 28, 23, 50, 0)
    h = make(base=base, clock=clock)
    until = h.m.mute_today()
    assert until == datetime(2026, 9, 29, 4, 0, 0)
    assert h.m.until_text(until) == "明日の朝4時"
    h.m.stop()
    h2 = make(ctx=h.ctx, base=base, clock=clock)
    assert h2.m.muted()
    assert h2.m.status_text() == "今日は止めています"
    h2.run(4 * 3600 + 9 * 60 - 5)  # 翌 3:58:55 まで
    assert h2.ctx.notifications == []
    assert "prompt kind=body result=muted" in h2.ctx.handler.text()
    h2.run(5 * 60)  # 4:00 を過ぎた
    assert not h2.m.muted()
    assert json.loads((h2.ctx.data_dir / "state.json").read_text(encoding="utf-8")) == {"muted_until": None}
    h2.run(20 * 60)
    assert len(h2.ctx.notifications) >= 1


def test_mute_counts_and_resume_restarts(make: Any) -> None:
    h = make()
    h.run(15 * 60)
    h.m.mute_today()
    h.m.resume()
    assert not h.m.muted() and h.m.engine.tracker.eye_s == 0
    h.run(20 * 60)
    assert len(h.ctx.notifications) == 1
    d = h.m.today_counts()
    assert d["mute_today"] == 1
    assert "mute" in h.ctx.handler.text() and "resume" in h.ctx.handler.text()


# ---------------------------------------------------------------- AC-11・E-5・FR-13
@pytest.mark.parametrize(("idle_s", "rested"), [(25, 1), (15, 0)])
def test_ac11_rest_detection_after_eye_prompt(make: Any, idle_s: int, rested: int) -> None:
    h = make()
    h.run(20 * 60)
    h.run(idle_s, active=False)
    h.run(60)
    assert h.m.today_counts()["rested"] == rested


def test_body_rest_needs_break_idle_within_15_minutes(make: Any) -> None:
    h = make({"eye": {"enabled": False}})
    h.run(60 * 60)
    h.run(3 * 60)
    h.run(5 * 60, active=False)
    assert h.m.today_counts()["rested"] == 1
    assert "rested kind=body" in h.ctx.handler.text()


# ---------------------------------------------------------------- AC-12・FR-15・FR-20
def test_ac12_dry_run_never_notifies(make: Any) -> None:
    h = make({"mode": "dry_run"})
    h.run(3 * 3600)
    assert h.ctx.notifications == []
    c = h.m.today_counts()
    assert c["dry"] > 0 and c["eye"] == 0 and c["body"] == 0
    assert h.m.usage(7)[0].per_day[-1] == 0
    assert h.m.status_text() == "様子見中"


# ---------------------------------------------------------------- AC-13・E-1
def test_ac13_idle_wraps_32bit() -> None:
    assert _win32.idle_ms(0xFFFFFF00, 0x00000100) == 512
    assert _win32.idle_ms(0x00000200, 0x00000100) == 0
    assert _win32.idle_ms(1000, 6000) == 5000


# ---------------------------------------------------------------- AC-14・INV-4
def test_ac14_no_exe_or_mode_names_in_files_logs_usage_diagnostics(make: Any) -> None:
    h = make({"quiet_modes": [SECRET_MODE]})
    h.ctx.game = True
    h.run(30 * 60)
    h.ctx.game = False
    h.run(60)
    h.ctx.emit_event("modeshift.switched", {"mode": SECRET_MODE, "run_id": "r1", "failed": 0})
    h.run(30 * 60)
    h.m.mute_today()
    h.m.resume()
    blob = "".join(p.read_text(encoding="utf-8") for p in h.ctx.data_dir.iterdir() if p.is_file())
    blob += h.ctx.handler.text() + repr(h.m.usage(30)) + json.dumps(h.m.diagnostics(), ensure_ascii=False)
    blob += h.m.status_text()
    for s in (SECRET_EXE, SECRET_MODE, SECRET_LABEL, "zzsecret"):
        assert s.lower() not in blob.lower()


# ---------------------------------------------------------------- FR-1・FR-19
def test_fr1_three_failures_mark_unavailable_and_log_once(make: Any) -> None:
    h = make()
    h.api.fail = True
    h.run(10)
    assert not h.m.engine.unavailable
    h.run(5)
    assert h.m.engine.unavailable
    assert h.ctx.status == "この PC では使っている時間を読めません"
    h.run(60)
    assert h.ctx.handler.text().count("idle_unavailable") == 1
    assert h.m.handle_cli(["status"])[0] == 20
    h.api.fail = False
    h.run(5)
    assert not h.m.engine.unavailable
    assert h.m.handle_cli(["status"])[0] == 0


def test_failed_reads_are_not_counted(make: Any) -> None:
    h = make()
    h.run(10 * 60)
    h.api.fail = True
    h.run(10 * 60)
    h.api.fail = False
    h.run(10 * 60 - 5)
    assert h.ctx.notifications == []
    h.run(5)
    assert len(h.ctx.notifications) == 1


def test_fr19_cli(make: Any) -> None:
    h = make()
    h.run(12 * 60)
    assert h.m.handle_cli(["status"]) == (0, "目 12分・体 12分")
    assert h.m.handle_cli(["later"]) == (0, "10分あとに、もう一度声をかけます")
    assert h.m.handle_cli(["later", "5"])[0] == 0
    assert h.m.handle_cli(["later", "0"])[0] == 1
    assert h.m.handle_cli(["later", "x"])[0] == 1
    assert h.m.handle_cli(["later", "61"])[0] == 1
    rc, msg = h.m.handle_cli(["mute-today"])
    assert rc == 0 and msg.endswith("まで出しません")
    assert h.m.handle_cli(["status"]) == (0, "今日は止めています")
    assert h.m.handle_cli(["resume"])[0] == 0
    assert h.m.handle_cli(["bogus"])[0] == 1
    assert h.m.handle_cli([])[0] == 1


# ---------------------------------------------------------------- FR-14
def test_fr14_power_broadcast_is_a_break(make: Any) -> None:
    h = make()
    h.run(19 * 60)
    for fn in h.ctx.natives[WM_POWERBROADCAST]:
        fn(7, 0)
    h.run(19 * 60)
    assert h.ctx.notifications == []
    h.run(60)
    assert len(h.ctx.notifications) == 1


# ---------------------------------------------------------------- FR-17・FR-18
def test_fr17_tray_and_fr18_quick_actions(make: Any) -> None:
    h = make()
    assert set(h.ctx.tray) == {"10分あとで", "今日はもう出さない", "再開する"}
    q = {x.label: x for x in h.ctx.quick}
    assert set(q) == {QUICK_MUTE, QUICK_RESUME}
    assert q[QUICK_MUTE].enabled() and not q[QUICK_RESUME].enabled()
    h.ctx.tray["今日はもう出さない"].cb()
    assert not q[QUICK_MUTE].enabled() and q[QUICK_RESUME].enabled()
    q[QUICK_RESUME].callback()
    assert not h.m.muted()
    assert {ms for ms, _cb in h.ctx.timers} == {5000, 60_000}


# ---------------------------------------------------------------- 契約 §2.2: replace_key を受け付けない本体
def test_replace_key_fallback_once_then_remembered(make: Any) -> None:
    h = make(ctx_cls=OldNotifyCtx)
    h.run(60 * 60)
    ctx: Any = h.ctx
    assert len(ctx.notifications) == 3
    assert ctx.type_errors == 1
    assert h.m.diagnostics()["replace_key"] is False
    assert h.ctx.handler.text().count("notify replace_key unsupported") == 1


# ---------------------------------------------------------------- §9 設定・§10
def test_settings_missing_keys_are_filled_and_written(tmp_path: Any) -> None:
    sec, cfg, filled = parse({"enabled": True, "eye": {"interval_min": 30}})
    assert filled and sec["eye"] == {"interval_min": 30, "enabled": True} and cfg.eye_min == 30
    assert sec["mode"] == "dry_run" and "enabled" not in sec


@pytest.mark.parametrize("bad", [
    {"eye": {"enabled": True, "interval_min": 4}},
    {"body": {"enabled": True, "interval_min": 241}},
    {"break_idle_min": 0}, {"later_min": 61}, {"after_game_delay_s": -1}, {"day_start_hour": 24},
    {"mode": "on"}, {"game": "stop"}, {"quiet_modes": "a"}, {"eye": {"enabled": "yes"}}, {"eye": 5},
    {"later_min": True},
])
def test_settings_out_of_range_raise(bad: dict[str, Any]) -> None:
    with pytest.raises(EyeBreakSettingsError):
        parse(bad)


def test_module_create_raises_on_bad_section(tmp_path: Any) -> None:
    from deskkit.modules.eyebreak.module import EyeBreakModule

    with pytest.raises(EyeBreakSettingsError):
        EyeBreakModule(FakeCtx(tmp_path, {"later_min": 0}), api=_win32.FakeApi())


def test_changing_interval_keeps_count(make: Any) -> None:
    h = make()
    h.run(15 * 60)
    assert h.m.update_settings({"eye": {"interval_min": 10}}) is None
    h.run(5)
    assert len(h.ctx.notifications) == 1  # すでに超えていれば次の読み取りで
    assert h.ctx.writes[-1]["eye"] == {"enabled": True, "interval_min": 10}
    assert h.m.update_settings({"later_min": 99}) is not None
    assert h.m.cfg.later_min == 10


def test_eye_disabled_only_body(make: Any) -> None:
    h = make({"eye": {"enabled": False}})
    h.run(60 * 60)
    assert h.titles() == ["ひと休みしませんか"]


def test_daily_broken_is_set_aside_and_old_days_pruned(tmp_path: Any, make: Any) -> None:
    ctx = FakeCtx(tmp_path / "d2", {"mode": "live"})
    (ctx.data_dir / "daily.json").write_text("{bad", encoding="utf-8")
    h = make(ctx=ctx)
    assert (ctx.data_dir / "daily.json.bad").exists()
    assert "daily broken" in ctx.handler.text()
    h.m.daily.days["2026-01-01"] = {"eye": 3}
    h.m.daily.days["2026-06-01"] = {"eye": 2}
    h.m.daily.save()
    h.m.stop()
    h2 = make(ctx=ctx)
    assert "2026-01-01" not in h2.m.daily.days and "2026-06-01" in h2.m.daily.days


def test_usage_and_diagnostics(make: Any) -> None:
    h = make()
    h.run(60 * 60)
    h.run(30, active=False)
    u = {s.key: s for s in h.m.usage(7)}
    assert u["prompts"].per_day[-1] == 3 and u["prompts"].primary and "R-3" in (u["prompts"].hint or "")
    assert u["rested"].per_day[-1] == 0
    d = h.m.diagnostics()
    assert d["mode"] == "live" and d["idle_readable"] is True and d["game"] == "after"
    assert d["prompts_14d"] == 3 and d["quiet_modes"] == 0 and d["muted_today"] is False
    assert all(isinstance(v, (str, int, bool)) for v in d.values())


def test_notification_click_opens_dialog(qapp: Any, make: Any) -> None:
    h = make()
    h.run(20 * 60)
    h.ctx.notifications[0]["on_click"]()
    dlg = h.m._dialog  # noqa: SLF001
    assert dlg is not None and dlg.isVisible()
    dlg.later_btn.click()
    assert h.m.engine.later_ is not None and h.m.today_counts()["later"] == 1
    h.ctx.notifications[0]["on_click"]()
    dlg = h.m._dialog  # noqa: SLF001
    dlg.mute_btn.click()
    assert h.m.muted()
    assert dlg.done_label.text() == "明日の朝4時まで出しません。トレイの『再開する』でいつでも戻せます。"
    dlg.settings_btn.click()
    assert h.ctx.shown == 1
