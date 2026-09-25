# ClipShelf のテスト共通部品(偽 ctx・一時フォルダの環境・QApplication)。実機のクリップボードと %LOCALAPPDATA% には触れない。
from __future__ import annotations

import copy
import logging
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from deskkit.foreground import ForegroundInfo  # noqa: E402
from deskkit.modules.clipshelf import config as cfgmod  # noqa: E402
from deskkit.modules.clipshelf.fakes import FakeCipher, FakeClock, FakeWin32  # noqa: E402
from deskkit.modules.clipshelf.monitor import ClipMonitor  # noqa: E402
from deskkit.modules.clipshelf.ops import OpsLog  # noqa: E402
from deskkit.modules.clipshelf.store import Store  # noqa: E402

SAFE_FG = ForegroundInfo(0x7001, 777, "notepad.exe", False, False, False)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 万一 paths を使うコードが走っても本番の %LOCALAPPDATA%\DeskKit を汚さない
    monkeypatch.setenv("DESKKIT_HOME", str(tmp_path / "home"))


class _ListHandler(logging.Handler):
    def __init__(self, sink: list[logging.LogRecord]) -> None:
        super().__init__(logging.DEBUG)
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self.sink.append(record)


@pytest.fixture
def make_env(tmp_path: Path) -> Iterator[Callable[..., SimpleNamespace]]:
    """偽 Win32 + 偽暗号 + 偽の時計で monitor / store / ops を組む。"""
    stores: list[Store] = []
    handlers: list[tuple[logging.Logger, logging.Handler]] = []

    def factory(**over: Any) -> SimpleNamespace:
        api = FakeWin32()
        clock = FakeClock()
        sec, _ = cfgmod.merge_defaults({"mode": "record", **over})
        cfg = [cfgmod.parse(sec)]
        store = Store(tmp_path / f"db{len(stores)}.db", FakeCipher(), clock)
        store.open()
        stores.append(store)
        ops = OpsLog(tmp_path / "ops.jsonl", clock)
        records: list[logging.LogRecord] = []
        log = logging.getLogger(f"deskkit.clipshelf.test{len(stores)}")
        log.setLevel(logging.DEBUG)
        h = _ListHandler(records)
        log.addHandler(h)
        handlers.append((log, h))
        paused = [False]
        mon = ClipMonitor(api, owner_hwnd=lambda: 0x99, config=lambda: cfg[0], store=lambda: store,
                          paused=lambda: paused[0], log=log, ops=ops, now=clock, sleep=lambda _s: None)
        mon.mark_startup()
        return SimpleNamespace(api=api, clock=clock, store=store, ops=ops, mon=mon, paused=paused, cfg=cfg,
                               log_records=records, log=log)

    yield factory
    for s in stores:
        s.close()
    for lg, h in handlers:
        lg.removeHandler(h)


# ------------------------------------------------------------------ GUI・モジュール用の偽 ctx
class FakeTrayItem:
    def __init__(self, label: str, cb: Callable[[], None]) -> None:
        self.label = label
        self.cb = cb
        self.checked = False

    def set_text(self, t: str) -> None:
        self.label = t

    def set_checked(self, c: bool) -> None:
        self.checked = c

    def set_enabled(self, _e: bool) -> None:
        pass

    def set_visible(self, _v: bool) -> None:
        pass


class FakeHotkeys:
    def __init__(self) -> None:
        self.fail: set[str] = set()
        self.registered: dict[str, str] = {}
        self.callbacks: dict[str, list[Callable[[], None]]] = {}

    def register_text(self, name: str, text: str | None) -> bool:
        if not text or name in self.fail:
            return False
        self.registered[name] = text
        return True

    def triggered(self, name: str) -> Any:
        return SimpleNamespace(connect=lambda cb: self.callbacks.setdefault(name, []).append(cb))

    def unregister(self, name: str) -> None:
        self.registered.pop(name, None)

    def press(self, name: str) -> None:
        for cb in self.callbacks.get(name, []):
            cb()


