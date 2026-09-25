# PcCheckup のテスト共通部品(偽 ctx・QApplication・DESKKIT_HOME の隔離)。本物の %TEMP%・ごみ箱・設定には触れない。
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
        self.name = "pccheckup"
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.log = logging.getLogger(f"deskkit.pccheckup.test.{id(self)}")
        self.log.setLevel(logging.DEBUG)
        self.log.propagate = False
        self.handler = ListHandler()
        self.log.addHandler(self.handler)
        self._section: dict[str, Any] = {"enabled": True, **(section or {})}
        self.notifications: list[tuple[str, str, str, Any]] = []
        self.writes: list[dict[str, Any]] = []
        self.status = ""
        self.snoozed = False
        self.quick: list[SimpleNamespace] = []
        self.shown = 0
        self.timers: list[tuple[int, Callable[[], None], bool]] = []
        self.fail_write = False

    def settings(self) -> dict[str, Any]:
        return dict(self._section)

    def settings_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._section)

    def write_settings(self, section: dict[str, Any], *, restart: bool = False) -> None:
        if self.fail_write:
            raise ValueError("broken settings")
        self._section = {**copy.deepcopy(section), "enabled": True}
        self.writes.append(copy.deepcopy(section))

    def is_snoozed(self) -> bool:
        return self.snoozed

    def add_quick_action(self, label: str, callback: Callable[[], None], *, keywords: str = "", glyph: str | None = None,
                         enabled: Callable[[], bool] | None = None) -> None:
        self.quick.append(SimpleNamespace(label=label, callback=callback, keywords=keywords, glyph=glyph, enabled=enabled))

    def set_tray_status(self, text: str) -> None:
        self.status = text

    def notify(self, title: str, text: str, on_click: Callable[[], None] | None = None, *, level: str = "info") -> None:
        self.notifications.append((title, text, level, on_click))

    def show_page(self) -> None:
        self.shown += 1

    def call_soon(self, fn: Callable[[], None]) -> None:
        fn()

    def start_timer(self, ms: int, cb: Callable[[], None], *, single_shot: bool = False) -> Any:
        self.timers.append((ms, cb, single_shot))
        return SimpleNamespace(stop=lambda: None)

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
    """偽 probes・偽 ctx・同期実行の PcCheckupModule を作る。"""
    from deskkit.modules.pccheckup.fakes import FakeProbes
    from deskkit.modules.pccheckup.module import PcCheckupModule

    mods: list[Any] = []

    def factory(probes: FakeProbes | None = None, section: dict[str, Any] | None = None, **kw: Any) -> tuple[Any, FakeCtx, FakeProbes]:
        ctx = FakeCtx(tmp_path / f"data{len(mods)}", section)
        fp = probes or FakeProbes()
        opened: list[str] = []
        m = PcCheckupModule(ctx, probes_factory=lambda: fp, threaded=False, startfile=opened.append, **kw)
        m.opened = opened  # type: ignore[attr-defined]
        m.start()
        mods.append(m)
        return m, ctx, fp

    yield factory
    for m in mods:
        m.stop()
