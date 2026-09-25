# モジュール結合と GUI の煙テスト(offscreen)。AC-9(選択後は行数不変・last_used_at だけ更新)、AC-16(ゲーム時は
# パレットを開かず palette_blocked)、D-11(除外形式の引き継ぎ)、全消去、パレットのキー操作、設定画面に履歴本文が出ないこと。
from __future__ import annotations

import logging
from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QLineEdit, QListWidget, QPlainTextEdit, QTableWidget, QWidget

from deskkit.foreground import ForegroundInfo
from deskkit.modules.clipshelf import policy
from deskkit.modules.clipshelf._win32 import FMT_EXCLUDE_MONITOR, FMT_ORIGIN, WM_CLIPBOARDUPDATE
from deskkit.modules.clipshelf.writer import decode_origin

MARKER = "HISTORY-BODY-MARKER-9f3"


class _Errors(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def errors() -> Any:
    h = _Errors()
    lg = logging.getLogger("deskkit.clipshelf")
    lg.addHandler(h)
    yield h
    lg.removeHandler(h)


def _copy(m: Any, api: Any, text: str, **kw: Any) -> Any:
    api.put(text, **kw)
    return m.monitor.process()


def test_start_defaults_hotkeys_tray(module_factory: Any) -> None:
    m, ctx, api, _ = module_factory()
    sec, restart = ctx.writes[0]
    assert sec["mode"] == "observe" and sec["hotkeys"]["open_palette"] == "Ctrl+Alt+V" and not restart
    assert sec["exclude_exes"] == [] and sec["unknown_owner_policy"] == "skip" and sec["auto_paste"] == "off"
    assert ctx.hotkeys.registered == {"open_palette": "Ctrl+Alt+V"}
    assert WM_CLIPBOARDUPDATE in ctx.native
    assert [t.label for t in ctx.tray][:3] == ["パレットを開く", "記録を一時停止", "書式なしにする"]
    assert ctx.status == "観察モード(記録しない)"
    assert m.store is not None and (ctx.data_dir / "clipshelf.db").exists()
    assert m.handle_cli(["nope"]) == (2, "unsupported")
    assert m.handle_cli(["status"])[1].startswith("mode=observe")


def test_observe_mode_records_nothing(module_factory: Any) -> None:
    m, ctx, api, _ = module_factory()
    for i in range(5):
        d = _copy(m, api, f"t{i}")
        assert d.reason == policy.OBSERVE_ONLY
    assert m.store.row_count() == 0 and api.text_reads == 0


def test_ac9_choose_touches_only(module_factory: Any) -> None:
    m, ctx, api, clock = module_factory({"mode": "record"})
    d = _copy(m, api, "hello")
    item = m.store.get(d.item_id)
    before = item.last_used_at
    clock.advance(minutes=10)
    m.choose(item)
    assert api.text_now() == "hello" and decode_origin(api.clip[api.fmt_id(FMT_ORIGIN)]) == item.id
    d2 = m.monitor.process()  # 自分の書き込みによる WM_CLIPBOARDUPDATE
    assert d2.reason == policy.SELF_ORIGIN
    assert m.store.row_count() == 1
    assert m.store.get(item.id).last_used_at > before
    # パレットを開いていない(復帰先なし)ので通知で知らせる
    assert ctx.notifications[-1][1] == "クリップボードに置きました"


def test_choose_restores_foreground(module_factory: Any, errors: Any) -> None:
    m, ctx, api, _ = module_factory({"mode": "record"})
    item = m.store.get(_copy(m, api, "abc").item_id)
    m.open_palette()
    assert m._palette is not None and m._palette.isVisible()
    m.choose(item)
    assert api.set_fg_calls == 1 and api.fg_hwnd == ctx.fg.hwnd
    assert not m._palette.isVisible()
    assert not errors.records


@pytest.mark.parametrize(
    ("fg", "reason"),
    [
        (ForegroundInfo(0x7001, 5, "game.exe", True, False, False), "game"),
        (ForegroundInfo(0x7001, 5, "video.exe", False, True, False), "fullscreen"),
        (ForegroundInfo(0x7001, 5, "video.exe", False, None, False), "fullscreen_unknown"),
    ],
)
def test_ac16_palette_blocked(module_factory: Any, fg: ForegroundInfo, reason: str) -> None:
    m, ctx, api, _ = module_factory(fg=fg)
    ctx.hotkeys.press("open_palette")
    assert m._palette is None
    ops = m.ops.read()
    assert ops[-1]["op"] == "palette_blocked" and ops[-1]["reason"] == reason
    assert not any(o["op"] == "palette_open" for o in ops)


def test_snippet_clipboard_inherits_exclusion(module_factory: Any) -> None:
    m, ctx, api, _ = module_factory({"mode": "record"})
    snip = m.save_snippet(None, "包む", "[{clipboard}] {{ok}}")
    api.put("pw", formats={FMT_EXCLUDE_MONITOR: b"\x01\x00\x00\x00"})
    m.choose(snip)
    assert api.text_now() == "[pw] {ok}"
    assert FMT_EXCLUDE_MONITOR in api.format_names_now()  # D-11 / FR-17
    assert m.monitor.process().reason == policy.SELF_ORIGIN


def test_clear_all_from_module(module_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from deskkit.ui import widgets

    m, ctx, api, clock = module_factory({"mode": "record"})
    for i in range(4):
        clock.advance(minutes=1)
        _copy(m, api, f"x{i}")
    m.store.set_pinned(m.store.history()[0].id, True)
    m.save_snippet(None, "s", "t")
    monkeypatch.setattr(widgets, "confirm", lambda *a, **k: (True, [False, True]))
    m.clear_all_interactive(None)
    assert m.counts() == {"history": 1, "pinned": 1, "snippets": 1, "undecryptable": 0}
    assert api.clip == {}  # 現在のクリップボードも空にした
    op = [o for o in m.ops.read() if o["op"] == "clear_all"][-1]
    assert op["deleted"] == 3 and op["pins_deleted"] is False


def test_plain_text_hotkey_and_pause(module_factory: Any) -> None:
    m, ctx, api, _ = module_factory({"mode": "record", "hotkeys": {"open_palette": "Ctrl+Alt+V", "plain_text": "Ctrl+Alt+T",
                                                                    "toggle_pause": "Ctrl+Alt+P"}})
    api.put("text", formats={"HTML Format": b"<i>t</i>"})
    ctx.hotkeys.press("plain_text")
    assert "HTML Format" not in api.format_names_now() and api.text_now() == "text"
    ctx.hotkeys.press("toggle_pause")
    assert m.paused and ctx.status == "一時停止中"
    assert _copy(m, api, "while paused").reason == policy.PAUSED
    assert not ctx.errors


def test_palette_keys(module_factory: Any, qapp: Any, errors: Any) -> None:
    m, ctx, api, clock = module_factory({"mode": "record"})
    for i in range(5):
        clock.advance(minutes=1)
        _copy(m, api, f"alpha {i}\n二行目 {i}")
    m.save_snippet(None, "署名", "よろしく {date}")
    m.open_palette()
    pal = m._palette
    assert pal is not None and pal.isVisible()
    assert pal.model.rowCount() == 5 and pal.tabs.index() == 0
    QTest.keyClicks(pal.search, "alpha 3")
    assert pal.model.rowCount() == 1
    pal.search.clear()
    QTest.keyClick(pal.search, Qt.Key.Key_Down)
    assert pal.view.currentIndex().row() == 1
    target = pal.current_item()
    QTest.keyClick(pal.search, Qt.Key.Key_P, Qt.KeyboardModifier.ControlModifier)
    assert m.store.get(target.id).pinned
    assert pal.current_item().id == target.id and pal.model.item_at(0).id == target.id  # ピンが先頭に来る
    QTest.keyClick(pal.search, Qt.Key.Key_Tab)
    assert pal.tabs.index() == 1 and pal.model.rowCount() == 1
    assert "2026-10-01" in pal.pv_text.toPlainText()
    QTest.keyClick(pal.search, Qt.Key.Key_Tab)
    assert pal.tabs.index() == 0
    QTest.keyClick(pal.search, Qt.Key.Key_Return)
    assert api.text_now() == target.text
    assert not pal.isVisible()
    m.open_palette()
    QTest.keyClick(pal.search, Qt.Key.Key_Escape)
    pal.dismiss(animated=False)
    assert not errors.records


def _all_texts(root: QWidget) -> list[str]:
    out: list[str] = []
    for w in [root, *root.findChildren(QWidget)]:
        if isinstance(w, QLabel):
            out.append(w.text())
            out.append(w.toolTip())
        elif isinstance(w, QLineEdit):
            out.append(w.text())
        elif isinstance(w, QPlainTextEdit):
            out.append(w.toPlainText())
        elif isinstance(w, QTableWidget):
            for r in range(w.rowCount()):
                for c in range(w.columnCount()):
                    it = w.item(r, c)
                    if it is not None:
                        out.append(it.text())
        elif isinstance(w, QListWidget):
            out.extend(w.item(i).text() for i in range(w.count()))
    return out


def test_page_smoke_and_no_history_text(module_factory: Any, qapp: Any, errors: Any) -> None:
    m, ctx, api, _ = module_factory({"mode": "record", "exclude_exes": ["Blocked.EXE"]})
    _copy(m, api, f"{MARKER} 本文")
    _copy(m, api, "secret", formats={FMT_EXCLUDE_MONITOR: b"\x00"})
    m.save_snippet(None, "挨拶", "こんにちは {date}")
    page = m.create_page()
    page.resize(1100, 900)
    page.show()
    qapp.processEvents()
    texts = "\n".join(_all_texts(page))
    assert MARKER not in texts  # 履歴の本文は設定画面に出さない
    assert "exclude_format" in texts and "recorded" in texts
    assert "挨拶" in texts  # 定型文の管理は画面で行う
    assert page.t_hist.value.text() == "1" and page.t_excl.value.text() == "1"
    # モード切替で即保存
    page.mode_seg.set_value("observe", emit=True)
    assert ctx.writes[-1][0]["mode"] == "observe" and m.config.mode == "observe"
    # 除外アプリの追加
    page.excl_editor.add_value("C:\\Apps\\Other.exe")
    assert ctx.writes[-1][0]["exclude_exes"] == ["blocked.exe", "other.exe"]
    # 定型文の新規保存
    page._new_snippet()
    page.snip_editor.set_values("署名", "{time} 送信")
    page._save_snippet()
    assert {s.name for s in m.store.snippets()} == {"挨拶", "署名"}
    page.close()
    assert not errors.records and not ctx.errors
