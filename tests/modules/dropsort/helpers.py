# DropSort のテスト用の偽 ModuleContext・偽タイマー・偽ホットキー(host なしでモジュールを動かす)。
from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType
from typing import Any

from deskkit.foreground import ForegroundInfo

SAFE_FG = ForegroundInfo(1, 2, "explorer.exe", False, False, False)
GAME_FG = ForegroundInfo(1, 2, "game.exe", True, False, False)
FULL_FG = ForegroundInfo(1, 2, "video.exe", False, True, False)


class FakeTimer:
    def __init__(self, ms: int, cb: Callable[[], None], single: bool) -> None:
        self.interval = ms
        self.cb = cb
        self.single = single
        self.active = True

    def start(self, ms: int | None = None) -> None:
        if ms is not None:
            self.interval = ms
        self.active = True

    def stop(self) -> None:
        self.active = False

    def setInterval(self, ms: int) -> None:  # noqa: N802
        self.interval = ms

    def fire(self) -> None:
        if self.single:
            self.active = False
        self.cb()


class FakeTrayItem:
    def __init__(self, label: str, cb: Callable[[], None], checked: bool) -> None:
        self.label = label
        self.cb = cb
        self.checked = checked

    def set_text(self, t: str) -> None:
        self.label = t

    def set_checked(self, c: bool) -> None:
        self.checked = c

    def set_enabled(self, e: bool) -> None:
        pass

    def set_visible(self, v: bool) -> None:
        pass


class _Trig:
    def __init__(self, hk: FakeHotkeys, name: str) -> None:
        self.hk = hk
        self.name = name

    def connect(self, cb: Callable[[], None]) -> None:
        self.hk.callbacks[self.name] = cb


class FakeHotkeys:
    def __init__(self) -> None:
        self.registered: dict[str, str] = {}
        self.callbacks: dict[str, Callable[[], None]] = {}
        self.fail = False

    def register_text(self, name: str, text: str | None) -> bool:
        if not text or self.fail:
            return False
        self.registered[name] = text
        return True

    def triggered(self, name: str) -> _Trig:
        return _Trig(self, name)


class FakeCtx:
    def __init__(self, data_dir: Path, section: dict[str, Any] | None = None) -> None:
        self.name = "dropsort"
        self.data_dir = data_dir
        self.log = logging.getLogger("deskkit.dropsort.test")
        self._section: dict[str, Any] = {"enabled": True, **(section or {})}
        self.writes: list[tuple[dict[str, Any], bool]] = []
        self.notes: list[tuple[str, str, str]] = []
        self.status = ""
        self.tray: list[FakeTrayItem] = []
        self.timers: list[FakeTimer] = []
        self.queue: list[Callable[[], None]] = []
        self.errors: list[str] = []
        self.fg = SAFE_FG
        self.hotkeys = FakeHotkeys()
        self.quick: list[tuple[str, Callable[..., Any], str]] = []
        self.page_shown = 0

    def settings(self) -> Any:
        return MappingProxyType(copy.deepcopy(self._section))

    def settings_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._section)

    def write_settings(self, section: Any, *, restart: bool = False) -> None:
        sec = copy.deepcopy(dict(section))
        sec["enabled"] = True
        self._section = sec
        self.writes.append((sec, restart))

    def game_processes(self) -> frozenset[str]:
        return frozenset({"game.exe"})

    def add_tray_action(self, label: str, cb: Callable[[], None], *, checkable: bool = False, checked: bool = False,
                        submenu: str | None = None) -> FakeTrayItem:
        it = FakeTrayItem(label, self.safe(cb, label), checked)
        self.tray.append(it)
        return it

    def add_tray_separator(self, submenu: str | None = None) -> None:
        pass

    def clear_tray_actions(self, submenu: str | None = None) -> None:
        self.tray.clear()

    def set_tray_status(self, text: str) -> None:
        self.status = text

    def notify(self, title: str, text: str, on_click: Callable[[], None] | None = None, *, level: str = "info") -> None:
        self.notes.append((title, text, level))

    def foreground(self) -> ForegroundInfo:
        return self.fg

    def safe(self, fn: Callable[..., Any], label: str | None = None) -> Callable[..., Any]:
        def w(*a: Any, **k: Any) -> Any:
            try:
                return fn(*a, **k)
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{label}: {type(e).__name__}: {e}")
                return None

        return w

    def call_soon(self, fn: Callable[[], None]) -> None:
        self.queue.append(self.safe(fn, "call_soon"))

    def drain(self) -> None:
        while self.queue:
            self.queue.pop(0)()

    def start_timer(self, ms: int, cb: Callable[[], None], *, single_shot: bool = False) -> FakeTimer:
        t = FakeTimer(ms, self.safe(cb, "timer"), single_shot)
        self.timers.append(t)
        return t

    def window_parent(self) -> None:
        return None

    def add_quick_action(self, label: str, cb: Callable[[], None], *, keywords: str = "", glyph: str | None = None,
                         enabled: Callable[[], bool] | None = None) -> None:
        self.quick.append((label, self.safe(cb, f"quick:{label}"), keywords))

    def clear_quick_actions(self) -> None:
        self.quick.clear()

    def show_page(self) -> None:
        self.page_shown += 1

    def dpi_awareness(self) -> str:
        return "per_monitor_aware_v2"


def wait_worker(module: Any, ctx: FakeCtx) -> None:
    """作業スレッドの仕事が終わるまで待ち、メインスレッド側のコールバックを流す。"""
    ex = module._executor
    if ex is not None:
        ex.submit(lambda: None).result(timeout=10)
    ctx.drain()
