# v0.4.1 の本体の画面の直し(docs/INTERFACES_v0.4.1.md §3 A): サイドバー・ホームのカード・利用状況の空の欄・表示名・FadeStack。
# offscreen で組み立てて大きさと並びを確かめる(見た目は実際の Windows の描画で撮って確かめる。docs/v0.4.1/ui.md)。
from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from deskkit.catalog import MODULE_NAMES
from deskkit.usage import UsageSeries


def _pump(app: Any, n: int = 20) -> None:
    for _ in range(n):
        app.processEvents()


@pytest.fixture
def host(qapp: Any, home: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    from deskkit import paths
    from deskkit.host import Host

    settings = {"version": 1, "host": {"quick_action_hotkey": ""},
                "modules": {**{n: {"enabled": False} for n in MODULE_NAMES}, "_selftest_ok": {"enabled": True}}}
    p = paths.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(settings), encoding="utf-8")
    h = Host(qapp, "per_monitor_aware_v2", start_ipc=False, show_tray=False)
    monkeypatch.setattr(h, "_foreground_busy", lambda: False)
    h.start()
    _pump(qapp)
    yield h
    h.loader.stop_all()
    h.hotkeys.unregister_all()
    if h._window is not None:
        h._window.deleteLater()
    h.native.close_native()
    _pump(qapp)


def _window(host: Any, qapp: Any, page: str, w: int = 1220, h: int = 800) -> Any:
    host.show_window(page)
    win = host._window
    win.resize(w, h)
    _pump(qapp, 40)
    win.open_page(page, animate=False)
    _pump(qapp, 40)
    return win


# ------------------------------------------------------------------ 1. サイドバー
def test_sidebar_fits_default_height_without_scrolling(host: Any, qapp: Any) -> None:
    win = _window(host, qapp, "plugsave")  # いちばん下のモジュールを選んでも
    sb = win.side_scroll.verticalScrollBar()
    assert win.sidebar.minimumSizeHint().height() <= 800
    assert sb.maximum() == 0 and sb.value() == 0  # スクロールしない = 見出し(ロゴ・DeskKit・版)が切れない
    logs = win.sidebar.items["logs"]
    bottom = logs.mapTo(win.sidebar, logs.rect().bottomLeft()).y()
    assert bottom <= win.side_scroll.viewport().height()  # 最後の「ログ」まで見える
    win.hide()


def test_sidebar_still_scrolls_at_minimum_height(host: Any, qapp: Any) -> None:
    win = _window(host, qapp, "plugsave", 1000, 660)
    assert win.minimumSizeHint().height() <= 660
    item = win.sidebar.items["plugsave"]
    top = item.mapTo(win.sidebar, item.rect().topLeft()).y() - win.side_scroll.verticalScrollBar().value()
    assert top >= 0 and top + item.height() <= win.side_scroll.viewport().height()  # 選んだ項目は見える
    win.hide()


# ------------------------------------------------------------------ 2. ホームのカード
def test_card_columns() -> None:
    from deskkit.ui.main_window import card_columns

    assert card_columns(910) == 3  # 窓 1220px の中身の幅
    assert card_columns(690) == 2  # 窓 1000px(最小)
    assert card_columns(300) == 2  # どれだけ狭くても 2 列より減らさない(今までと同じ)


def test_home_cards_three_columns_and_short(host: Any, qapp: Any) -> None:
    win = _window(host, qapp, "home")
    home = win.home
    assert home.card_grid.columns == 3
    card = home.cards["modeshift"]
    assert card.height() <= 125  # 以前は 176px 以上(6 割前後)
    # 今ある物は残す: アイコン・名前・一言・状態・状態文・スイッチ・開く
    from PySide6.QtWidgets import QLabel

    texts = [lb.text() for lb in card.findChildren(QLabel)]
    assert "ModeShift" in texts and "作業モードをワンタッチで切り替え" in texts
    assert card.toggle.isVisible() and card.pill.isVisible() and card.open_btn.isVisible() and card.status.isVisible()
    assert card.status.full_text() == "オンにすると使えるようになります"
    # 15 枚が 5 段(3 列)に並ぶ
    ys = sorted({c.y() for c in home.cards.values()})
    assert len(ys) == 5
    # 狭くすると 2 列に戻す
    win.resize(1000, 660)
    _pump(qapp, 40)
    assert home.card_grid.columns == 2
    assert len({c.y() for c in home.cards.values()}) == 8
    win.hide()


