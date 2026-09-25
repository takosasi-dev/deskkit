# v0.2 の「記録を止める理由」: 本体のスヌーズ(ctx.is_snoozed)と、ModeShift のモード中は止める(M3、契約 §2)。
# どちらも判定理由は paused(§9.4 の語彙を増やさない)で、本文は読まない(INV-3)。通知は出さず、状態表示だけ。
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from deskkit.modules.clipshelf import policy
from deskkit.modules.clipshelf.fakes import FakeCipher, FakeClock, FakeWin32
from deskkit.modules.clipshelf.module import MODE_STATE_FILE, PAUSE_MODE, PAUSE_SNOOZE, ClipShelfModule


def _copy(m: Any, api: Any, text: str) -> Any:
    api.put(text)
    return m.monitor.process()


def test_snooze_stops_recording_without_reading_text(module_factory: Any) -> None:
    m, ctx, api, _ = module_factory({"mode": "record"})
    ctx.snoozed = True
    d = _copy(m, api, "while snoozed")
    assert d.reason == policy.PAUSED and api.text_reads == 0 and m.store.row_count() == 0
    assert m.pause_reasons() == [PAUSE_SNOOZE]
    m._periodic()  # イベントが来なくても定期処理で状態表示を直す
    assert ctx.status == "スヌーズ中(記録しない)"
    ctx.snoozed = False
    assert _copy(m, api, "after").reason == policy.RECORDED
    ctx.fire("host.snooze_changed", {"snoozed": False, "until": None})
    assert ctx.status == "記録中"
    assert not any(n[1].startswith("記録") for n in ctx.notifications)  # 止めた・再開したは通知しない


def test_snooze_does_not_block_manual_actions(module_factory: Any) -> None:
    m, ctx, api, _ = module_factory({"mode": "record"})
    item = m.store.get(_copy(m, api, "x").item_id)
    ctx.snoozed = True
    m.choose(item)  # 利用者の明示操作は止めない(契約 §1)
    assert api.text_now() == "x"


def test_old_host_without_snooze_api(module_factory: Any) -> None:
    m, ctx, api, _ = module_factory({"mode": "record"})
    m.ctx = SimpleNamespace(log=ctx.log)  # 古い本体(is_snoozed / list_modes が無い)
    try:
        assert m.is_snoozed() is False and m.mode_choices() == []
    finally:
        m.ctx = ctx


def test_broken_host_apis_do_not_stop_recording(module_factory: Any) -> None:
    m, ctx, api, _ = module_factory({"mode": "record"})

    def boom() -> Any:
        raise RuntimeError("host bug")

    ctx.is_snoozed = boom  # type: ignore[method-assign]
    ctx.list_modes = boom  # type: ignore[method-assign]
    assert m.is_snoozed() is False and m.mode_choices() == []
    assert _copy(m, api, "x").reason == policy.RECORDED


def test_mode_pause_switched_and_reverted(module_factory: Any) -> None:
    m, ctx, api, _ = module_factory({"mode": "record", "pause_in_modes": ["meeting"]})
    ctx.fire("modeshift.switched", {"mode": "focus", "run_id": "r1", "failed": 0})
    assert not m.mode_paused() and _copy(m, api, "a").reason == policy.RECORDED
    ctx.fire("modeshift.switched", {"mode": "meeting", "run_id": "r2", "failed": 0})
    assert m.pause_reasons() == [PAUSE_MODE] and ctx.status == "モード中のため停止(記録しない)"
    d = _copy(m, api, "secret in meeting")
    assert d.reason == policy.PAUSED and api.text_reads == 1  # 1回目の記録分だけ
    # リスト外のモードへの switched で再開
    ctx.fire("modeshift.switched", {"mode": "focus", "run_id": "r3", "failed": 0})
    assert not m.mode_paused()
    ctx.fire("modeshift.switched", {"mode": "meeting", "run_id": "r4", "failed": 1})
    assert m.mode_paused()
    ctx.fire("modeshift.reverted", {"mode": "meeting", "run_id": "r4"})
    assert not m.mode_paused() and ctx.status == "記録中"
    assert _copy(m, api, "b").reason == policy.RECORDED
    assert not ctx.errors and not [n for n in ctx.notifications if "モード" in n[1]]
    logs = (ctx.data_dir / "ops.jsonl").read_text(encoding="utf-8")
    assert "meeting" not in logs


