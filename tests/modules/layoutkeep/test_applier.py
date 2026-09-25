# 適用(applier)と store のテスト: dry_run で SetWindowPlacement 0回(INV-7)・undo 書き込み失敗で中止(INV-6 / AC-18)・
# 曖昧は動かさない(INV-2)・失敗コードの記録・取り消し。
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from deskkit.modules.layoutkeep.applier import Applier
from deskkit.modules.layoutkeep.model import SW_SHOWMAXIMIZED, SW_SHOWNOACTIVATE
from deskkit.modules.layoutkeep.selftest import Scenario, _FailingStore, make_plan
from deskkit.modules.layoutkeep.store import Store, StoreWriteError


def test_dry_run_never_sets(scenario: Scenario, tmp_path: Path) -> None:
    res = Applier(scenario.api, Store(tmp_path)).apply(make_plan(scenario), dry_run=True)
    assert res.status == "dry_run"
    assert scenario.api.set_calls == []
    assert not (tmp_path / "undo.json").exists()


def test_undo_write_failure_aborts(scenario: Scenario, tmp_path: Path) -> None:
    res = Applier(scenario.api, _FailingStore(tmp_path)).apply(make_plan(scenario), dry_run=False)
    assert res.status == "undo_failed" and res.moved == 0
    assert scenario.api.set_calls == []


def test_readonly_undo_file_aborts(scenario: Scenario, tmp_path: Path) -> None:
    # AC-18 のロジック: undo.json を読み取り専用にすると置き換えに失敗し、どのウィンドウも動かさない
    undo = tmp_path / "undo.json"
    undo.write_text("{}", encoding="utf-8")
    os.chmod(undo, stat.S_IREAD)
    try:
        res = Applier(scenario.api, Store(tmp_path)).apply(make_plan(scenario), dry_run=False)
        assert res.status == "undo_failed"
        assert scenario.api.set_calls == []
    finally:
        os.chmod(undo, stat.S_IWRITE | stat.S_IREAD)


def test_live_moves_only_planned_and_never_ambiguous(scenario: Scenario, tmp_path: Path) -> None:
    plan = make_plan(scenario)
    res = Applier(scenario.api, Store(tmp_path)).apply(plan, dry_run=False)
    called = [h for h, _ in scenario.api.set_calls]
    assert sorted(called) == sorted(m.hwnd for m in plan.moves if m.hwnd is not None)
    assert 401 not in called and 402 not in called  # Chrome 2枚(M-5)
    assert 501 not in called  # 最小化中(M-6)
    assert res.moved == len(plan.moves)
    undo = json.loads((tmp_path / "undo.json").read_text(encoding="utf-8"))
    assert len(undo["windows"]) == len(plan.moves)
    assert "title" not in json.dumps(undo)


def test_show_cmd_does_not_activate_normal_windows(scenario: Scenario, tmp_path: Path) -> None:
    Applier(scenario.api, Store(tmp_path)).apply(make_plan(scenario), dry_run=False)
    cmds = {p.show_cmd for _, p in scenario.api.set_calls}
    assert cmds <= {SW_SHOWNOACTIVATE, SW_SHOWMAXIMIZED}


def test_set_failure_is_recorded_and_others_continue(scenario: Scenario, tmp_path: Path) -> None:
    scenario.api.fail_hwnds[101] = 5  # ERROR_ACCESS_DENIED(昇格ウィンドウ相当)
    plan = make_plan(scenario)
    res = Applier(scenario.api, Store(tmp_path)).apply(plan, dry_run=False)
    assert res.skipped.get("set_failed") == 1
    assert res.errors == [{"exe": "notepad.exe", "class": "Notepad", "hwnd": 101, "win32_error": 5}]
    assert res.moved == len(plan.moves) - 1


def test_focus_change_is_detected(scenario: Scenario, tmp_path: Path) -> None:
    scenario.api.steal_focus = True
    res = Applier(scenario.api, Store(tmp_path)).apply(make_plan(scenario), dry_run=False)
    assert res.focus_kept is False


def test_undo_restores_and_clears(scenario: Scenario, tmp_path: Path) -> None:
    store = Store(tmp_path)
    plan = make_plan(scenario)
    before = {w.hwnd: w.placement for w in scenario.api.windows}
    Applier(scenario.api, store).apply(plan, dry_run=False)
    res = Applier(scenario.api, store).undo(plan.signature, dry_run=False)
    assert res.status == "applied" and res.moved == len(plan.moves)
    after = {w.hwnd: w.placement for w in scenario.api.windows}
    for m in plan.moves:
        assert m.hwnd is not None
        b, a = before[m.hwnd], after[m.hwnd]
        assert b is not None and a is not None and b.normal_rect == a.normal_rect
    assert store.read_undo() is None


def test_undo_refused_on_other_signature(scenario: Scenario, tmp_path: Path) -> None:
    store = Store(tmp_path)
    plan = make_plan(scenario)
    Applier(scenario.api, store).apply(plan, dry_run=False)
    n = len(scenario.api.set_calls)
    res = Applier(scenario.api, store).undo("ffffffffffff", dry_run=False)
    assert res.status == "sig_mismatch" and len(scenario.api.set_calls) == n
    assert store.read_undo() is not None


def test_undo_in_dry_run_does_not_set(scenario: Scenario, tmp_path: Path) -> None:
    store = Store(tmp_path)
    plan = make_plan(scenario)
    Applier(scenario.api, store).apply(plan, dry_run=False)
    n = len(scenario.api.set_calls)
    res = Applier(scenario.api, store).undo(plan.signature, dry_run=True)
    assert res.status == "dry_run" and len(scenario.api.set_calls) == n


def test_store_broken_layout_is_renamed_on_save(scenario: Scenario, tmp_path: Path) -> None:
    store = Store(tmp_path)
    sig = scenario.layout.signature
    store.layouts_dir.mkdir(parents=True)
    store.layout_path(sig).write_text("{ broken", encoding="utf-8")
    broken_to = store.save_layout(scenario.layout)
    assert broken_to is not None and broken_to.exists() and ".broken-" in broken_to.name
    assert store.load_layout(sig) is not None


def test_store_prev_and_delete(scenario: Scenario, tmp_path: Path) -> None:
    store = Store(tmp_path)
    sig = scenario.layout.signature
    store.save_layout(scenario.layout)
    store.save_layout(scenario.layout)
    assert store.prev_path(sig).exists()
    dst = store.delete_layout(sig)
    assert dst.exists() and not store.layout_path(sig).exists()
    assert [s.signature for s in store.list_layouts()] == []


def test_oplog_sanitizes_title_keys(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.append_oplog({"action": "save", "title": "秘密", "title_at_save": "秘密", "sig": "x"})
    text = (tmp_path / "oplog.jsonl").read_text(encoding="utf-8")
    assert '"title' not in text and "秘密" not in text


def test_atomic_write_error_type(tmp_path: Path) -> None:
    store = Store(tmp_path / "nope")
    (tmp_path / "nope").write_text("file, not dir", encoding="utf-8")
    try:
        store.write_undo({"windows": []})
    except StoreWriteError:
        pass
    else:
        raise AssertionError("StoreWriteError が出るはず")
