# PlugSave のテスト共通部品(偽 ctx・偽のタイマー・偽の Win32・一時フォルダのコピー元とドライブ)。
# 本物のドライブには触れない。バックアップ先もコピー元も tmp_path の中に作り、ドライブの種類・ボリューム情報は FakeDriveApi で決める。
from __future__ import annotations

import copy
import datetime as _dt
import logging
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deskkit.modules.plugsave.fakes import FakeDriveApi

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SECRET_PC = "ZzSecretPcName"
SECRET_LABEL = "ZzSecretLabel"
SECRET_SRC = "ZzSecretFolder"
SECRET_FILE = "zz-secret-file.txt"
LETTER = "Q"


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
        out = []
        for r in self.records:
            out.append(r.getMessage())
            if r.exc_info and r.exc_info[1] is not None:
                out.append(str(r.exc_info[1]))
        return "\n".join(out)


class FakeTimer:
    def __init__(self, ms: int, cb: Callable[[], None], single_shot: bool) -> None:
        self.ms = ms
        self.cb = cb
        self.single_shot = single_shot
        self.active = True

    def stop(self) -> None:
        self.active = False

    def fire(self) -> None:
        if not self.active:
            return
        if self.single_shot:
            self.active = False
        self.cb()


class FakeTray:
    def __init__(self) -> None:
        self.visible = True

    def set_visible(self, v: bool) -> None:
        self.visible = v


class FakeCtx:
    def __init__(self, data_dir: Path, section: dict[str, Any] | None = None) -> None:
        self.name = "plugsave"
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.log = logging.getLogger(f"deskkit.plugsave.test.{id(self)}")
        self.log.setLevel(logging.DEBUG)
        self.log.propagate = False
        self.handler = ListHandler()
        self.log.addHandler(self.handler)
        self._section: dict[str, Any] = {"enabled": True, **(section or {})}
        self.notifications: list[tuple[str, str, str, Any]] = []
        self.writes: list[dict[str, Any]] = []
        self.statuses: list[str] = []
        self.snoozed = False
        self.game = False
        self.fullscreen: bool | None = False
        self.timers: list[FakeTimer] = []
        self.native: dict[int, Callable[[int, int], None]] = {}
        self.tray: dict[str, Any] = {}
        self.shown = 0
        self.queue_calls = False
        self.queued: list[Callable[[], None]] = []

    def settings_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._section)

    def write_settings(self, section: dict[str, Any], *, restart: bool = False) -> None:
        self._section = {**copy.deepcopy(section), "enabled": True}
        self.writes.append(copy.deepcopy(section))

    def is_snoozed(self) -> bool:
        return self.snoozed

    def foreground(self) -> Any:
        return SimpleNamespace(is_game=self.game, is_fullscreen=self.fullscreen)

    def notify(self, title: str, text: str, on_click: Callable[[], None] | None = None, *, level: str = "info",
               replace_key: str | None = None) -> None:
        self.notifications.append((title, text, level, on_click))

    def call_soon(self, fn: Callable[[], None]) -> None:
        if self.queue_calls:
            self.queued.append(fn)
        else:
            fn()

    def flush(self) -> None:
        while self.queued:
            self.queued.pop(0)()

    def on_native(self, msg: int, handler: Callable[[int, int], None]) -> None:
        self.native[msg] = handler

    def add_tray_action(self, label: str, cb: Callable[[], None], **_kw: Any) -> FakeTray:
        t = FakeTray()
        self.tray[label] = (cb, t)
        return t

    def set_tray_status(self, text: str) -> None:
        self.statuses.append(text)

    def start_timer(self, ms: int, cb: Callable[[], None], *, single_shot: bool = False) -> FakeTimer:
        t = FakeTimer(ms, cb, single_shot)
        self.timers.append(t)
        return t

    def show_page(self) -> None:
        self.shown += 1

    def window_parent(self) -> None:
        return None

    def active_timers(self, ms: int) -> list[FakeTimer]:
        return [t for t in self.timers if t.active and t.ms == ms]


class Clock:
    def __init__(self) -> None:
        self.t = _dt.datetime(2026, 9, 28, 10, 15, 0).astimezone()
        self.mono = 1000.0

    def now(self) -> _dt.datetime:
        return self.t

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.t += _dt.timedelta(seconds=seconds)
        self.mono += seconds


@dataclass
class Env:
    tmp: Path
    src: Path
    drive: Path
    api: FakeDriveApi
    clock: Clock
    env: dict[str, str]
    ctx: FakeCtx | None = None
    mods: list[Any] = field(default_factory=list)

    def make(self, section: dict[str, Any] | None = None, *, threaded: bool = False, ctx: FakeCtx | None = None,
             **kw: Any) -> Any:
        from deskkit.modules.plugsave.module import PlugSaveModule

        c = ctx or FakeCtx(self.tmp / "home" / "DeskKit" / "plugsave", section)
        self.ctx = c
        opened: list[str] = []
        m = PlugSaveModule(c, api=self.api, env=self.env, threaded=threaded, now=self.clock.now, mono=self.clock.monotonic,
                           sleep=lambda _s: None, opener=opened.append, probe_wait_s=0, **kw)
        m.opened = opened  # type: ignore[attr-defined]
        self.mods.append(m)
        return m

    def pc_dir(self) -> Path:
        return self.drive / "DeskKitバックアップ" / SECRET_PC

    def dest(self, name: str = SECRET_SRC) -> Path:
        return self.pc_dir() / name


def write(p: Path, data: bytes, mtime: float | None = None) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    src = tmp_path / "source" / SECRET_SRC
    src.mkdir(parents=True)
    drive = tmp_path / "drive_q"
    drive.mkdir()
    api = FakeDriveApi()
    api.add(LETTER, str(drive), label=SECRET_LABEL, serial=0x1234ABCD, fs="NTFS")
    e = Env(tmp_path, src, drive, api, Clock(), {
        "COMPUTERNAME": SECRET_PC, "SystemDrive": "C:", "APPDATA": str(tmp_path / "appdata" / "Roaming"),
        "LOCALAPPDATA": str(tmp_path / "appdata" / "Local"), "WINDIR": r"C:\Windows",
    })
    yield e
    for m in e.mods:
        try:
            m.stop()
        except Exception:  # noqa: BLE001
            pass


def ready_module(e: Env, *, first_done: bool = True, section: dict[str, Any] | None = None, **kw: Any) -> tuple[Any, str]:
    """コピー元を足し、偽のドライブを登録した状態の module を返す(start 済み)。"""
    m = e.make(section, **kw)
    m.start()
    assert m.add_source(str(e.src)) is None
    errs: list[str | None] = []
    m.register_drive(LETTER, done=errs.append)
    assert errs == [None]
    did = m.config["drives"][0]["id"]
    if first_done:
        m.config["drives"][0]["first_done"] = True
    return m, did
