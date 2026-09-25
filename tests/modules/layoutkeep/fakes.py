# LayoutKeep のテスト用の偽 ModuleContext。タイマは手で発火させ、通知・イベント・トレイ項目を記録する。
from __future__ import annotations

import copy
import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from deskkit.foreground import ForegroundInfo

SAFE_FG = ForegroundInfo(hwnd=1, pid=1, exe="explorer.exe", is_game=False, is_fullscreen=False, is_elevated=False)
GAME_FG = ForegroundInfo(hwnd=2, pid=2, exe="game.exe", is_game=True, is_fullscreen=True, is_elevated=False)


class FakeTimer:
    def __init__(self, ms: int, cb: Callable[[], None], single_shot: bool) -> None:
        self.interval = ms
        self.cb = cb
        self.single_shot = single_shot
        self.active = True

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False

    def setInterval(self, ms: int) -> None:  # noqa: N802
        self.interval = ms

    def fire(self) -> None:
        if not self.active:
            return
        if self.single_shot:
            self.active = False
        self.cb()


class FakeTrayItem:
    def __init__(self, label: str, cb: Callable[[], None]) -> None:
        self.text = label
        self.cb = cb
        self.enabled = True

    def set_text(self, t: str) -> None:
        self.text = t

    def set_checked(self, c: bool) -> None:
        pass

    def set_enabled(self, e: bool) -> None:
        self.enabled = e

    def set_visible(self, v: bool) -> None:
        pass


class _Proxy:
    def __init__(self, hk: FakeHotkeys, name: str) -> None:
        self.hk, self.name = hk, name

    def connect(self, cb: Callable[[], None]) -> None:
        self.hk.callbacks[self.name] = cb


class FakeHotkeys:
    def __init__(self) -> None:
        self.registered: dict[str, str] = {}
        self.callbacks: dict[str, Callable[[], None]] = {}
        self.fail: set[str] = set()

    def register_text(self, name: str, text: str | None) -> bool:
        if not text or name in self.fail:
            return False
        self.registered[name] = text
        return True

    def triggered(self, name: str) -> _Proxy:
        return _Proxy(self, name)

    def unregister(self, name: str) -> None:
        self.registered.pop(name, None)


class FakeCtx:
    def __init__(self, data_dir: Path, section: Mapping[str, Any] | None = None, *,
                 awareness: str = "per_monitor_aware_v2") -> None:
        self.name = "layoutkeep"
        self.data_dir = data_dir
        self.log = logging.getLogger("test.layoutkeep")
        self.section: dict[str, Any] = {"enabled": True, **copy.deepcopy(dict(section or {}))}
        self.awareness = awareness
        self.fg = SAFE_FG
        self.games: set[str] = set()
        self.notes: list[dict[str, Any]] = []
        self.emitted: list[tuple[str, dict[str, Any]]] = []
        self.native: dict[int, list[Callable[[int, int], None]]] = {}
        self.handlers: dict[str, list[Callable[[Mapping[str, Any]], None]]] = {}
        self.timers: list[FakeTimer] = []
        self.tray: list[FakeTrayItem] = []
        self.quick: list[tuple[str, Callable[[], None]]] = []
        self.status = ""
        self.restarts = 0
        self.hotkeys = FakeHotkeys()
        self.snoozed = False
        self.modes: list[tuple[str, str]] = []

    def is_snoozed(self) -> bool:
        """契約 §1: 一時停止(スヌーズ)中か(既定 False)。"""
        return self.snoozed

    def list_modes(self) -> list[tuple[str, str]]:
        """契約 §1: ModeShift のモード (name, label) の一覧。"""
        return list(self.modes)

    def settings(self) -> Mapping[str, Any]:
        return MappingProxyType(copy.deepcopy(self.section))

    def settings_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.section)

    def write_settings(self, section: Mapping[str, Any], *, restart: bool = False) -> None:
        self.section = {**copy.deepcopy(dict(section)), "enabled": True}
        if restart:
            self.restarts += 1

    def game_processes(self) -> frozenset[str]:
        return frozenset(self.games)

    def add_tray_action(self, label: str, cb: Callable[[], None], *, checkable: bool = False, checked: bool = False,
                        submenu: str | None = None) -> FakeTrayItem:
        it = FakeTrayItem(label, cb)
        self.tray.append(it)
        return it

    def add_tray_separator(self, submenu: str | None = None) -> None:
        pass

    def clear_tray_actions(self, submenu: str | None = None) -> None:
        self.tray.clear()

    def add_quick_action(self, label: str, cb: Callable[[], None], *, keywords: str = "", glyph: str | None = None,
                         enabled: Callable[[], bool] | None = None) -> None:
        self.quick.append((label, cb))

    def clear_quick_actions(self) -> None:
        self.quick.clear()

    def set_tray_status(self, text: str) -> None:
        self.status = text

    def notify(self, title: str, text: str, on_click: Callable[[], None] | None = None, *, level: str = "info") -> None:
        self.notes.append({"title": title, "text": text, "on_click": on_click, "level": level})

    def on_native(self, msg: int, handler: Callable[[int, int], None]) -> None:
        self.native.setdefault(msg, []).append(handler)

    def hidden_hwnd(self) -> int:
        return 0

    def foreground(self) -> ForegroundInfo:
        return self.fg

    def emit(self, event: str, payload: Mapping[str, Any]) -> None:
        self.emitted.append((event, dict(payload)))
        for h in self.handlers.get(event, []):
            h(dict(payload))

    def on(self, event: str, handler: Callable[[Mapping[str, Any]], None]) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def safe(self, fn: Callable[..., Any], label: str | None = None) -> Callable[..., Any]:
        return fn

    def call_soon(self, fn: Callable[[], None]) -> None:
        fn()

    def start_timer(self, ms: int, cb: Callable[[], None], *, single_shot: bool = False) -> FakeTimer:
        t = FakeTimer(ms, cb, single_shot)
        self.timers.append(t)
        return t

    def dpi_awareness(self) -> str:
        return self.awareness

    def window_parent(self) -> None:
        return None


def make_module(tmp_path: Path, api: Any, section: dict[str, Any] | None = None, **kw: Any) -> tuple[FakeCtx, Any]:
    from deskkit.modules.layoutkeep.module import LayoutKeepModule

    ctx = FakeCtx(tmp_path / "data", section or {}, **kw)
    os.makedirs(ctx.data_dir, exist_ok=True)
    mod = LayoutKeepModule(ctx, api=api)
    return ctx, mod
