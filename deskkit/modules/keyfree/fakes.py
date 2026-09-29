# テストと自己検査で使う偽物: 偽の ctx.hotkeys(probe・snapshot・format・parse・register・unregister)・偽の配列・偽の ctx。
# Windows のホットキーの登録は本当には行わない。probe の結果は flush() を呼んだときに届く(本物と同じく probe() から戻った後)。
from __future__ import annotations

import copy
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from deskkit.hotkeys import (
    FailedKey,
    HeldKey,
    HotkeySnapshot,
    ProbeBatch,
    ProbeDone,
    ProbeResult,
    format_hotkey,
    try_parse_hotkey,
)
from deskkit.modules.keyfree import keynames

# 米国配列の刻印(VK_OEM_102 は米国配列に無いが、偽物では全部あることにする)
US_CHARS = {0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/", 0xC0: "`", 0xDB: "[", 0xDC: "\\",
            0xDD: "]", 0xDE: "'", 0xE2: "<"}


class FakeKeyboard:
    def __init__(self, chars: dict[int, str] | None = None) -> None:
        self.chars = dict(US_CHARS if chars is None else chars)

    def vk_to_char(self, vk: int) -> int:
        c = self.chars.get(vk)
        return ord(c) if c else 0


class FakeHotkeyError(Exception):
    """overlaykit.HotkeyError の代わり(code を持つ)。"""

    def __init__(self, code: int) -> None:
        super().__init__(f"hotkey failed {code}")
        self.code = code


class FakeHandle:
    def __init__(self) -> None:
        self.cancelled = False
        self.done = False
        self.cancel_calls = 0

    def cancel(self) -> None:
        self.cancelled = True
        self.cancel_calls += 1


@dataclass
class _Job:
    combos: list[tuple[int, int]]
    on_batch: Callable[[Any], None]
    on_done: Callable[[Any], None]
    batch: int
    handle: FakeHandle
    pos: int = 0


class FakeHotkeys:
    """used の組は 1409、errors の組はその番号。held は DeskKit が持つ組(snapshot と probe の "deskkit")。"""

    def __init__(self) -> None:
        self.used: set[tuple[int, int]] = set()
        self.errors: dict[tuple[int, int], int] = {}
        self.held: dict[str, tuple[int, int]] = {}
        self.failed: list[FailedKey] = []
        self.pressed_plan: list[tuple[int, int]] = []     # 次のバッチで「押された組」として返す
        self.release_fail = False
        self.busy = False
        self.calls: list[list[tuple[int, int]]] = []
        self.batch_sizes: list[int] = []
        self.jobs: list[_Job] = []
        self.registered: dict[str, tuple[int, int]] = {}
        self.register_log: list[tuple[str, int, int]] = []
        self.unregister_log: list[str] = []
        self.callbacks: dict[str, list[Callable[[], None]]] = {}
        self.format_override: Callable[[int, int], str] | None = None
        self.auto = False            # True なら probe() の後 QTimer で flush する(CLI の待ちのテスト)

    # ---- v0.4 の API
    def probe(self, combos: Sequence[tuple[int, int]], on_batch: Callable[[Any], None], on_done: Callable[[Any], None],
              batch: int = 16) -> FakeHandle:
        items = [(int(m) & 0xF, int(v)) for m, v in combos]
        self.calls.append(items)
        self.batch_sizes.append(batch)
        h = FakeHandle()
        if self.busy:
            h.done = True
            self.jobs.append(_Job([], on_batch, lambda _d: on_done(ProbeDone("busy", 0)), batch, h))
        else:
            self.jobs.append(_Job(items, on_batch, on_done, max(1, int(batch)), h))
        if self.auto:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(0, self.flush)
        return h

    def flush(self, batches: int | None = None) -> None:
        """待っている probe を進める。batches を渡すとそのバッチ数だけ進めて止める(途中の中止の確認用)。"""
        n = 0
        while self.jobs:
            job = self.jobs[0]
            if job.handle.done:                       # busy
                self.jobs.pop(0)
                job.on_done(None)
                continue
            results: list[ProbeResult] = []
            reason = "finished"
            while job.pos < len(job.combos):
                if job.handle.cancelled:
                    reason = "cancelled"
                    break
                m, v = job.combos[job.pos]
                job.pos += 1
                if (m, v) in self.held.values():
                    results.append(ProbeResult(m, v, "deskkit"))
                elif (m, v) in self.used:
                    results.append(ProbeResult(m, v, "used"))
                elif (m, v) in self.errors:
                    results.append(ProbeResult(m, v, "error", self.errors[(m, v)]))
                else:
                    if self.release_fail:
                        reason = "release_failed"
                        break
                    results.append(ProbeResult(m, v, "free"))
                if len(results) >= job.batch:
                    pressed, self.pressed_plan = self.pressed_plan, []
                    job.on_batch(ProbeBatch(results, pressed))
                    results = []
                    n += 1
                    if batches is not None and n >= batches:
                        return
            pressed, self.pressed_plan = self.pressed_plan, []
            if results or pressed:
                job.on_batch(ProbeBatch(results, pressed))
            self.jobs.pop(0)
            job.handle.done = True
            job.on_done(ProbeDone(reason, job.pos - (1 if reason == "release_failed" else 0),
                                  1419 if reason == "release_failed" else 0))

    def snapshot(self) -> HotkeySnapshot:
        return HotkeySnapshot([HeldKey(n, m, v) for n, (m, v) in sorted(self.held.items())], list(self.failed))

    def format(self, mods: int, vk: int) -> str:
        if self.format_override is not None:
            return self.format_override(mods, vk)
        return format_hotkey(mods & 0xF, vk)

    def parse(self, text: str) -> tuple[int, int] | None:
        return try_parse_hotkey(text)

    # ---- 既存の API(押して確かめる)
    def register(self, name: str, mods: int, vk: int) -> None:
        self.register_log.append((name, mods, vk))
        c = (mods & 0xF, vk)
        if c in self.used:
            raise FakeHotkeyError(1409)
        if c in self.errors:
            raise FakeHotkeyError(self.errors[c])
        self.registered[name] = c
        self.held[f"keyfree.{name}"] = c

    def unregister(self, name: str) -> None:
        self.unregister_log.append(name)
        self.registered.pop(name, None)
        self.held.pop(f"keyfree.{name}", None)
        self.callbacks.pop(name, None)

    def triggered(self, name: str) -> Any:
        return SimpleNamespace(connect=lambda cb: self.callbacks.setdefault(name, []).append(cb))

    def fire(self, name: str) -> None:
        for cb in list(self.callbacks.get(name, [])):
            cb()


class OldHotkeys:
    """probe の無い古い host の ctx.hotkeys(FR-17)。"""

    def register_text(self, name: str, text: str | None) -> bool:
        return False

    def parse(self, text: str) -> tuple[int, int] | None:
        return try_parse_hotkey(text)

    def format(self, mods: int, vk: int) -> str:
        return format_hotkey(mods, vk)


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def text(self) -> str:
        return "\n".join(r.getMessage() for r in self.records)


class FakeTimer:
    def __init__(self, ms: int, cb: Callable[[], None], single_shot: bool) -> None:
        self.ms = ms
        self.cb = cb
        self.single_shot = single_shot
        self.active = True

    def stop(self) -> None:
        self.active = False

    def start(self) -> None:
        self.active = True

    def setTimerType(self, _t: Any) -> None:  # noqa: N802 - QTimer と同じ名前
        pass


class FakeCtx:
    def __init__(self, data_dir: Path, section: dict[str, Any] | None = None, hotkeys: Any = None) -> None:
        from deskkit.foreground import ForegroundInfo

        self.name = "keyfree"
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.log = logging.getLogger(f"deskkit.keyfree.test.{id(self)}")
        self.log.setLevel(logging.DEBUG)
        self.log.propagate = False
        self.handler = ListHandler()
        self.log.addHandler(self.handler)
        self._section: dict[str, Any] = {"enabled": True, **(section or {})}
        self.writes: list[dict[str, Any]] = []
        self.hotkeys = FakeHotkeys() if hotkeys is None else hotkeys
        self.tray: list[tuple[str, Callable[[], None]]] = []
        self.status = ""
        self.shown = 0
        self.timers: list[FakeTimer] = []
        self.notifications: list[tuple[str, str]] = []
        self.fg = ForegroundInfo(hwnd=500, pid=4242, exe="editor.exe", is_game=False, is_fullscreen=False,
                                 is_elevated=False)

    def settings(self) -> dict[str, Any]:
        return dict(self._section)

    def settings_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._section)

    def write_settings(self, section: dict[str, Any], *, restart: bool = False) -> None:
        self._section = {**copy.deepcopy(section), "enabled": True}
        self.writes.append(copy.deepcopy(section))

    def safe(self, fn: Callable[..., Any], label: str | None = None) -> Callable[..., Any]:
        return fn

    def is_snoozed(self) -> bool:
        return False

    def add_tray_action(self, label: str, cb: Callable[[], None], **_kw: Any) -> Any:
        self.tray.append((label, cb))
        return SimpleNamespace(set_text=lambda _t: None, set_enabled=lambda _e: None)

    def add_quick_action(self, *a: Any, **k: Any) -> None:
        pass

    def set_tray_status(self, text: str) -> None:
        self.status = text

    def notify(self, title: str, text: str, on_click: Callable[[], None] | None = None, **_kw: Any) -> None:
        self.notifications.append((title, text))

    def show_page(self) -> None:
        self.shown += 1

    def call_soon(self, fn: Callable[[], None]) -> None:
        fn()

    def start_timer(self, ms: int, cb: Callable[[], None], *, single_shot: bool = False) -> FakeTimer:
        t = FakeTimer(ms, cb, single_shot)
        self.timers.append(t)
        return t

    def run_timers(self, ms: int) -> None:
        for t in [t for t in self.timers if t.ms == ms and t.active]:
            if t.single_shot:
                t.active = False
            t.cb()

    def active_timers(self, ms: int) -> list[FakeTimer]:
        return [t for t in self.timers if t.ms == ms and t.active]

    def foreground(self) -> Any:
        return self.fg

    def window_parent(self) -> None:
        return None

    def list_modes(self) -> list[tuple[str, str]]:
        return []


def full_symbols() -> dict[int, str]:
    return {vk: US_CHARS[vk] for vk in keynames.SYMBOLS}
