# PagePress の画面(offscreen): タブ・投入・一覧の操作・範囲の入力・整理の格子・確認(P-6・FR-9)・実行中の表示(FR-13・FR-14)。
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from deskkit.modules.pagepress import jobs as J
from tests.modules.pagepress import samples
from tests.modules.pagepress.conftest import FakeCtx, add_and_wait, job_done, make_module, pump


@pytest.fixture
def page_env(qapp: Any, ctx: FakeCtx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from deskkit.modules.pagepress import page as P

    answers: list[bool] = []
    asked: list[str] = []

    def fake_confirm(parent: Any, title: str, text: str, **k: Any) -> tuple[bool, list[bool]]:
        asked.append(text)
        return (answers.pop(0) if answers else True), []

    msgs: list[str] = []
    monkeypatch.setattr(P.W, "confirm", fake_confirm)
    monkeypatch.setattr(P.W, "message", lambda parent, title, text, **k: msgs.append(text))
    m = make_module(ctx, tmp_path)
    m.start()
    pg = m.create_page()
    pg.resize(1000, 900)
    pg.show()
    qapp.processEvents()

    class Env:
        pass

    env = Env()
    env.m, env.page, env.answers, env.asked, env.msgs, env.ctx = m, pg, answers, asked, msgs, ctx  # type: ignore[attr-defined]
    yield env
    pg.close()
    pg.deleteLater()
    m.stop()


def _pump_ui(qapp: Any, ctx: FakeCtx, pred: Any, timeout: float = 30.0) -> None:
    def p() -> bool:
        qapp.processEvents()
        return bool(pred())

    pump(ctx, p, timeout)


def test_tabs_and_last_tab(page_env: Any, qapp: Any) -> None:
    pg, m = page_env.page, page_env.m
    assert pg.current == "merge"
    pg.switch("compress")
    assert m.config.last_tab == "compress" and pg.t_compress.isVisibleTo(pg) and not pg.t_merge.isVisibleTo(pg)
    pg.switch("bogus")
    assert pg.current == "merge"


def test_merge_list_ops_and_confirm(page_env: Any, qapp: Any, tmp_path: Path) -> None:
    pg, m, ctx = page_env.page, page_env.m, page_env.ctx
    a = samples.form_pdf(tmp_path / "a.pdf", "name", "A")
    b = samples.form_pdf(tmp_path / "b.pdf", "name", "B")
    photo = tmp_path / "c.jpg"
    photo.write_bytes(samples.jpeg_bytes(200, 300))
    pg.add_to("merge", [str(a), str(b), str(photo)])
    _pump_ui(qapp, ctx, lambda: m.probing["merge"] == 0 and pg.t_merge.list.count() == 3)
    t = pg.t_merge
    assert t.img_card.isVisibleTo(pg) and t.run_btn.isEnabled()
    assert "3 件" in t.total.text()
    t.list.item(2).setSelected(True)
    t._move(-1)
    assert [e.name for e in m.lists["merge"]] == ["a.pdf", "c.jpg", "b.pdf"]
    # 行のサムネイルが描画スレッドから届く
    _pump_ui(qapp, ctx, lambda: len(m.thumbs) >= 3)
    page_env.answers.append(False)  # P-6 で「やめる」
    pg.run_merge()
    assert page_env.asked and "入力欄の名前が重なっています" in page_env.asked[-1]
    assert m.job is None and not list(tmp_path.glob("*_まとめ*"))
    page_env.answers.append(True)
    pg.run_merge()
    _pump_ui(qapp, ctx, lambda: job_done(m))
    qapp.processEvents()
    assert t.results_lay.count() == 1
    assert list(tmp_path.glob("a_まとめ.pdf"))


def test_rejected_notes_shown(page_env: Any, qapp: Any, tmp_path: Path) -> None:
    pg, m, ctx = page_env.page, page_env.m, page_env.ctx
    enc = samples.encrypted_pdf(tmp_path / "locked.pdf", user="pw")
    txt = tmp_path / "memo.txt"
    txt.write_text("x", encoding="utf-8")
    pg.add_to("merge", [str(enc), str(txt)])
    _pump_ui(qapp, ctx, lambda: m.probing["merge"] == 0)
    qapp.processEvents()
    text = pg.t_merge.notes.label.text()
    assert "locked.pdf" in text and "パスワード" in text and "対応していない形式: 1 件" in text


def test_split_range_validation(page_env: Any, qapp: Any, tmp_path: Path) -> None:
    pg, m, ctx = page_env.page, page_env.m, page_env.ctx
    pg.switch("split")
    a = samples.blank_pdf(tmp_path / "ten.pdf", 10)
    pg.add_to("split", [str(a)])
    _pump_ui(qapp, ctx, lambda: pg.t_split.list.count() == 1)
    t = pg.t_split
    t.range_edit.setText("5-3")
    assert t.range_err.text() == "5-3 の順が逆です" and not t.run_btn.isEnabled()
    t.range_edit.setText("11")
    assert t.range_err.text() == "10 ページまでしかありません" and not t.run_btn.isEnabled()
    t.range_edit.setText("あ")
    assert t.range_err.text() == "数字で書いてください"
    t.range_edit.setText("1-3, 5, 8-")
    assert t.range_err.text() == "" and t.run_btn.isEnabled() and "3 個" in t.run_hint.text()
    t.seg_mode.set_value("every", emit=True)
    t.spin.setValue(3)
    assert "4 個" in t.run_hint.text() and m.config.split_every == 3
    t.seg_mode.set_value("ranges", emit=True)
    pg.run_split()
    _pump_ui(qapp, ctx, lambda: job_done(m))
    assert sorted(p.name for p in (tmp_path / "ten_分割").iterdir()) == ["ten_1-3.pdf", "ten_5.pdf", "ten_8-10.pdf"]


def test_organize_grid_ops_and_unsaved(page_env: Any, qapp: Any, tmp_path: Path) -> None:
    pg, m, ctx = page_env.page, page_env.m, page_env.ctx
    a = samples.blank_pdf(tmp_path / "four.pdf", 4)
    pg.switch("organize")
    pg.add_to("organize", [str(a)])
    _pump_ui(qapp, ctx, lambda: m.organize_entry is not None)
    t = pg.t_organize
    assert t.model.rowCount() == 4 and t.view.isVisibleTo(pg)
    _pump_ui(qapp, ctx, lambda: all(m.page_thumb(i)[0] for i in range(4)))  # 見えている分が描かれる
    t._select([0])
    t._rotate(90)
    assert m.organize_state.items[0] == (0, 90)
    t._select([1])
    t._delete()
    assert [o for o, _r in m.organize_state.items] == [0, 2, 3]
    t._undo()
    assert len(m.organize_state.items) == 4
    t._redo()
    assert len(m.organize_state.items) == 3
    t._select([2])
    t._moved([2], 0)
    assert [o for o, _r in m.organize_state.items] == [3, 0, 2]
    assert t.dirty_pill.isVisibleTo(pg)
    # タブを移るとき、保存していない変更の確認(FR-9)。「戻る」
    page_env.answers.append(False)
    pg._seg_changed("merge")
    assert pg.current == "organize"
    # 別の PDF を開くときも確かめる。「戻る」なら開かない
    b = samples.blank_pdf(tmp_path / "other.pdf", 2)
    page_env.answers.append(False)
    pg.add_to("organize", [str(b)])
    assert m.organize_entry.name == "four.pdf"
    pg.run_organize()
    _pump_ui(qapp, ctx, lambda: job_done(m))
    qapp.processEvents()
    assert (tmp_path / "four_整理.pdf").exists() and not m.organize_dirty()
    assert not t.dirty_pill.isVisibleTo(pg)
    n_before = len(page_env.asked)
    pg._seg_changed("merge")  # 保存したので確かめない
    assert pg.current == "merge" and len(page_env.asked) == n_before


def test_organize_view_keys_and_drop_row(page_env: Any, qapp: Any, tmp_path: Path) -> None:
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    pg, m, ctx = page_env.page, page_env.m, page_env.ctx
    pg.switch("organize")
    pg.add_to("organize", [str(samples.blank_pdf(tmp_path / "six.pdf", 6))])
    _pump_ui(qapp, ctx, lambda: m.organize_entry is not None)
    t = pg.t_organize
    t._select([0, 1])
    t.view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Delete, Qt.KeyboardModifier.NoModifier))
    assert len(m.organize_state.items) == 4
    t.view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier))
    assert len(m.organize_state.items) == 6
    t.view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier))
    assert len(m.organize_state.items) == 4
    first = t.view.visualRect(t.model.index(0, 0))
    assert t.view.drop_row(first.center() + first.center() * 0) in (0, 1)
    f, last = t.view.visible_rows()
    assert f == 0 and last >= 3


