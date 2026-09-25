# host の画面部品のテスト(offscreen)。ログ画面のスレッド安全性など。
from __future__ import annotations

import logging
import threading
import types
from typing import Any

import pytest

from deskkit.logging_setup import memory_tail


def _pump(app: Any, n: int = 20) -> None:
    for _ in range(n):
        app.processEvents()


def test_logs_page_touches_widgets_only_on_gui_thread(qapp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from deskkit.ui.main_window import LogsPage

    host = types.SimpleNamespace(open_logs=lambda: None)
    page = LogsPage(host)  # type: ignore[arg-type]
    main = threading.get_ident()
    threads: list[int] = []
    orig = LogsPage._append

    def spy(self: LogsPage, r: logging.LogRecord) -> None:
        threads.append(threading.get_ident())
        orig(self, r)

    monkeypatch.setattr(LogsPage, "_append", spy)

    def worker(i: int) -> None:
        for j in range(50):
            memory_tail.emit(logging.LogRecord(f"deskkit.w{i}", logging.INFO, __file__, 1, "msg %d", (j,), None))

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    _pump(qapp)
    assert len(threads) >= 200
    assert all(t == main for t in threads)
    page.deleteLater()
    _pump(qapp)


def test_logs_page_search_is_debounced(qapp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from deskkit.ui.main_window import LogsPage

    page = LogsPage(types.SimpleNamespace(open_logs=lambda: None))  # type: ignore[arg-type]
    calls: list[int] = []
    monkeypatch.setattr(page, "rebuild", lambda: calls.append(1))
    page._search_timer.timeout.disconnect()
    page._search_timer.timeout.connect(page.rebuild)
    for ch in "abcdef":
        page.search.setText(page.search.text() + ch)
    assert calls == []
    page._search_timer.setInterval(0)
    page._search_timer.start()
    _pump(qapp)
    assert calls == [1]
    assert page.source.itemText(1) == "DeskKit 本体"