def test_home_does_not_overflow_sideways_at_minimum_width(host: Any, qapp: Any) -> None:
    win = _window(host, qapp, "home", 1000, 660)
    home = win.home
    assert home.widget().width() <= home.viewport().width()  # 見出しの状態ピルが折り返すので横にはみ出さない
    assert home.horizontalScrollBar().maximum() == 0
    win.hide()


def test_flow_layout_wraps_and_skips_hidden(qapp: Any) -> None:
    from PySide6.QtWidgets import QLabel, QWidget

    from deskkit.ui.widgets import FlowLayout

    box = QWidget()
    fl = FlowLayout(8)
    box.setLayout(fl)
    labels = [QLabel("x" * 20) for _ in range(3)]
    for lb in labels:
        lb.setFixedSize(100, 20)
        fl.addWidget(lb)
    assert fl.sizeHint().width() == 3 * 100 + 2 * 8 and fl.heightForWidth(400) == 20
    assert fl.heightForWidth(220) == 20 + 8 + 20  # 2 つ目までで 1 行、3 つ目は次の行
    assert fl.minimumSize().width() == 100
    box.show()
    labels[2].hide()
    assert fl.heightForWidth(220) == 20  # 隠れている物は詰める
    box.hide()
    box.deleteLater()


def test_home_card_click_still_opens(host: Any, qapp: Any) -> None:
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    win = _window(host, qapp, "home")
    card = win.home.cards["dropsort"]
    opened: list[str] = []
    card._open_page = opened.append
    for t in (QMouseEvent.Type.MouseButtonPress, QMouseEvent.Type.MouseButtonRelease):
        ev = QMouseEvent(t, QPointF(5, 5), card.mapToGlobal(QPoint(5, 5)).toPointF(), Qt.MouseButton.LeftButton,
                         Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        qapp.sendEvent(card, ev)
    assert opened == ["dropsort"]
    win.hide()


def test_elided_label_keeps_full_text_in_tooltip(qapp: Any) -> None:
    from deskkit.ui.widgets import ElidedLabel

    lb = ElidedLabel("短い")
    lb.resize(200, 20)
    assert lb.text() == "短い" and lb.toolTip() == ""
    long = "とても長い状態の文。" * 10
    lb.set_full_text(long)
    lb.resize(120, 20)
    assert lb.text().endswith("…") and lb.text() != long
    assert lb.toolTip() == long and lb.full_text() == long
    assert lb.minimumSizeHint().width() == 0


# ------------------------------------------------------------------ 3. 利用状況の空の欄
def _series(values: list[int], label: str = "x") -> UsageSeries:
    return UsageSeries("k", label, values, primary=True)


@pytest.mark.parametrize("view", ["chart", "table"])
def test_usage_empty_modules_collapse_to_rows_at_bottom(host: Any, qapp: Any, view: str) -> None:
    from PySide6.QtWidgets import QLabel, QTableWidget

    from deskkit.ui import usage_page
    from deskkit.ui.widgets import Card

    win = _window(host, qapp, "usage")
    page = win.usage
    page.view.set_value(view, emit=False)
    results = {
        "modeshift": [_series([0] * 7)],               # 記録なし(0 だけ)
        "dropsort": [_series([0, 0, 3, 0, 0, 0, 1])],  # 記録あり
        "clipshelf": [],                                # 集計できる物が無い
        "pagepress": [_series([0] * 7), _series([0, 0, 0, 0, 0, 0, 2], "y")],  # 2 つめの指標にだけ記録
    }
    page._render(7, results)
    _pump(qapp)
    widgets = [page.box.itemAt(i).widget() for i in range(page.box.count())]
    assert len(widgets) == 3
    # 記録のあるものが上(catalog の順のまま: DropSort → PagePress)、無いものは下に 1 行ずつ
    head = [w for w in widgets if not isinstance(w, usage_page.EmptyList)]
    assert all(isinstance(w, Card) for w in head)
    assert ["DropSort" in "".join(lb.text() for lb in w.findChildren(QLabel)) for w in head] == [True, False]
    assert isinstance(widgets[-1], usage_page.EmptyList)
    rows = widgets[-1].rows
    assert [r.name for r in rows] == ["modeshift", "clipshelf"]
    for r in rows:
        texts = [lb.text() for lb in r.findChildren(QLabel)]
        assert usage_page.EMPTY_TEXT in texts
    assert "ModeShift" in [lb.text() for lb in rows[0].findChildren(QLabel)]
    # 空の行にはグラフも表も無い
    assert not widgets[-1].findChildren(usage_page.BarChart) and not widgets[-1].findChildren(QTableWidget)
    if view == "table":
        assert len(head[0].findChildren(QTableWidget)) == 1
    else:
        assert head[0].findChildren(usage_page.BarChart)
    win.hide()


def test_usage_has_records() -> None:
    from deskkit.ui.usage_page import has_records

    assert not has_records([])
    assert not has_records([_series([0, 0]), _series([0])])
    assert has_records([_series([0, 0]), _series([0, 1])])


# ------------------------------------------------------------------ 4. 表示名
def test_backup_summary_uses_display_names() -> None:
    from deskkit import backup

    lines = backup.summary({"modules": {"modeshift": {"enabled": True, "modes": [{}, {}]},
                                        "dropsort": {"enabled": False, "rules": [{}]},
                                        "clipshelf": {"enabled": True}}, "game_processes": []})
    assert lines[0] == "有効なモジュール: ModeShift, ClipShelf"
    assert "DropSort: 1 ルール" in lines and "ModeShift: 2 モード" in lines
    assert not any("modeshift" in s or "dropsort" in s or "clipshelf" in s for s in lines)


def test_loader_log_uses_display_name(host: Any, qapp: Any, caplog: pytest.LogCaptureFixture,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    from deskkit import catalog

    monkeypatch.setattr(catalog, "MODULES", (*catalog.MODULES, catalog.ModuleInfo("_selftest_ok", "SelfTest", "", "#888888", "")))
    with caplog.at_level(logging.INFO, logger="deskkit.host.loader"):
        host.loader.restart("_selftest_ok")
    msgs = [(r.name, r.getMessage()) for r in caplog.records]
    assert ("deskkit.host.loader", "SelfTest を起動しました") in msgs  # ロガー名は今のまま
    assert not any("_selftest_ok" in m for _n, m in msgs)


# ------------------------------------------------------------------ 5. FadeStack の高さ
def test_fade_stack_uses_only_current_page_height(qapp: Any) -> None:
    from PySide6.QtWidgets import QFrame, QScrollArea, QVBoxLayout, QWidget

    from deskkit.ui.widgets import FadeStack

    tall = QWidget()
    tall.setMinimumHeight(900)
    short = QWidget()
    sl = QVBoxLayout(short)
    card = QFrame()
    card.setMinimumHeight(60)
    sl.addWidget(card)
    stack = FadeStack()
    stack.addWidget(tall)
    stack.addWidget(short)
    stack.setCurrentWidget(tall)
    assert stack.minimumSizeHint().height() >= 900
    stack.switch_to(short)
    assert stack.minimumSizeHint().height() < 200 and stack.sizeHint().height() < 200
    # ページの中に置いたとき、短いページが長いページの高さまで伸びない
    area = QScrollArea()
    area.setWidgetResizable(True)
    inner = QWidget()
    il = QVBoxLayout(inner)
    il.addWidget(stack)
    il.addStretch(1)
    area.setWidget(inner)
    area.resize(400, 500)
    area.show()
    _pump(qapp, 40)
    assert inner.height() <= 500  # スクロールが要らない
    assert card.height() < 150  # カードが縦に伸びない
    stack.switch_to(tall)
    _pump(qapp, 40)
    assert inner.height() >= 900  # 長いページに戻せば、その高さでスクロールできる
    area.hide()
    area.deleteLater()