def test_busy_state_other_tabs(page_env: Any, qapp: Any, tmp_path: Path) -> None:
    pg, m, ctx = page_env.page, page_env.m, page_env.ctx
    gate = threading.Event()
    entered = threading.Event()

    def on_write() -> None:
        entered.set()
        gate.wait(10)

    m.env.on_write = on_write
    a = samples.blank_pdf(tmp_path / "a.pdf", 3)
    add_and_wait(m, ctx, "merge", [a])
    add_and_wait(m, ctx, "compress", [a])
    qapp.processEvents()
    pg.run_merge()
    assert entered.wait(10)
    _pump_ui(qapp, ctx, lambda: pg.t_merge.progress.isVisibleTo(pg))
    assert not pg.t_compress.busy_note.isHidden()  # 「ほかの作業が終わるまでお待ちください」(FR-13)
    assert pg.t_merge.busy_note.isHidden()
    assert not pg.t_compress.run_btn.isEnabled() and not pg.t_merge.run_btn.isEnabled()
    assert pg.t_merge.progress.phase.text() == "書き出しています"
    pg.t_merge.progress.cancel.click()
    gate.set()
    _pump_ui(qapp, ctx, lambda: job_done(m))
    qapp.processEvents()
    assert m.job.state == J.STATE_CANCELLED
    assert pg.t_merge.error.label.text() == J.MSG_CANCELLED
    assert pg.t_compress.run_btn.isEnabled()


def test_sendto_toggle(page_env: Any, qapp: Any, tmp_path: Path) -> None:
    pg, m = page_env.page, page_env.m
    pg.sendto_toggle.setChecked(True)
    link = tmp_path / "SendTo" / "PagePress でまとめる.lnk"
    assert link.exists() and m.config.sendto_enabled
    assert "登録されています" in pg.sendto_note.text()
    pg.sendto_toggle.setChecked(False)
    assert not link.exists() and not m.config.sendto_enabled


def test_page_recreated_keeps_state(qapp: Any, ctx: FakeCtx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    m = make_module(ctx, tmp_path)
    m.start()
    add_and_wait(m, ctx, "merge", [samples.blank_pdf(tmp_path / "a.pdf", 1)])
    p1 = m.create_page()
    p1.deleteLater()
    p2 = m.create_page()
    assert p2.t_merge.list.count() == 1
    p2.deleteLater()
    m.stop()