class FakeCtx:
    def __init__(self, data_dir: Path, section: dict[str, Any] | None = None, fg: ForegroundInfo = SAFE_FG) -> None:
        self.name = "clipshelf"
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.log = logging.getLogger("deskkit.clipshelf.fakectx")
        self._section: dict[str, Any] = {"enabled": True, **(section or {})}
        self.fg = fg
        self.hotkeys = FakeHotkeys()
        self.tray: list[FakeTrayItem] = []
        self.notifications: list[tuple[str, str, str]] = []
        self.native: dict[int, Callable[[int, int], None]] = {}
        self.status = ""
        self.writes: list[tuple[dict[str, Any], bool]] = []
        self.errors: list[str] = []
        self._timers: list[Any] = []
        self.quick: list[SimpleNamespace] = []

    def add_quick_action(self, label: str, callback: Callable[[], None], *, keywords: str = "", glyph: str | None = None,
                         enabled: Callable[[], bool] | None = None) -> None:
        self.quick.append(SimpleNamespace(label=label, callback=self.safe(callback, f"quick:{label}"), keywords=keywords,
                                          glyph=glyph, enabled=enabled))

    def clear_quick_actions(self) -> None:
        self.quick.clear()

    def quick_action(self, label: str) -> SimpleNamespace:
        return next(q for q in self.quick if q.label == label)

    def settings(self) -> dict[str, Any]:
        return dict(self._section)

    def settings_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._section)

    def write_settings(self, section: dict[str, Any], *, restart: bool = False) -> None:
        self._section = {**copy.deepcopy(section), "enabled": True}
        self.writes.append((copy.deepcopy(section), restart))

    def game_processes(self) -> frozenset[str]:
        return frozenset({"game.exe"})

    def add_tray_action(self, label: str, cb: Callable[[], None], *, checkable: bool = False, checked: bool = False,
                        submenu: str | None = None) -> FakeTrayItem:
        it = FakeTrayItem(label, cb)
        self.tray.append(it)
        return it

    def add_tray_separator(self, submenu: str | None = None) -> None:
        pass

    def clear_tray_actions(self, submenu: str | None = None) -> None:
        self.tray.clear()

    def set_tray_status(self, text: str) -> None:
        self.status = text

    def notify(self, title: str, text: str, on_click: Callable[[], None] | None = None, *, level: str = "info") -> None:
        self.notifications.append((title, text, level))

    def on_native(self, msg: int, handler: Callable[[int, int], None]) -> None:
        self.native[msg] = handler

    def hidden_hwnd(self) -> int:
        return 0x99

    def foreground(self) -> ForegroundInfo:
        return self.fg

    def emit(self, event: str, payload: Any) -> None:
        pass

    def on(self, event: str, handler: Callable[[Any], None]) -> None:
        pass

    def safe(self, fn: Callable[..., Any], label: str | None = None) -> Callable[..., Any]:
        def w(*a: Any, **k: Any) -> Any:
            try:
                return fn(*a, **k)
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{label}: {type(e).__name__}: {e}")
                return None

        return w

    def call_soon(self, fn: Callable[[], None]) -> None:
        fn()

    def start_timer(self, ms: int, cb: Callable[[], None], *, single_shot: bool = False) -> Any:
        from PySide6.QtCore import QTimer

        t = QTimer()
        t.setSingleShot(single_shot)
        t.setInterval(ms)
        t.timeout.connect(self.safe(cb, "timer"))
        t.start()
        self._timers.append(t)
        return t

    def dpi_awareness(self) -> str:
        return "per_monitor_aware_v2"

    def window_parent(self) -> None:
        return None


@pytest.fixture(scope="session")
def qapp() -> Any:
    from PySide6.QtWidgets import QApplication

    from deskkit.ui import theme

    app = QApplication.instance() or QApplication([])
    theme.apply(app)  # type: ignore[arg-type]
    return app


@pytest.fixture
def module_factory(tmp_path: Path, qapp: Any) -> Iterator[Callable[..., Any]]:
    """偽 ctx・偽 Win32・偽暗号で ClipShelfModule を start した状態を作る。"""
    from deskkit.modules.clipshelf.module import ClipShelfModule

    mods: list[Any] = []

    def factory(section: dict[str, Any] | None = None, fg: ForegroundInfo = SAFE_FG) -> tuple[Any, FakeCtx, FakeWin32, FakeClock]:
        ctx = FakeCtx(tmp_path / f"data{len(mods)}", section, fg)
        api = FakeWin32()
        if fg.hwnd:
            api.windows[fg.hwnd] = fg.pid or 0
        clock = FakeClock()
        m = ClipShelfModule(ctx, api=api, cipher=FakeCipher(), now=clock)  # type: ignore[arg-type]
        m.start()
        mods.append(m)
        return m, ctx, api, clock

    yield factory
    for m in mods:
        m.stop()
