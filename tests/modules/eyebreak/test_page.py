# EyeBreak の画面(FR-16): 組み立ての順・様子見の表示と切り替え・設定の保存・静かにするモード・7日間の記録。
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QCheckBox, QLabel

from .conftest import SECRET_LABEL, SECRET_MODE, FakeCtx


def _texts(w: Any) -> str:
    return "\n".join(lb.text() for lb in w.findChildren(QLabel))


def test_page_dry_run_hero_and_start(qapp: Any, make: Any) -> None:
    h = make({"mode": "dry_run"})
    h.run(20 * 60)
    page = h.m.create_page()
    page.resize(1000, 1400)
    page.show()
    qapp.processEvents()
    assert page.dry_label.text() == "様子見中: 声かけは出していません。出していたら今日は 1 回でした。"
    assert page.mode_btn.text().endswith("声かけを始める")
    page.mode_btn.click()
    assert h.m.cfg.mode == "live" and h.ctx.writes[-1]["mode"] == "live"
    assert page.mode_btn.text().endswith("様子見に戻す") and page.dry_label.isHidden()
    t = _texts(page)
    assert "目や体の調子が気になるときは、この道具ではなく専門家に相談してください。" in t
    assert "米国眼科学会" in t
    page.close()


def test_page_tiles_and_table(qapp: Any, make: Any) -> None:
    h = make()
    h.run(20 * 60)
    h.run(30, active=False)
    page = h.m.create_page()
    assert page.t_prompts.value.text() == "1 回"
    assert page.t_rested.value.text() == "1 回"
    assert page.table.item(0, 1).text() == "1" and page.table.item(0, 2).text() == "1"
    assert page.table.item(0, 0).text() == "9/28(月)"


def test_page_settings_save_and_quiet_modes(qapp: Any, make: Any) -> None:
    h = make()
    page = h.m.create_page()
    page.eye_min.setValue(30)
    page.eye_min.editingFinished.emit()
    assert h.m.cfg.eye_min == 30
    page.body_on.click()
    assert h.m.cfg.body_enabled is False
    page.game.set_value("pause", emit=True)
    assert h.m.cfg.game == "pause"
    boxes = {cb.text(): cb for cb in page.findChildren(QCheckBox)}
    assert SECRET_LABEL in boxes
    boxes[SECRET_LABEL].setChecked(True)
    assert h.m.cfg.quiet_modes == (SECRET_MODE,)
    page.later_min.setValue(15)
    page.later_min.editingFinished.emit()
    assert h.m.cfg.later_min == 15
    assert h.ctx.tray["10分あとで"].label == "15分あとで"  # トレイの項目の文言も変わる


def test_page_quiet_card_disabled_without_modes(qapp: Any, make: Any, tmp_path: Any) -> None:
    ctx = FakeCtx(tmp_path / "nm", {"mode": "live"})
    ctx.modes = []
    h = make(ctx=ctx)
    page = h.m.create_page()
    assert "ModeShift のモードがありません。" in _texts(page)


def test_page_unavailable_warning(qapp: Any, make: Any) -> None:
    h = make()
    h.api.fail = True
    h.run(15)
    page = h.m.create_page()
    page.show()
    qapp.processEvents()
    assert page.warn.isVisible()
    assert page.pill.text().endswith("この PC では使っている時間を読めません")
    page.close()
