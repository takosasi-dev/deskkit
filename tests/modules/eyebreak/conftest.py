# EyeBreak のテスト共通部品(偽 ctx・偽の時計・偽の入力時刻・偽の前面・DESKKIT_HOME の隔離)。
# FakeCtx.notify は replace_key を受け付ける(本体の H4-6 の後)。OldNotifyCtx は受け付けない(本体の H4-6 の前)。
from __future__ import annotations

import copy
import logging
import os
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SECRET_EXE = "zzsecretgame.exe"
SECRET_MODE = "zzSecretMode"
SECRET_LABEL = "ZzSecretLabel"


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


class TrayItem:
    def __init__(self, label: str, cb: Callable[[], None]) -> None:
        self.label = label
        self.cb = cb
        self.enabled = True

    def set_text(self, t: str) -> None:
        self.label = t

    def set_enabled(self, b: bool) -> None:
        self.enabled = b


class FakeCtx:
    def __init__(self, data_dir: Path, section: dict[str, Any] | None = None) -> None:
        self.name = "eyebreak"
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.log = logging.getLogger(f"deskkit.eyebreak.test.{id(self)}")
        self.log.setLevel(logging.DEBUG)
        self.log.propagate = False
        self.handler = ListHandler()
        self.log.addHandler(self.handler)
        self._section: dict[str, Any] = {"enabled": True, **copy.deepcopy(section or {})}
        self.notifications: list[dict[str, Any]] = []
        self.writes: list[dict[str, Any]] = []
        self.status = ""
        self.snoozed = False
        self.game = False
        self.fullscreen: bool | None = False
        self.fg_reads = 0
        self.modes: list[tuple[str, str]] = [(SECRET_MODE, SECRET_LABEL), ("work", "作業")]
        self.handlers: dict[str, list[Callable[[Mapping[str, Any]], None]]] = {}
        self.natives: dict[int, list[Callable[[int, int], None]]] = {}
        self.timers: list[tuple[int, Callable[[], None]]] = []
        self.tray: dict[str, TrayItem] = {}
        self.quick: list[SimpleNamespace] = []
        self.shown = 0

    def settings(self) -> dict[str, Any]:
        return dict(self._section)

    def settings_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._section)

    def write_settings(self, section: dict[str, Any], *, restart: bool = False) -> None:
        self._section = {**copy.deepcopy(section), "enabled": True}
        self.writes.append(copy.deepcopy(section))

    def is_snoozed(self) -> bool:
        return self.snoozed

    def foreground(self) -> Any:
        self.fg_reads += 1
        return SimpleNamespace(exe=SECRET_EXE, is_game=self.game, is_fullscreen=self.fullscreen, is_elevated=False)

    def list_modes(self) -> list[tuple[str, str]]:
        return list(self.modes)

    def on(self, event: str, handler: Callable[[Mapping[str, Any]], None]) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def emit_event(self, event: str, payload: Mapping[str, Any]) -> None:
        for h in self.handlers.get(event, []):
            h(payload)

    def on_native(self, msg: int, handler: Callable[[int, int], None]) -> None:
        self.natives.setdefault(msg, []).append(handler)

    def start_timer(self, ms: int, cb: Callable[[], None], *, single_shot: bool = False) -> Any:
        self.timers.append((ms, cb))
        return SimpleNamespace(stop=lambda: None)

    def add_tray_action(self, label: str, cb: Callable[[], None], **_kw: Any) -> TrayItem:
        item = TrayItem(label, cb)
        self.tray[label] = item
        return item

    def add_quick_action(self, label: str, callback: Callable[[], None], *, keywords: str = "", glyph: str | None = None,
                         enabled: Callable[[], bool] | None = None) -> None:
        self.quick.append(SimpleNamespace(label=label, callback=callback, keywords=keywords, enabled=enabled))

    def set_tray_status(self, text: str) -> None:
        self.status = text

    def notify(self, title: str, text: str, on_click: Callable[[], None] | None = None, *, level: str = "info",
               replace_key: str | None = None) -> None:
        self.notifications.append({"title": title, "text": text, "on_click": on_click, "level": level,
                                   "replace_key": replace_key})

    def show_page(self) -> None:
        self.shown += 1

    def call_soon(self, fn: Callable[[], None]) -> None:
        fn()

    def window_parent(self) -> None:
        return None


class OldNotifyCtx(FakeCtx):
    """本体の H4-6 の前: notify が replace_key を受け付けない。"""

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        self.type_errors = 0

    def notify(self, title: str, text: str, on_click: Callable[[], None] | None = None, *,  # type: ignore[override]
               level: str = "info", **kw: Any) -> None:
        if kw:
            self.type_errors += 1
            raise TypeError("notify() got an unexpected keyword argument 'replace_key'")
        self.notifications.append({"title": title, "text": text, "on_click": on_click, "level": level,
                                   "replace_key": None})


class Harness:
    """偽の時計(monotonic と壁の時計)と偽の入力で、モジュールを 5 秒ずつ進める。"""

    def __init__(self, module: Any, ctx: FakeCtx, api: Any, clock: list[float], base: datetime) -> None:
        self.m = module
        self.ctx = ctx
        self.api = api
        self.clock = clock
        self.base = base

    def wall(self) -> datetime:
        return self.base + timedelta(seconds=self.clock[0])

    def run(self, seconds: float, *, active: bool = True) -> None:
        for _ in range(int(round(seconds / 5.0))):
            self.clock[0] += 5.0
            self.api.advance(5.0, active=active)
            self.m.tick()

    def titles(self) -> list[str]:
        return [n["title"] for n in self.ctx.notifications]


@pytest.fixture(scope="session")
def qapp() -> Any:
    from PySide6.QtWidgets import QApplication

    from deskkit.ui import theme

    app = QApplication.instance() or QApplication([])
    theme.apply(app)  # type: ignore[arg-type]
    return app


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Callable[..., Harness]]:
    """EyeBreakModule を偽物で作って start する。section は settings の modules.eyebreak(既定は live)。"""
    from deskkit.modules.eyebreak._win32 import FakeApi
    from deskkit.modules.eyebreak.module import EyeBreakModule

    made: list[Any] = []

    def factory(section: dict[str, Any] | None = None, *, ctx: FakeCtx | None = None, ctx_cls: type[FakeCtx] = FakeCtx,
                base: datetime | None = None, clock: list[float] | None = None) -> Harness:
        c = ctx or ctx_cls(tmp_path / "data", {"mode": "live", **(section or {})})
        api = FakeApi()
        clk = clock if clock is not None else [0.0]
        b = base or datetime(2026, 9, 28, 9, 0, 0)
        h = Harness(None, c, api, clk, b)
        m = EyeBreakModule(c, api=api, clock=lambda: clk[0], wall=h.wall)
        h.m = m
        m.start()
        made.append(m)
        return h

    yield factory
    for m in made:
        m.stop()
