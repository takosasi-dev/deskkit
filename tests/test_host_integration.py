# 本物の Host(トレイ非表示・IPC なし)を offscreen で組み立て、v0.2 の本体機能と Control Center の画面を通しで確かめる。
# 設定・データは一時フォルダ(DESKKIT_HOME)。常駐アプリは起動しない。
from __future__ import annotations

import datetime as _dt
import json
import time
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


# ------------------------------------------------------------------ H2 一時停止: ctx・イベント・トレイ
def test_snooze_reaches_ctx_event_and_tray(host: Any, qapp: Any) -> None:
    ctx = host.loader.slots["_selftest_ok"].ctx
    got: list[dict[str, Any]] = []
    host.events.on("_selftest_ok", "host.snooze_changed", lambda p: got.append(dict(p)))
    assert ctx.is_snoozed() is False
    host.snooze_for(30)
    assert ctx.is_snoozed() is True
    assert got[-1]["snoozed"] is True and got[-1]["until"]
    assert "一時停止中" in host.tray.snooze_status.text()
    assert host.tray.resume_act.isEnabled()
    assert "一時停止中" in host.tray.icon.toolTip()
    host.resume()
    assert ctx.is_snoozed() is False and got[-1] == {"snoozed": False, "until": None}
    assert not host.tray.resume_act.isEnabled()


def test_list_modes_via_ctx(host: Any) -> None:
    host.settings.write_module("modeshift", {"enabled": False, "modes": [{"name": "g", "label": "ゲーム"}, {"x": 1}]})
    ctx = host.loader.slots["_selftest_ok"].ctx
    assert ctx.list_modes() == [("g", "ゲーム")]