def test_mode_pause_follows_setting_changes(module_factory: Any) -> None:
    m, ctx, api, _ = module_factory({"mode": "record", "pause_in_modes": []})
    ctx.fire("modeshift.switched", {"mode": "meeting", "run_id": "r", "failed": 0})
    assert not m.mode_paused()

    def add(s: dict[str, Any]) -> None:
        s["pause_in_modes"] = ["meeting", "gone-mode"]

    assert m.update_settings(add) is None
    assert m.mode_paused() and ctx.status.startswith("モード中")
    assert ctx.writes[-1][0]["pause_in_modes"] == ["meeting", "gone-mode"]  # 今は無いモード名も残す
    m.clear_mode_pause()
    assert not m.mode_paused() and not (ctx.data_dir / MODE_STATE_FILE).exists()


def test_mode_state_survives_restart(tmp_path: Any, qapp: Any) -> None:
    from tests.modules.clipshelf.conftest import FakeCtx

    ctx = FakeCtx(tmp_path / "d", {"mode": "record", "pause_in_modes": ["meeting"]})
    m = ClipShelfModule(ctx, api=FakeWin32(), cipher=FakeCipher(), now=FakeClock())  # type: ignore[arg-type]
    m.start()
    ctx.fire("modeshift.switched", {"mode": "meeting", "run_id": "r", "failed": 0})
    m.stop()
    assert json.loads((ctx.data_dir / MODE_STATE_FILE).read_text(encoding="utf-8")) == {"mode": "meeting"}
    ctx2 = FakeCtx(tmp_path / "d", {"mode": "record", "pause_in_modes": ["meeting"]})
    m2 = ClipShelfModule(ctx2, api=FakeWin32(), cipher=FakeCipher(), now=FakeClock())  # type: ignore[arg-type]
    m2.start()
    try:
        assert m2.mode_paused() and ctx2.status.startswith("モード中")
    finally:
        m2.stop()


def test_bad_mode_payload_is_ignored(module_factory: Any) -> None:
    m, ctx, api, _ = module_factory({"mode": "record", "pause_in_modes": ["meeting"]})
    ctx.fire("modeshift.switched", {"run_id": "r"})
    ctx.fire("modeshift.switched", {"mode": 3})
    assert not m.mode_paused() and not ctx.errors


def test_page_mode_list_and_state(module_factory: Any, qapp: Any) -> None:
    from PySide6.QtCore import Qt

    m, ctx, api, _ = module_factory({"mode": "record", "pause_in_modes": ["old-mode"]})
    ctx.modes = [("meeting", "会議"), ("focus", "focus")]
    page = m.create_page()
    rows = [(page.mode_list.item(i).data(Qt.ItemDataRole.UserRole), page.mode_list.item(i).checkState())
            for i in range(page.mode_list.count())]
    assert rows == [("meeting", Qt.CheckState.Unchecked), ("focus", Qt.CheckState.Unchecked),
                    ("old-mode", Qt.CheckState.Checked)]
    page.mode_list.item(0).setCheckState(Qt.CheckState.Checked)
    assert ctx.writes[-1][0]["pause_in_modes"] == ["meeting", "old-mode"]
    ctx.fire("modeshift.switched", {"mode": "meeting", "run_id": "r", "failed": 0})
    assert "meeting" in page.mode_state.text() and page.mode_clear_btn.isVisibleTo(page)
    assert page.pause_pill.isVisibleTo(page) and "モード中" in page.pause_pill.text()
    page._clear_mode_pause()
    assert not m.mode_paused() and not page.mode_clear_btn.isVisibleTo(page)
    page.close()
