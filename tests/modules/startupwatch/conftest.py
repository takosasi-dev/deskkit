# StartupWatch のテスト共通部品(偽 ctx・偽の Win32・QApplication・DESKKIT_HOME の隔離)。本物のレジストリと
# スタートアップ フォルダには触れない。
from __future__ import annotations

import copy
import logging
import os
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SECRET_NAME = "ZzSecretStartupApp"
SECRET_CMD = r"C:\Users\zz-user\AppData\Local\ZzSecretStartupApp\zz-secret.exe --tray"
DESKKIT_EXE = r"C:\Tools\DeskKit\DeskKit.exe"


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


class FakeCtx:
    def __init__(self, data_dir: Path, section: dict[str, Any] | None = None) -> None:
        self.name = "startupwatch"
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.log = logging.getLogger(f"deskkit.startupwatch.test.{id(self)}")
        self.log.setLevel(logging.DEBUG)
        self.log.propagate = False
        self.handler = ListHandler()
        self.log.addHandler(self.handler)
        self._section: dict[str, Any] = {"enabled": True, **(section or {})}
        self.notifications: list[tuple[str, str, str, Any]] = []
        self.writes: list[tuple[dict[str, Any], bool]] = []
        self.status = ""
        self.snoozed = False
        self.handlers: dict[str, list[Callable[[Mapping[str, Any]], None]]] = {}
        self.tray: list[tuple[str, Callable[[], None]]] = []
        self.shown = 0

    def settings(self) -> dict[str, Any]:
        return dict(self._section)

    def settings_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._section)

    def write_settings(self, section: dict[str, Any], *, restart: bool = False) -> None:
        self._section = {**copy.deepcopy(section), "enabled": True}
        self.writes.append((copy.deepcopy(section), restart))

    def is_snoozed(self) -> bool:
        return self.snoozed

    def on(self, event: str, handler: Callable[[Mapping[str, Any]], None]) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def emit_event(self, event: str, payload: Mapping[str, Any]) -> None:
        for h in self.handlers.get(event, []):
            h(payload)

    def add_tray_action(self, label: str, cb: Callable[[], None], **_kw: Any) -> Any:
        self.tray.append((label, cb))
        return SimpleNamespace(set_text=lambda _t: None, set_enabled=lambda _b: None)

    def set_tray_status(self, text: str) -> None:
        self.status = text

    def notify(self, title: str, text: str, on_click: Callable[[], None] | None = None, *, level: str = "info") -> None:
        self.notifications.append((title, text, level, on_click))

    def show_page(self) -> None:
        self.shown += 1

    def call_soon(self, fn: Callable[[], None]) -> None:
        fn()

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
def make_module(tmp_path: Path) -> Iterator[Callable[..., Any]]:
    """偽の Win32・偽 ctx・同期実行の StartupWatchModule を作る(start はしない)。同じ data_dir を使い回せる。"""
    from deskkit.modules.startupwatch.module import StartupWatchModule

    mods: list[Any] = []

    def factory(api: Any, ctx: FakeCtx | None = None, section: dict[str, Any] | None = None,
                threaded: bool = False) -> tuple[Any, FakeCtx, list[str]]:
        c = ctx or FakeCtx(tmp_path / "data", section)
        opened: list[str] = []
        m = StartupWatchModule(c, api=api, threaded=threaded, clock=api.clock, startfile=opened.append,
                               executable=DESKKIT_EXE)
        mods.append(m)
        return m, c, opened

    yield factory
    for m in mods:
        m.stop()
