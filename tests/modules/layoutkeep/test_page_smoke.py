# 画面(page.py)のスモークテスト: offscreen の QApplication で偽 ctx + 偽 Win32 のページを組み立て、
# 更新・マップのホバー・レイアウト選択・計画表示が例外なく動くことを確かめる(ダイアログは開かない)。
from __future__ import annotations

import os
from pathlib import Path

import pytest

from deskkit.modules.layoutkeep.selftest import Scenario

from .fakes import make_module

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():  # type: ignore[no-untyped-def]
    from PySide6.QtWidgets import QApplication

    from deskkit.ui import theme

    app = QApplication.instance() or QApplication([])
    theme.apply(app)  # type: ignore[arg-type]
    return app


def _pump(app: object, n: int = 5) -> None:
    for _ in range(n):
        app.processEvents()  # type: ignore[attr-defined]


def test_page_builds_and_updates(qapp: object, tmp_path: Path, scenario: Scenario) -> None:
    from PySide6.QtCore import QPointF

    ctx, mod = make_module(tmp_path, scenario.api, {"targets": [{"exe": t} for t in scenario.targets],
                                                     "names": {"aaaaaaaaaaaa": "別構成"}})
    mod.store.save_layout(scenario.layout)
    mod.start()
    page = mod.create_page()
    page.resize(1100, 900)
    page.show()
    _pump(qapp)
    page.refresh()
    _pump(qapp)
    assert page.tbl_entries.rowCount() == len(scenario.layout.windows)
    assert page.tbl_layouts.rowCount() == 1
    assert page.tbl_targets.rowCount() == len(scenario.targets)
    # 計画(試運転)→ 表とマップに反映。SetWindowPlacement は呼ばれない
    page._plan()
    _pump(qapp)
    assert page.tbl_plan.rowCount() == len(scenario.layout.windows)
    assert scenario.api.set_calls == []
    # マップのホバー(箱の中心を探して当てる)
    m = page.map
    m.grab()  # 描画を1回通す
    xf = m._xform()
    assert xf is not None
    for b in m._boxes:
        r = m._map(m._box_rect(b), xf)
        idx = m._box_at(QPointF(r.center()))
        assert idx >= 0
        break
    page._on_map_hover(0)
    page._on_map_hover(-1)
    # 履歴
    page._fill_history()
    assert page.tbl_hist.rowCount() >= 1
    # 変更通知で作り直しても落ちない
    mod.signals.changed.emit()
    _pump(qapp)
    page.close()
    page.deleteLater()
    _pump(qapp)


def test_page_when_disabled(qapp: object, tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = make_module(tmp_path, scenario.api, {}, awareness="unaware")
    mod.start()
    page = mod.create_page()
    page.show()
    _pump(qapp)
    assert page.banner.isVisible()
    assert not page.btn_save.isEnabled()
    page.close()


def test_monitor_map_standalone(qapp: object) -> None:
    from deskkit.modules.layoutkeep.monitor_map import MapBox, MapMonitor, MonitorMap

    m = MonitorMap("#60A5FA")
    m.resize(600, 300)
    m.show()
    m.set_data([MapMonitor("a", (0, 0, 1920, 1080), (0, 0, 1920, 1040), True, 96, 1),
                MapMonitor("b", (1920, 0, 4480, 1440), None, False, 144, 2)],
               [MapBox("k1", 0, (100, 100, 900, 700), "notepad", current=(0, 0, 400, 300), moving=True),
                MapBox("k2", 1, (2000, 0, 4480, 1400), "chrome", maximized=True, muted=True)])
    _pump(qapp)
    m.grab()
    m.set_data([], [])
    m.grab()
    m.close()


def test_page_presets_snapshot_and_place_cards(qapp: object, tmp_path: Path, scenario: Scenario) -> None:
    from deskkit.modules.layoutkeep.placer import PlaceEvent

    ctx, mod = make_module(tmp_path, scenario.api, {"targets": [{"exe": t} for t in scenario.targets]})
    sig = scenario.layout.signature
    mod.store.save_layout(scenario.layout)
    mod.store.save_layout(scenario.layout, "配信用")
    mod.start()
    assert mod.snapshot_now("cli") == "saved"
    mod.place_events.appendleft(PlaceEvent("2026-09-25T10:00:00+09:00", "ed.exe", "EdWnd", 5, "would_move", "move", "M-3",
                                           "基本", (0, 0, 10, 10), (100, 100, 700, 600)))
    page = mod.create_page()
    page.resize(1100, 900)
    page.show()
    _pump(qapp)
    page.refresh()
    _pump(qapp)
    # プリセット2件 + 自動保存「最新」
    assert page.tbl_presets.rowCount() == 3
    assert page.tbl_layouts.item(0, 2).text().startswith("2 件")
    assert page.tbl_place.rowCount() == 1 and "試運転" in page.tbl_place.item(0, 1).text()
    assert "最新" in page.lbl_snap.text()
    # 「配信用」を選ぶ → マップ・表はそのプリセット。計画は試運転で動かさない
    page.tbl_presets.selectRow(1)
    _pump(qapp)
    assert page._shown_preset == "配信用"
    assert page.btn_p_default.isEnabled() and page.btn_p_apply.isEnabled()
    page._preset_plan()
    _pump(qapp)
    assert mod.last_plan_preset == "配信用" and "配信用" in page.lbl_plan.text()
    assert scenario.api.set_calls == []
    # 既定にする
    page._preset_default()
    _pump(qapp)
    assert mod.preset_names(sig)[0] == "配信用"
    # 自動保存の行を選ぶと上書き・名前変更・削除はできない
    page.tbl_presets.selectRow(2)
    _pump(qapp)
    assert page._shown_preset == "自動"
    assert not page.btn_p_overwrite.isEnabled() and not page.btn_p_delete.isEnabled() and page.btn_p_plan.isEnabled()
    # L2 / L3 のスイッチ
    page._on_place_toggle(True)
    assert ctx.section["place_new"]["enabled"] is True and mod._place_timer is not None
    page._set_nested("auto_snapshot", "enabled", False)
    assert ctx.section["auto_snapshot"]["enabled"] is False
    page._snap_now()
    ctx.snoozed = True
    page.refresh()
    _pump(qapp)
    assert not page.pill_snooze.isHidden()
    page.close()
    page.deleteLater()
    _pump(qapp)
