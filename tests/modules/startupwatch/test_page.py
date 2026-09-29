# StartupWatch の画面: 組み立て・新しく増えた物の行・確かめた・表・見ている場所・設定の切り替え(偽の Win32 で)。
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QLabel, QPushButton

from deskkit.modules.startupwatch.fakes import reg_key_of, sample_api

from .conftest import FakeCtx


def _texts(w: Any) -> str:
    return "\n".join(lb.text() for lb in w.findChildren(QLabel))


def _button(w: Any, text: str) -> QPushButton:
    for b in w.findChildren(QPushButton):
        if b.text().endswith(text):
            return b
    raise AssertionError(text)


def test_page_shows_baseline_new_items_and_locations(qapp: Any, make_module: Any) -> None:
    api = sample_api()
    m0, ctx, _ = make_module(api)
    m0.start()
    m0.stop()
    api.add_value("hkcu_run", "NewApp", r"C:\N\new.exe --min")
    api.reg_errors[reg_key_of("hklm_run32")] = 5
    m, _, opened = make_module(api, ctx)
    m.start()
    page = m.create_page()
    page.resize(1000, 900)
    page.show()
    qapp.processEvents()
    t = _texts(page)
    assert "NewApp" in t and r"C:\N\new.exe --min" in t
    assert "読めませんでした(管理者でないと見られない場所かもしれません)" in t
    assert "変わるとすぐに気づけます" in t
    assert page.pill.text().endswith("新しい物 1 件")
    assert page.table.rowCount() == 3
    assert page.table.item(0, 1).text() == "NewApp" and page.table.item(0, 0).text() == "新しい"
    _button(page, "スタートアップの設定を開く").click()
    _button(page, "タスク マネージャーを開く").click()
    assert opened == ["ms-settings:startupapps", "taskmgr.exe"]
    _button(page, "確かめた").click()
    qapp.processEvents()
    assert m.new_count() == 0
    assert "新しく増えた物はありません" in _texts(page)
    page.table.selectRow(0)
    assert "OneDriveSample" in page.detail.text() or "NewApp" in page.detail.text()
    page.close()


def test_page_baseline_notice_and_snooze(qapp: Any, make_module: Any, tmp_path: Any) -> None:
    api = sample_api()
    ctx = FakeCtx(tmp_path / "p2")
    m, _, _ = make_module(api, ctx)
    m.start()
    page = m.create_page()
    assert "今ある 2 個を覚えました。これから増えたら知らせます。" in page.notice_label.text()
    ctx.snoozed = True
    m.notifier.changed.emit()
    assert page.pill.text().endswith("一時停止中は見ていません")


def test_page_settings_toggles_write(qapp: Any, make_module: Any) -> None:
    api = sample_api()
    m, ctx, _ = make_module(api)
    m.start()
    page = m.create_page()
    page.sw_runonce.click()
    assert m.cfg["notify_runonce"] is True and ctx.writes[-1][0]["notify_runonce"] is True
    page.poll.setValue(30)
    page.poll.editingFinished.emit()
    assert m.cfg["poll_minutes"] == 30 and ctx.writes[-1][1] is True


def test_page_folder_item_has_open_folder_button(qapp: Any, make_module: Any) -> None:
    api = sample_api()
    folder = api.folders["startup"]
    api.add_file(folder, "Tool.lnk", r"C:\T\tool.exe")
    m, _, opened = make_module(api)
    m.start()
    page = m.create_page()
    w = None
    for r in range(page.table.rowCount()):
        if page.table.item(r, 1).text() == "Tool":
            w = page.table.cellWidget(r, 5)
    assert isinstance(w, QPushButton)
    w.click()
    assert opened == [folder]
