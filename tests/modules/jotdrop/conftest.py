# JotDrop のテスト共通部品(偽 ctx・偽のホットキー・偽の前面・QApplication・DESKKIT_HOME の隔離)。
# ファイルは fakes.FakeWin32(メモリの中)に書く。本物のファイルに書くテストは test_real_win32.py(win32_real)だけ。
from __future__ import annotations

import copy
import logging
import os
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

DOCS = "C:\\Users\\someone\\Documents"
NOTES = "C:\\Notes"


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DESKKIT_HOME", str(tmp_path / "home"))


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def text(self) -> str:
        return "\n".join(r.getMessage() for r in self.records)


class FakeHotkeys:
    def __init__(self) -> None:
        self.result = True
        self.registered: list[tuple[str, str]] = []
        self.callbacks: dict[str, list[Callable[[], None]]] = {}

    def register_text(self, name: str, text: str | None) -> bool:
        if not text:
            return False
        self.registered.append((name, text))
        return self.result

    def triggered(self, name: str) -> Any:
        return SimpleNamespace(connect=lambda cb: self.callbacks.setdefault(name, []).append(cb))

    def fire(self, name: str) -> None:
        for cb in self.callbacks.get(name, []):
            cb()


class FakeCtx:
    def __init__(self, data_dir: Path, section: dict[str, Any] | None = None) -> None:
        self.name = "jotdrop"
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.log = logging.getLogger(f"deskkit.jotdrop.test.{id(self)}")
        self.log.setLevel(logging.DEBUG)
        self.log.propagate = False
        self.handler = ListHandler()
        self.log.addHandler(self.handler)
        self._section: dict[str, Any] = {"enabled": True, **(section or {})}
        self.notifications: list[tuple[str, str, str, Any]] = []
        self.writes: list[tuple[dict[str, Any], bool]] = []
        self.status = ""
        self.snoozed = False
        self.tray: list[tuple[str, Callable[[], None]]] = []
        self.shown = 0
        self.timers: list[tuple[int, Callable[[], None], bool]] = []
        self.fail_write = False
        self.hotkeys = FakeHotkeys()
        from deskkit.foreground import ForegroundInfo

        self.fg = ForegroundInfo(hwnd=500, pid=4242, exe="editor.exe", is_game=False, is_fullscreen=False,
                                 is_elevated=False)

    def settings(self) -> dict[str, Any]:
        return dict(self._section)

    def settings_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._section)

    def write_settings(self, section: dict[str, Any], *, restart: bool = False) -> None:
        if self.fail_write:
            raise ValueError("broken settings")
        self._section = {**copy.deepcopy(section), "enabled": True}
        self.writes.append((copy.deepcopy(section), restart))

    def safe(self, fn: Callable[..., Any], label: str | None = None) -> Callable[..., Any]:
        return fn

    def is_snoozed(self) -> bool:
        return self.snoozed

    def add_tray_action(self, label: str, cb: Callable[[], None], **_kw: Any) -> Any:
        self.tray.append((label, cb))
        return SimpleNamespace(set_text=lambda _t: None, set_enabled=lambda _e: None)

    def tray_cb(self, label: str) -> Callable[[], None]:
        return next(cb for lb, cb in self.tray if lb == label)

    def add_quick_action(self, *a: Any, **k: Any) -> None:
        pass

    def set_tray_status(self, text: str) -> None:
        self.status = text

    def notify(self, title: str, text: str, on_click: Callable[[], None] | None = None, *, level: str = "info",
               **_kw: Any) -> None:
        self.notifications.append((title, text, level, on_click))

    def show_page(self) -> None:
        self.shown += 1

    def call_soon(self, fn: Callable[[], None]) -> None:
        fn()

    def start_timer(self, ms: int, cb: Callable[[], None], *, single_shot: bool = False) -> Any:
        self.timers.append((ms, cb, single_shot))
        return SimpleNamespace(stop=lambda: None)

    def foreground(self) -> Any:
        return self.fg

    def window_parent(self) -> None:
        return None

    def run_timers(self, ms: int) -> None:
        """その間隔の単発タイマーを実行して取り除く(照合の 10 秒など)。"""
        due = [t for t in self.timers if t[0] == ms and t[2]]
        self.timers = [t for t in self.timers if t not in due]
        for _ms, cb, _s in due:
            cb()


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t
        self.mono = 1000.0

    def now(self) -> datetime:
        return self.t

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)
        self.mono += seconds


@pytest.fixture(scope="session")
def qapp() -> Any:
    from PySide6.QtWidgets import QApplication

    from deskkit.ui import theme

    app = QApplication.instance() or QApplication([])
    theme.apply(app)  # type: ignore[arg-type]
    return app


@pytest.fixture
def make_module(tmp_path: Path, qapp: Any) -> Iterator[Callable[..., Any]]:
    """偽の Win32・偽 ctx・同期実行の JotDropModule を作る。既定の書き込み先は C:\\Notes\\{date}.md(確認済み)。"""
    from deskkit.modules.jotdrop.fakes import FakeWin32
    from deskkit.modules.jotdrop.module import JotDropModule

    mods: list[Any] = []

    def factory(section: dict[str, Any] | None = None, api: FakeWin32 | None = None,
                when: datetime = datetime(2026, 9, 26, 14, 5), start: bool = True) -> tuple[Any, FakeCtx, FakeWin32, Clock]:
        sec = {"folder": NOTES, "target_confirmed": True, **(section or {})}
        ctx = FakeCtx(tmp_path / f"data{len(mods)}", sec)
        fa = api or FakeWin32()
        fa.add_dir(NOTES)
        fa.add_dir(DOCS)
        fa.window_pids.update({500: 4242, 600: os.getpid()})
        clock = Clock(when)
        opened: list[str] = []
        m = JotDropModule(ctx, api=fa, threaded=False, clock=clock.now, mono=clock.monotonic, documents=lambda: DOCS,
                          startfile=opened.append, retry_sleep=lambda _s: False)
        m.opened = opened  # type: ignore[attr-defined]
        if start:
            m.start()
            ctx.run_timers(3_000)       # 入力欄を前もって作るタイマー(POPUP_PREBUILD_MS)
        mods.append(m)
        return m, ctx, fa, clock

    yield factory
    for m in mods:
        try:
            m.stop()
        except Exception:  # noqa: BLE001
            pass