# ------------------------------------------------------------------ H1 保留は本物の notify から
def test_notify_hold_records_activity_immediately(host: Any, qapp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    shown: list[Any] = []
    monkeypatch.setattr(host, "_show_note", shown.append)
    busy = {"v": True}
    monkeypatch.setattr(host, "_foreground_busy", lambda: busy["v"])
    n0 = len(host.activities)
    host.notify("dropsort", "移動しました", "本文", None)
    host.notify("layoutkeep", "配置を戻しました", "", None, level="warn")
    host.notify("clipshelf", "壊れています", "", None, level="error")
    assert len(host.activities) == n0 + 3  # 記録はすぐ
    assert [n.title for n in shown] == ["壊れています"]  # error だけすぐ出る
    busy["v"] = False
    host.hold._tick()
    assert shown[-1].title == "保留中の通知 2 件" and shown[-1].level == "warn"
    assert "DropSort: 移動しました" in shown[-1].text


def test_hold_setting_off(host: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    shown: list[Any] = []
    monkeypatch.setattr(host, "_show_note", shown.append)
    monkeypatch.setattr(host, "_foreground_busy", lambda: True)
    host.settings.write_host({"hold_notifications": False})
    host.notify("dropsort", "x", "", None)
    assert len(shown) == 1


# ------------------------------------------------------------------ H3 世代
def test_settings_writes_make_generations_and_restore(host: Any, qapp: Any) -> None:
    first = host.snapshots.list()
    assert len(first) == 1  # 起動時の世代
    host.settings.write_game_processes(["a.exe"])
    host.settings.write_game_processes(["a.exe", "b.exe"])
    assert host._snap_timer.isActive()  # 2 秒まとめる
    host.snapshot_now()
    snaps = host.snapshots.list()
    assert len(snaps) == 2
    assert host.snapshots.read(snaps[0])["game_processes"] == ["a.exe", "b.exe"]
    host.restore_snapshot(snaps[1])
    _pump(qapp)
    assert host.settings.game_processes() == frozenset()
    assert len(host.snapshots.list()) == 3  # 戻す前の設定も残る
    assert host.loader.slots["_selftest_ok"].state == "running"


# ------------------------------------------------------------------ H4 診断
def test_copy_diagnostics(host: Any, qapp: Any) -> None:
    from PySide6.QtGui import QGuiApplication

    host.copy_diagnostics()
    text = QGuiApplication.clipboard().text()
    assert "DeskKit 診断レポート" in text and "_selftest_ok" not in text
    assert str(Path.home()).lower() not in text.lower()


# ------------------------------------------------------------------ Control Center
def test_home_page_ux(host: Any, qapp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    from deskkit.ui import main_window as mw

    host.show_window()
    _pump(qapp)
    cc = host._window
    home = cc.home
    # UX-2 挨拶は refresh で時刻から作り直す
    monkeypatch.setattr(mw, "greeting", lambda now=None: "こんばんは")
    home.refresh()
    assert home.hero.title_label.text() == "こんばんは"
    # UX-3 ホットキー: 表示名・組み合わせ・「他 N 件」
    for i in range(14):
        host.hotkeys.combos[f"dropsort.k{i}"] = (0x2, 0x41 + i)
    monkeypatch.setattr(host.hotkeys.registry, "names", lambda: ["host.quick", *[f"dropsort.k{i}" for i in range(14)]])
    host.hotkeys.combos["host.quick"] = (0x3, 0x20)
    home.refresh()
    texts = _texts(home.keys)
    assert "クイックアクション" in texts and "Ctrl+Alt+Space" in texts and "他 3 件" in texts
    # UX-6 カード全体のクリックで画面を開く
    opened: list[str] = []
    card = home.cards["dropsort"]
    card._open_page = opened.append
    pos = QPointF(5, 5)
    for t in (QMouseEvent.Type.MouseButtonPress, QMouseEvent.Type.MouseButtonRelease):
        ev = QMouseEvent(t, pos, card.mapToGlobal(QPoint(5, 5)).toPointF(), Qt.MouseButton.LeftButton,
                         Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        qapp.sendEvent(card, ev)
    assert opened == ["dropsort"]
    # UX-4 通知の行クリック: 新しければ元の動作、古ければ送り主の画面
    hits: list[int] = []
    host.notify("dropsort", "新しい", "", lambda: hits.append(1))
    act = host.activities[0]
    host.open_activity(act)
    assert hits == [1]
    act.ts -= _dt.timedelta(minutes=11)
    shown: list[Any] = []
    monkeypatch.setattr(host, "show_window", lambda page=None: shown.append(page))
    host.open_activity(act)
    assert hits == [1] and shown == ["dropsort"]
    act.ts = _dt.datetime.now()
    act.alive = lambda: False  # 送り主が止まった
    host.open_activity(act)
    assert shown == ["dropsort", "dropsort"]


def _texts(w: Any) -> str:
    from PySide6.QtWidgets import QLabel

    return "\n".join(lb.text() for lb in w.findChildren(QLabel))


def test_settings_numeric_fields_are_debounced(host: Any, qapp: Any) -> None:
    host.show_window("settings")
    _pump(qapp)
    page = host._window.settings_page
    writes: list[dict[str, Any]] = []
    orig = host.settings.write_host
    host.settings.write_host = lambda v: (writes.append(dict(v)), orig(v))  # type: ignore[method-assign]
    for v in (6, 7, 8, 9):
        page.lim.setValue(v)
    assert writes == []
    page._adv_timer.setInterval(0)
    page._adv_timer.start()
    _pump(qapp)
    assert len(writes) == 1 and writes[0]["handler_error_limit"] == 9
    assert host.settings.host()["handler_error_limit"] == 9


def test_settings_page_rebuilt_after_restore(host: Any, qapp: Any) -> None:
    host.show_window("settings")
    _pump(qapp)
    old = host._window.settings_page
    host.reload()
    _pump(qapp)
    assert host._window.settings_page is not old
    assert host._window.pages["settings"] is host._window.settings_page


def test_usage_page_collects_off_thread_and_caches(host: Any, qapp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from deskkit.ui import usage_page

    main = threading.get_ident()
    calls: list[int] = []
    mod = host.loader.slots["_selftest_ok"].module

    def usage(days: int) -> list[UsageSeries]:
        calls.append(threading.get_ident())
        time.sleep(0.05)
        return [UsageSeries("x", "テスト", [1] * days, primary=True)]

    mod.usage = usage
    monkeypatch.setattr(usage_page.catalog, "MODULES", (*usage_page.catalog.MODULES,
                                                          usage_page.catalog.ModuleInfo("_selftest_ok", "Test", "", "#888888", "")))
    host.show_window("usage")
    page = host._window.usage
    _wait(qapp, lambda: not page.loading.isVisible() and 30 in page._cache)
    assert calls and all(t != main for t in calls)  # GUI スレッド以外で呼ぶ
    n = len(calls)
    page.view.set_value("table", emit=True)  # 表示の切替は集計し直さない
    _pump(qapp)
    assert len(calls) == n
    assert host.settings.host()["usage_view"] == "table"
    page.period.set_value("7", emit=True)
    _wait(qapp, lambda: 7 in page._cache)
    assert host.settings.host()["usage_period"] == "7"
    page.refresh()
    assert len(calls) == n + 1  # 7 日はキャッシュから


def test_usage_page_retries_thread_bound_module_on_gui(host: Any, qapp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from deskkit.ui import usage_page

    main = threading.get_ident()
    mod = host.loader.slots["_selftest_ok"].module

    def usage(days: int) -> list[UsageSeries]:
        if threading.get_ident() != main:
            raise RuntimeError("SQLite objects created in a thread can only be used in that same thread")
        return [UsageSeries("x", "GUI で集計", [2] * days)]

    mod.usage = usage
    monkeypatch.setattr(usage_page.catalog, "MODULES", (*usage_page.catalog.MODULES,
                                                          usage_page.catalog.ModuleInfo("_selftest_ok", "Test", "", "#888888", "")))
    host.show_window("usage")
    page = host._window.usage
    _wait(qapp, lambda: 30 in page._cache)
    assert page._cache[30][2]["_selftest_ok"][0].label == "GUI で集計"


def _wait(app: Any, cond: Any, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("時間内に終わりませんでした")


# ------------------------------------------------------------------ UX-1 ホイール
def test_wheel_guard_passes_wheel_to_scroll_area(qapp: Any) -> None:
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QSpinBox

    from deskkit.ui import wheel_guard
    from deskkit.ui.widgets import ScrollPage

    wheel_guard.install(qapp)
    page = ScrollPage()
    spin = QSpinBox()
    spin.setRange(0, 100)
    spin.setValue(50)
    for _ in range(40):
        page.add(QSpinBox())
    page.add(spin)
    page.finish()
    page.resize(400, 300)
    page.show()
    _pump(qapp)
    assert spin.focusPolicy() == Qt.FocusPolicy.StrongFocus  # ホイールでフォーカスを取らない

    def wheel() -> None:
        ev = QWheelEvent(QPointF(5, 5), QPointF(spin.mapToGlobal(QPoint(5, 5))), QPoint(0, 0), QPoint(0, -120),
                         Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        qapp.sendEvent(spin, ev)
        _pump(qapp)

    before = page.verticalScrollBar().value()
    wheel()
    assert spin.value() == 50
    assert page.verticalScrollBar().value() > before  # ページがスクロールした
    spin.setFocus()
    _pump(qapp)
    if spin.hasFocus():  # offscreen でフォーカスが取れる環境だけ
        wheel()
        assert spin.value() != 50
    page.deleteLater()
