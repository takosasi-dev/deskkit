# L2 自動スナップショット: 構成が stable_min 分落ち着いたら1回・以後 interval_min 分ごと・同じ配置なら書かない。
# 利用者のプリセットには触らない(別ファイル)。スヌーズ中・ゲーム/全画面が前面の間は取らない。
# 構成が動き出したら「最新」を「抜く前」に写し、「抜く前の配置に戻す」で選べる。スヌーズ中は自動の提案もしない。
from __future__ import annotations

import types
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import deskkit.modules.layoutkeep.module as m
from deskkit.modules.layoutkeep.model import Placement
from deskkit.modules.layoutkeep.selftest import Scenario
from deskkit.modules.layoutkeep.sysevents import WM_DISPLAYCHANGE

from .fakes import GAME_FG, FakeCtx, make_module


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def minutes(self, n: float) -> None:
        self.t += n * 60


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(m, "time", types.SimpleNamespace(monotonic=lambda: c.t))
    return c


def _started(tmp_path: Path, sc: Scenario, **kw: Any) -> tuple[FakeCtx, Any]:
    ctx, mod = make_module(tmp_path, sc.api, {"targets": [{"exe": t} for t in sc.targets], **kw})
    mod.start()
    return ctx, mod


def _snap_lines(mod: Any) -> int:
    p = mod.store.oplog_path
    return p.read_text(encoding="utf-8").count('"action":"snapshot"') if p.exists() else 0


def _move_a_window(sc: Scenario) -> None:
    w = sc.api.windows[0]
    sc.api.windows[0] = replace(w, placement=Placement(1, (5, 5, 400, 400)), screen_rect=(5, 45, 400, 440))


