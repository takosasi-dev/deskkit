# v0.4.1 B-3: JotDrop の小さな直し。
# - 「ファイル名」の説明の日付を、固定の日付ではなくモジュールの時計の今日で出す(日付が変わったら書き直す)。
# - 設定の確かめ(normalize の file_pattern)にモジュールの時計を使う(本物の時計に頼らない)。
# - キーの欄を同時に 2 つ出さない(未設定なら呼びかけのカードだけ、設定済みなら「キー」のカードだけ)。
from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from deskkit.modules.jotdrop import compose
from deskkit.modules.jotdrop import module as module_mod


def test_pattern_hint_uses_module_clock_and_follows_date_change(make_module: Any) -> None:
    m, _ctx, _a, clock = make_module(when=datetime(2031, 1, 2, 23, 59, 30))
    page = m.create_page()
    assert "2031-01-02 の形" in page.pattern_hint.text()
    assert "2026-09-26" not in page.pattern_hint.text()
    clock.advance(60)  # 日付が変わる
    m.notifier.changed.emit()
    assert "2031-01-03 の形" in page.pattern_hint.text()
    page.close()


def test_normalize_validates_pattern_with_module_clock(make_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[datetime] = []
    real = compose.validate_pattern

    def spy(pattern: str, today: datetime) -> str | None:
        seen.append(today)
        return real(pattern, today)

    monkeypatch.setattr(module_mod.compose, "validate_pattern", spy)
    m, _ctx, _a, clock = make_module(when=datetime(2030, 5, 6, 23, 59, 50))
    assert seen and seen[-1] == datetime(2030, 5, 6, 23, 59, 50)  # 起動時の確かめ
    clock.advance(20)  # 日付が変わる
    seen.clear()
    assert m.save_settings({"max_chars": 500}) is None
    assert seen and seen[-1].date() == datetime(2030, 5, 7).date()  # 保存のあとの確かめも時計の今日


def test_key_card_and_banner_are_never_shown_together(make_module: Any) -> None:
    m, ctx, _a, _c = make_module()  # キー未設定
    page = m.create_page()
    page.resize(1000, 800)
    page.show()
    QApplication.processEvents()
    assert page.banner.isVisibleTo(page) and not page.key_card.isVisibleTo(page)
    page._on_hotkey("Ctrl+Alt+J")  # 設定する(host では起動し直しで画面を作り直すが、この画面も切り替わること)
    assert ctx.settings()["hotkey"] == "Ctrl+Alt+J"
    assert not page.banner.isVisibleTo(page) and page.key_card.isVisibleTo(page)
    assert page.key_edit.text() == "Ctrl+Alt+J"
    page.close()


def test_registered_failed_key_shows_only_key_card_with_reason(make_module: Any) -> None:
    m, _ctx, _a, _c = make_module({"hotkey": "Ctrl+F12"})  # 設定済みだが使えないキー
    page = m.create_page()
    assert m.hotkey_ok is False
    assert not page.banner.isVisibleTo(page) and page.key_card.isVisibleTo(page)
    assert "F12" in page.key_msg.text() and page.key_msg.isVisibleTo(page)
    page.close()


def test_key_set_at_start_shows_only_key_card(make_module: Any) -> None:
    m, _ctx, _a, _c = make_module({"hotkey": "Ctrl+Alt+J"})
    page = m.create_page()
    assert m.hotkey_ok is True
    assert not page.banner.isVisibleTo(page) and page.key_card.isVisibleTo(page)
    page.close()
