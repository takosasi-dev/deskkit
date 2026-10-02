# v0.4.1 B-5: モードの編集の一覧(左の列)とアクションの一覧で、行の窓が項目の高さより縮まないこと。
# theme の「QListWidget::item { padding }」で行の窓が縮み、2 行目(meeting など)の下が切れていた。
# 実機の描画の確認は docs/v0.4.1/fixes.md の PNG。ここでは offscreen で行の窓と項目の大きさを比べる。
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QListWidget  # noqa: E402

from deskkit.modules.modeshift.fakes import FakeCtx, fake_system, populate, sample_section  # noqa: E402
from deskkit.modules.modeshift.module import ModeShiftModule  # noqa: E402
from deskkit.ui import theme  # noqa: E402


@pytest.fixture(scope="module")
def app() -> QApplication:
    a = QApplication.instance() or QApplication([])
    theme.apply(a)  # type: ignore[arg-type]
    return a  # type: ignore[return-value]


def _rows_fit(lst: QListWidget) -> None:
    assert lst.count() > 0
    for r in range(lst.count()):
        it = lst.item(r)
        w = lst.itemWidget(it)
        rect = lst.visualItemRect(it)
        assert w is not None
        assert w.height() >= w.sizeHint().height(), (r, w.height(), w.sizeHint().height())
        assert w.height() >= rect.height(), (r, w.height(), rect.height())
        for lb in w.findChildren(QLabel):  # 2 行目の文字も行の中に収まる
            if lb.text():
                bottom = lb.mapTo(w, lb.rect().bottomLeft()).y()
                assert bottom < w.height(), (r, lb.text(), bottom, w.height())


# theme.set_mode("light") は catalog のアクセント色を書き換えて戻せない(ほかのテストに響く)ので、ここはダークだけ。
# 項目の padding は明暗で同じ。ライトは実機の撮影で確かめた(docs/v0.4.1/fixes.md)。
def test_mode_and_action_rows_are_not_clipped(app: QApplication, tmp_path: Path) -> None:
    sysm = fake_system()
    populate(sysm)
    ctx = FakeCtx(tmp_path / "data", sample_section(tmp_path), games=frozenset({"game.exe"}))
    mod = ModeShiftModule(ctx, backends=sysm.backends())
    mod.start()
    page = mod.create_page()
    page.resize(1220, 900)
    page.show()
    app.processEvents()
    ed = page.editor
    ed.modes.setCurrentRow(0)
    app.processEvents()
    _rows_fit(ed.modes)
    _rows_fit(ed.act_list)
    page.close()
    mod.stop()
