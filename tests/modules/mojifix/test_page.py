# 画面(offscreen): 横幅・ドロップの振り分け・候補のカードと「書き出す」(AC-2)・FR-11/12 の問い・zip の一覧・
# 名前の濁点の表(選ぶ・直す・元に戻す)・進捗の表示。ダイアログは差し替え口で答える。
from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any

from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtTest import QTest

from deskkit.modules.mojifix import textfix as TF

from .conftest import ADDRESS, raw_zip, settle


def _drop(page: Any, paths: list[Path]) -> None:
    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    ev = QDropEvent(QPointF(50, 50), Qt.DropAction.CopyAction, md, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    page.dropEvent(ev)


def test_page_text_flow(qapp: Any, make_module: Any, tmp_path: Path) -> None:
    m, ctx = make_module()
    page = m.create_page()
    page.resize(900, 900)
    page.show()
    assert page.minimumSizeHint().width() <= 900
    src = tmp_path / "売上.csv"
    src.write_bytes(ADDRESS.encode("cp932"))
    _drop(page, [src])
    settle(m, ctx)
    QTest.qWait(20)
    assert page.text_list.count() == 1
    assert len(page._cards) == 6 and page.cand_box.count() == 3 and page.more_box.count() == 3
    assert page._cards[0].key == "sjis"
    assert not page.export_btn.isEnabled()                      # AC-2
    assert "クリック" in page.export_hint.text()
    page._cards[0].clicked.emit("sjis")
    assert page.export_btn.isEnabled()
    assert page.preview.toPlainText().splitlines()[0] == "氏名,住所,電話番号"
    assert page.minimumSizeHint().width() <= 900
    page.export_btn.click()
    settle(m, ctx)
    assert page.text_result.isVisibleTo(page) and "売上_文字直し.csv" in page.text_result.label.text()
    assert page.open_out_btn.isVisibleTo(page)
    page._toggle_more()
    assert all(page.more_box.itemAt(i).widget().isVisibleTo(page) for i in range(page.more_box.count()))
    page.close()


def test_page_ask_hooks(qapp: Any, make_module: Any, tmp_path: Path) -> None:
    m, ctx = make_module({"text_output": "sjis"})
    page = m.create_page()
    asked: list[tuple[str, int]] = []

    def answer(kind: str, chk: TF.CheckResult) -> str:
        asked.append((kind, chk.unencodable))
        return "geta"

    page.ask_choice = answer
    src = tmp_path / "e.txt"
    src.write_bytes("a—b\n".encode())
    m.add_paths([src])
    settle(m, ctx)
    m.choose("utf8")
    page.export_btn.click()
    settle(m, ctx)
    assert asked == [("unencodable", 1)]
    assert m.text_out is not None and m.text_out.path.read_bytes().decode("cp932") == "a〓b\n"
    html = page._issue_html([TF.IssueLine(3, (("x<", False), ("—", True)))])
    assert "x&lt;" in html and "3 行目" in html
    page.close()


def test_page_zip_and_switch(qapp: Any, make_module: Any, tmp_path: Path) -> None:
    m, ctx = make_module()
    page = m.create_page()
    page.show()
    z = tmp_path / "w.zip"
    z.write_bytes(raw_zip([("表示/ソフト.txt".encode("cp932"), b"x"), (b"../evil.txt", b"y")]))
    _drop(page, [z])
    settle(m, ctx)
    QTest.qWait(20)
    assert page._area == "zip" and page.seg.value() == "zip"
    assert len(page._zip_cards) == 3 and not page.extract_btn.isEnabled()
    page._zip_cards[[c.key for c in page._zip_cards].index("sjis")].clicked.emit("sjis")
    settle(m, ctx)
    assert page.plan_model.rowCount() == 2
    assert page.plan_model.data(page.plan_model.index(1, 2)) == "飛ばす: 上のフォルダを指す名前"
    assert page.extract_btn.isEnabled()
    page.extract_btn.click()
    settle(m, ctx)
    assert page.zip_result.isVisibleTo(page) and page.zip_open_btn.isVisibleTo(page)
    assert (tmp_path / "w" / "表示" / "ソフト.txt").is_file() and not (tmp_path / "evil.txt").exists()
    page.close()


def test_page_rename(qapp: Any, make_module: Any, tmp_path: Path) -> None:
    m, ctx = make_module()
    page = m.create_page()
    page.show()
    d = tmp_path / "p"
    d.mkdir()
    nfd = unicodedata.normalize("NFD", "がぎ.txt")
    (d / nfd).write_text("x")
    (d / "げ.txt").write_text("y")
    (d / unicodedata.normalize("NFD", "げ.txt")).write_text("z")
    _drop(page, [d])
    assert page._area == "rename"
    page._scan()
    settle(m, ctx)
    model = page.rn_model
    assert model.rowCount() == 2
    states = {model.data(model.index(r, 1)): model.data(model.index(r, 4)) for r in range(2)}
    assert states[nfd] == "直せます" and states[unicodedata.normalize("NFD", "げ.txt")] == "同じ名前がすでにあります"
    assert "&#9676;" in model.data(model.index(0, 1), Qt.ItemDataRole.UserRole)
    fix_row = next(r for r in range(2) if model.data(model.index(r, 1)) == nfd)
    assert model.data(model.index(fix_row, 0), Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
    model.setData(model.index(fix_row, 0), Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
    assert not page.rename_btn.isEnabled()
    model.setData(model.index(fix_row, 0), Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
    assert page.rename_btn.isEnabled() and "選んだ 1 件" in page.rename_btn.text()
    asked: list[str] = []
    page.confirm = lambda title, text, ok: (asked.append(text), True)[1]
    page._rename()
    settle(m, ctx)
    assert asked and "ほかのソフトが前の名前で覚えていると" in asked[0]
    assert "がぎ.txt" in {p.name for p in d.iterdir()}
    assert page.undo_btn.isVisibleTo(page) and page.undo_note.text() == "DeskKit を閉じると元に戻せなくなります"
    page._undo()
    settle(m, ctx)
    assert nfd in {p.name for p in d.iterdir()} and not page.undo_btn.isVisibleTo(page)
    page.close()


def test_page_drive_root_needs_confirm(qapp: Any, make_module: Any, tmp_path: Path) -> None:
    m, ctx = make_module()
    page = m.create_page()
    m.set_folder(Path("C:\\"))
    asked: list[str] = []
    page.confirm = lambda title, text, ok: (asked.append(text), False)[1]
    page._scan()
    assert asked == ["時間がかかります。続けますか"] and not m.busy()
    page.close()


def test_page_progress_text(qapp: Any, make_module: Any) -> None:
    m, _ctx = make_module()
    page = m.create_page()
    page._show_progress("text", "write", 50, 100)
    assert page.prog_label.text() == "書き出しています(50%)"
    page._show_progress("scan", "scan", 1234, 0)
    assert page.prog_label.text() == "調べています(1,234 項目)"
    page.close()


def test_page_recreated(qapp: Any, make_module: Any) -> None:
    m, _ctx = make_module()
    p1 = m.create_page()
    p1.deleteLater()
    QTest.qWait(10)
    p2 = m.create_page()
    m.signals.changed.emit("text")  # 古い画面が消えていても例外にならない
    p2.close()
