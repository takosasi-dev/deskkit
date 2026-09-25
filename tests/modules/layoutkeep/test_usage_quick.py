# 利用状況(usage)とクイックアクションのテスト。oplog から日ごとの件数だけを数え、中身を持たないこと。
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from deskkit.modules.layoutkeep.selftest import Scenario
from deskkit.modules.layoutkeep.usage import R4_HINT, usage_series

from .fakes import make_module

TODAY = dt.date(2026, 9, 24)


def _write(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n{broken\n", encoding="utf-8")


def test_usage_series_counts(tmp_path: Path) -> None:
    log = tmp_path / "oplog.jsonl"
    _write(log, [
        {"ts": "2026-09-24T10:00:00+09:00", "action": "apply", "source": "tray", "skipped": {"ambiguous": 2}},
        {"ts": "2026-09-24T11:00:00+09:00", "action": "apply", "source": "auto", "skipped": {}},
        {"ts": "2026-09-24T12:00:00+09:00", "action": "apply", "result": "undo_failed"},
        {"ts": "2026-09-23T09:00:00+09:00", "action": "plan", "source": "gui", "skipped": {"ambiguous": 5}},
        {"ts": "2026-09-23T09:10:00+09:00", "action": "save"},
        {"ts": "2026-09-22T09:10:00+09:00", "action": "suppressed", "reason": "game"},
        {"ts": "2026-08-01T09:10:00+09:00", "action": "save"},  # 期間外
    ])
    s = {x.key: x for x in usage_series(log, 3, TODAY)}
    assert s["applies"].primary and s["applies"].per_day == [0, 0, 2]
    assert s["applies"].extra["auto"] == [0, 0, 1]
    assert s["plans"].per_day == [0, 1, 0]
    # 曖昧は本番の適用行だけ(試運転の計画は数えない)
    assert s["ambiguous"].per_day == [0, 0, 2] and s["ambiguous"].good_when == "low" and s["ambiguous"].hint == R4_HINT
    assert s["ambiguous"].extra["applies_with_ambiguous"] == [0, 0, 1]
    assert s["saves"].per_day == [0, 1, 0]
    assert s["suppressed"].per_day == [1, 0, 0]
    assert sum(1 for x in s.values() if x.primary) == 1


def test_usage_without_oplog(tmp_path: Path) -> None:
    assert all(x.per_day == [0] * 7 for x in usage_series(tmp_path / "none.jsonl", 7, TODAY))


def test_module_usage_and_quick_actions(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = make_module(tmp_path, scenario.api, {"targets": [{"exe": t} for t in scenario.targets], "mode": "live"})
    mod.store.save_layout(scenario.layout)
    mod.start()
    labels = [lb for lb, _ in ctx.quick]
    name = mod.display_name(scenario.layout.signature)
    assert f"{name} の計画を表示(試運転)" in labels
    assert "LayoutKeep を試運転に切り替える" in labels
    # 計画のクイックアクションは動かさない
    dict(ctx.quick)[f"{name} の計画を表示(試運転)"]()
    assert scenario.api.set_calls == []
    assert ctx.notes[-1]["title"].startswith("LayoutKeep(計画のみ)")
    # 試運転へ切り替え → そのクイックアクションは消える
    dict(ctx.quick)["LayoutKeep を試運転に切り替える"]()
    assert ctx.section["mode"] == "dry_run"
    assert "LayoutKeep を試運転に切り替える" not in [lb for lb, _ in ctx.quick]
    keys = [x.key for x in mod.usage(7)]
    assert keys == ["applies", "plans", "ambiguous", "saves", "suppressed"]
    assert mod.usage(7)[1].per_day[-1] == 1  # 今の計画が今日の分に入る


def test_no_plan_quick_action_without_layout(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = make_module(tmp_path, scenario.api, {"targets": [{"exe": t} for t in scenario.targets]})
    mod.start()
    assert not any("計画を表示" in lb for lb, _ in ctx.quick)