def test_defaults_on_but_harmless(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = make_module(tmp_path, scenario.api, {})
    assert ctx.section["auto_snapshot"] == {"enabled": True, "stable_min": 10, "interval_min": 30}
    assert ctx.section["place_new"] == {"enabled": False, "mode": "dry_run", "poll_ms": 1500}
    assert mod.cfg.snapshot_enabled and not mod.cfg.place_enabled


def test_schedule_stable_then_interval_and_unchanged_skip(tmp_path: Path, scenario: Scenario, clock: Clock) -> None:
    ctx, mod = _started(tmp_path, scenario)
    sig = mod.last_sig.signature
    clock.minutes(5)
    mod._snapshot_tick()
    assert _snap_lines(mod) == 0  # まだ落ち着いて10分たっていない
    clock.minutes(5)
    mod._snapshot_tick()
    assert _snap_lines(mod) == 1 and mod.codes["snapshot"] == "saved"
    auto = mod.store.load_auto(sig)
    assert auto is not None and "latest" in auto.slots and len(auto.slots["latest"].windows) == 7
    assert not mod.store.has_layout(sig)  # 利用者のプリセットは作らない
    clock.minutes(10)
    mod._snapshot_tick()
    assert _snap_lines(mod) == 1  # 30分たつまで取らない
    clock.minutes(21)
    mod._snapshot_tick()
    assert mod.codes["snapshot"] == "unchanged" and _snap_lines(mod) == 1  # 同じ配置なら書かない
    _move_a_window(scenario)
    clock.minutes(31)
    mod._snapshot_tick()
    assert mod.codes["snapshot"] == "saved" and _snap_lines(mod) == 2
    assert scenario.api.set_calls == []


def test_snapshot_never_touches_user_presets(tmp_path: Path, scenario: Scenario, clock: Clock) -> None:
    ctx, mod = _started(tmp_path, scenario)
    mod.store.save_layout(scenario.layout, "作業用")
    p = mod.store.layout_path(scenario.layout.signature)
    before = p.read_bytes()
    _move_a_window(scenario)
    assert mod.snapshot_now("cli") == "saved"
    assert p.read_bytes() == before
    assert mod.store.auto_path(scenario.layout.signature).exists()


def test_skipped_while_snoozed_or_game_and_retried(tmp_path: Path, scenario: Scenario, clock: Clock) -> None:
    ctx, mod = _started(tmp_path, scenario)
    clock.minutes(11)
    ctx.snoozed = True
    mod._snapshot_tick()
    assert mod.codes["snapshot"] == "snoozed" and _snap_lines(mod) == 0
    ctx.snoozed = False
    ctx.fg = GAME_FG
    clock.minutes(1)
    mod._snapshot_tick()
    assert mod.codes["snapshot"] == "game" and _snap_lines(mod) == 0
    ctx.fg = FakeCtx(tmp_path).fg  # 安全な前面
    clock.minutes(1)
    mod._snapshot_tick()  # 控えた理由が一時的なら次の確認でまた試す
    assert mod.codes["snapshot"] == "saved"


def test_disabled_snapshot_does_nothing(tmp_path: Path, scenario: Scenario, clock: Clock) -> None:
    ctx, mod = _started(tmp_path, scenario, auto_snapshot={"enabled": False})
    assert mod._snap_timer is None
    clock.minutes(60)
    mod._snapshot_tick()
    assert _snap_lines(mod) == 0


def test_kick_promotes_to_before_change_and_restore(tmp_path: Path, scenario: Scenario, clock: Clock) -> None:
    ctx, mod = _started(tmp_path, scenario)
    sig = mod.last_sig.signature
    assert mod.auto_restore_name(sig) is None
    assert mod.restore_auto("tray") == "no_layout"
    assert mod.snapshot_now("cli") == "saved"
    assert mod.auto_restore_name(sig) == "自動"
    # 抜き差し: 構成が動き出した時点で「最新」を「抜く前」に写す
    for h in ctx.native[WM_DISPLAYCHANGE]:
        h(0, 0)
    auto = mod.store.load_auto(sig)
    assert auto is not None and "before_change" in auto.slots
    assert mod.auto_restore_name(sig) == "抜く前"
    # 戻ってきた後の自動スナップショットは「最新」だけを更新し、「抜く前」は残る
    for _ in range(2):
        ctx.timers[-1].fire()
    _move_a_window(scenario)
    clock.minutes(11)
    mod._snapshot_tick()
    auto = mod.store.load_auto(sig)
    assert auto.slots["latest"].saved_at and auto.slots["before_change"].windows[0].normal_rect != (5, 5, 400, 400)
    # 「抜く前の配置に戻す」は試運転なら計画だけ
    assert mod.restore_auto("tray") == "dry_run"
    assert scenario.api.set_calls == []
    assert '"preset":"抜く前"' in mod.store.oplog_path.read_text(encoding="utf-8")


def test_restore_before_change_live_moves_windows(tmp_path: Path, scenario: Scenario, clock: Clock) -> None:
    ctx, mod = _started(tmp_path, scenario, mode="live")
    assert mod.snapshot_now("cli") == "saved"
    for h in ctx.native[WM_DISPLAYCHANGE]:
        h(0, 0)
    _move_a_window(scenario)
    assert mod.restore_auto("tray") == "applied"
    (hwnd, pl), = scenario.api.set_calls
    assert hwnd == scenario.api.windows[0].hwnd and pl.normal_rect == (50, 50, 600, 500)


def test_no_proposal_while_snoozed_but_manual_apply_works(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario, settle_checks=1)
    mod.store.save_layout(scenario.layout)
    ctx.snoozed = True
    for h in ctx.native[WM_DISPLAYCHANGE]:
        h(0, 0)
    ctx.timers[-1].fire()
    assert mod.proposal_sig is None
    assert not any("配置を戻しますか" in n["title"] for n in ctx.notes)
    text = mod.store.oplog_path.read_text(encoding="utf-8")
    assert '"reason":"snoozed"' in text
    assert "一時停止中" in ctx.status
    # 手で押した操作はスヌーズ中でも動く
    assert mod.apply_current("tray") == "dry_run"


def test_auto_apply_skipped_while_snoozed(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario, mode="live", auto_apply=True, settle_checks=1)
    mod.store.save_layout(scenario.layout)
    ctx.snoozed = True
    for h in ctx.native[WM_DISPLAYCHANGE]:
        h(0, 0)
    ctx.timers[-1].fire()
    assert scenario.api.set_calls == []
