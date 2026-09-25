# selftest とテストで使う偽物: プロセス・起動・電源(powercfg)・Core Audio・既定のハンドラ・ModuleContext・UI。
# 実物の OS には一切触らない(実機の電源プラン・音量・アプリを変えない)。
# 呼び出しを記録し、検査できるようにする。
from __future__ import annotations

import copy
import logging
import queue
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from deskkit.modules.modeshift.model import Plan, Step
from deskkit.modules.modeshift.system import (
    Backends,
    CloseRequest,
    LaunchResult,
    MasterState,
    PowerScheme,
    ProcInfo,
    SessionInfo,
)

# 実機の powercfg /list に出た GUID(§8.6: 実物に無い GUID をテストに書かない)。偽の電源の中でだけ使う
GUID_A = "381b4222-f694-41f0-9685-ff5bb260df2e"   # バランス
GUID_B = "89ee7eba-0db4-4b3a-8c33-69689521f195"   # dynabook 標準


class FakeProcesses:
    def __init__(self) -> None:
        self.procs: dict[int, str] = {}
        self.paths: dict[int, str] = {}
        self.close_behavior: dict[str, str] = {}   # exe → "exit" / "stay" / "notray"
        self.gate: threading.Event | None = None     # wait_exit をここで止める(busy のテスト用)
        self.posted: list[set[int]] = []
        self.forced: list[int] = []
        self._next = 5000
        self.lock = threading.Lock()

    def add(self, exe: str, path: str | None = None) -> int:
        with self.lock:
            self._next += 1
            pid = self._next
            self.procs[pid] = exe.lower()
            if path:
                self.paths[pid] = path
            return pid

    def list_processes(self) -> list[ProcInfo]:
        with self.lock:
            return [ProcInfo(p, e) for p, e in self.procs.items()]

    def exe_path(self, pid: int) -> str | None:
        return self.paths.get(pid)

    def own_pid(self) -> int:
        return 1

    def is_alive(self, pid: int) -> bool:
        return pid in self.procs

    def post_close(self, pids: set[int]) -> CloseRequest:
        self.posted.append(set(pids))
        n = 0
        for pid in pids:
            beh = self.close_behavior.get(self.procs.get(pid, ""), "exit")
            if beh == "notray":
                continue
            n += 1
            if beh == "exit":
                with self.lock:
                    self.procs.pop(pid, None)
        return CloseRequest(n, 0)

    def wait_exit(self, pids: set[int], timeout_s: float, abort: threading.Event) -> set[int]:
        if self.gate is not None:
            self.gate.wait(10)
        return {p for p in pids if p in self.procs}

    def force_terminate_confirmed(self, pid: int) -> tuple[bool, str]:
        self.forced.append(pid)
        with self.lock:
            self.procs.pop(pid, None)
        return True, "偽: 強制終了"


class FakeLauncher:
    def __init__(self, procs: FakeProcesses) -> None:
        self.procs = procs
        self.launched: list[tuple[str, list[str], str | None]] = []
        self.fail_with: str | None = None

    def launch(self, path: str, args: list[str], cwd: str | None) -> LaunchResult:
        self.launched.append((path, list(args), cwd))
        if self.fail_with:
            return LaunchResult(False, self.fail_with)
        pid = self.procs.add(Path(path).name.lower(), path)
        return LaunchResult(True, f"PID {pid}", pid)


class FakePower:
    """powercfg の偽物。実機の電源プランには触らない。"""

    def __init__(self, active: str = GUID_A) -> None:
        self.schemes = [PowerScheme(GUID_A, "バランス", False), PowerScheme(GUID_B, "dynabook 標準", False)]
        self.active: str | None = active
        self.set_calls: list[str] = []
        self.broken = False   # 出力書式が想定外(GUID が読めない)

    def list_schemes(self) -> list[PowerScheme]:
        return [PowerScheme(s.guid, s.name, s.guid == self.active) for s in self.schemes]

    def get_active(self) -> str | None:
        return None if self.broken else self.active

    def set_active(self, guid: str) -> tuple[bool, str]:
        self.set_calls.append(guid)
        self.active = guid
        return True, "読み戻して一致を確認"


class FakeAudio:
    def __init__(self) -> None:
        self.master: MasterState | None = MasterState(0.62, False, "{dev-1}")
        self.sessions: dict[str, SessionInfo] = {}
        self.master_calls: list[tuple[float | None, bool | None]] = []
        self.session_calls: list[dict[str, float]] = []

    def add_session(self, sid: str, pid: int, level: float = 1.0) -> None:
        self.sessions[sid] = SessionInfo(sid, pid, level, False)

    def get_master(self) -> MasterState | None:
        return self.master

    def set_master(self, level: float | None, mute: bool | None) -> MasterState | None:
        self.master_calls.append((level, mute))
        if self.master is None:
            return None
        m = self.master
        self.master = MasterState(m.level if level is None else level, m.mute if mute is None else mute, m.device_id)
        return self.master

    def list_sessions(self) -> list[SessionInfo]:
        return list(self.sessions.values())

    def set_sessions(self, levels: dict[str, float]) -> dict[str, float | None]:
        self.session_calls.append(dict(levels))
        out: dict[str, float | None] = {}
        for sid, lv in levels.items():
            s = self.sessions.get(sid)
            if s is None:
                out[sid] = None
                continue
            self.sessions[sid] = SessionInfo(sid, s.pid, lv, s.mute)
            out[sid] = lv
        return out


class FakeOpener:
    def __init__(self) -> None:
        self.opened: list[tuple[str, str]] = []

    def open_path(self, path: str) -> tuple[bool, str]:
        self.opened.append(("path", path))
        return True, "偽: 開いた"

    def open_url(self, url: str) -> tuple[bool, str]:
        self.opened.append(("url", url))
        return True, "偽: 渡した"


@dataclass
class FakeSystem:
    procs: FakeProcesses
    launcher: FakeLauncher
    power: FakePower
    audio: FakeAudio
    opener: FakeOpener

    def backends(self) -> Backends:
        return Backends(self.procs, self.launcher, self.power, self.audio, self.opener)


def fake_system() -> FakeSystem:
    p = FakeProcesses()
    return FakeSystem(p, FakeLauncher(p), FakePower(), FakeAudio(), FakeOpener())


# ------------------------------------------------------------------ ModuleContext の偽物
@dataclass(frozen=True)
class FakeForeground:
    hwnd: int | None = None
    pid: int | None = None
    exe: str | None = None
    is_game: bool = False
    is_fullscreen: bool | None = False
    is_elevated: bool | None = False

    def unsafe_for_input(self) -> bool:
        return self.is_game or self.is_fullscreen is not False or self.is_elevated is not False

    def reason(self) -> str | None:
        return "game" if self.is_game else None


class FakeTrayItem:
    def __init__(self, label: str, cb: Callable[[], None], checkable: bool, checked: bool, submenu: str | None) -> None:
        self.label, self.cb, self.checkable, self.checked, self.submenu = label, cb, checkable, checked, submenu
        self.enabled = True
        self.visible = True

    def set_text(self, text: str) -> None:
        self.label = text

    def set_checked(self, checked: bool) -> None:
        self.checked = checked

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def set_visible(self, visible: bool) -> None:
        self.visible = visible


class _Proxy:
    def __init__(self, hk: FakeHotkeys, name: str) -> None:
        self._hk, self._name = hk, name

    def connect(self, cb: Callable[[], None]) -> None:
        self._hk.callbacks.setdefault(self._name, []).append(cb)


class FakeHotkeys:
    def __init__(self) -> None:
        self.registered: dict[str, str] = {}
        self.callbacks: dict[str, list[Callable[[], None]]] = {}
        self.refuse: set[str] = set()

    def register_text(self, name: str, text: str | None) -> bool:
        if not text:
            return False
        if text in self.refuse:
            return False
        self.registered[name] = text
        return True

    def triggered(self, name: str) -> _Proxy:
        return _Proxy(self, name)

    def unregister(self, name: str) -> None:
        self.registered.pop(name, None)
        self.callbacks.pop(name, None)

    def fire(self, name: str) -> None:
        for cb in list(self.callbacks.get(name, [])):
            cb()


class _Timer:
    def stop(self) -> None:
        pass


class FakeCtx:
    def __init__(self, data_dir: Path, section: Mapping[str, Any], games: frozenset[str] = frozenset()) -> None:
        self.name = "modeshift"
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log = logging.getLogger("deskkit.modeshift")
        self._section = copy.deepcopy(dict(section))
        self._games = games
        self.fg: Any = FakeForeground()
        self.notifications: list[tuple[str, str, str]] = []
        self.emitted: list[tuple[str, dict[str, Any]]] = []
        self.handlers: dict[str, list[Callable[[Mapping[str, Any]], None]]] = {}
        self.hotkeys = FakeHotkeys()
        self.tray: list[FakeTrayItem] = []
        self.quick: list[tuple[str, Callable[[], None], str, str | None, Callable[[], bool] | None]] = []
        self.status = ""
        self.writes = 0
        self._q: queue.Queue[Callable[[], None]] = queue.Queue()

    def settings(self) -> Mapping[str, Any]:
        return MappingProxyType(copy.deepcopy(self._section))

    def settings_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._section)

    def write_settings(self, section: Mapping[str, Any], *, restart: bool = False) -> None:
        self._section = copy.deepcopy(dict(section))
        self.writes += 1

    def game_processes(self) -> frozenset[str]:
        return self._games

    def add_tray_action(self, label: str, callback: Callable[[], None], *, checkable: bool = False,
                        checked: bool = False, submenu: str | None = None) -> FakeTrayItem:
        it = FakeTrayItem(label, callback, checkable, checked, submenu)
        self.tray.append(it)
        return it

    def add_quick_action(self, label: str, callback: Callable[[], None], *, keywords: str = "",
                         glyph: str | None = None, enabled: Callable[[], bool] | None = None) -> None:
        self.quick.append((label, callback, keywords, glyph, enabled))

    def clear_quick_actions(self) -> None:
        self.quick.clear()

    def add_tray_separator(self, submenu: str | None = None) -> None:
        pass

    def clear_tray_actions(self, submenu: str | None = None) -> None:
        self.tray = [t for t in self.tray if t.submenu != submenu]

    def set_tray_status(self, text: str) -> None:
        self.status = text

    def notify(self, title: str, text: str, on_click: Callable[[], None] | None = None, *, level: str = "info") -> None:
        self.notifications.append((title, text, level))

    def foreground(self) -> Any:
        return self.fg

    def emit(self, event: str, payload: Mapping[str, Any]) -> None:
        self.emitted.append((event, dict(payload)))
        for h in list(self.handlers.get(event, [])):
            h(dict(payload))

    def on(self, event: str, handler: Callable[[Mapping[str, Any]], None]) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def safe(self, fn: Callable[..., Any], label: str | None = None) -> Callable[..., Any]:
        def w(*a: Any, **k: Any) -> Any:
            try:
                return fn(*a, **k)
            except Exception:  # noqa: BLE001
                self.log.exception("safe: %s", label)
                return None
        return w

    def call_soon(self, fn: Callable[[], None]) -> None:
        self._q.put(fn)

    def pump(self, timeout_s: float = 0.0) -> int:
        """call_soon で積まれた関数をこのスレッドで実行する(テスト用)。"""
        n = 0
        try:
            fn = self._q.get(timeout=timeout_s) if timeout_s else self._q.get_nowait()
            while True:
                fn()
                n += 1
                fn = self._q.get_nowait()
        except queue.Empty:
            return n

    def start_timer(self, interval_ms: int, callback: Callable[[], None], *, single_shot: bool = False) -> _Timer:
        return _Timer()

    def window_parent(self) -> None:
        return None

    def hidden_hwnd(self) -> int:
        return 0

    def dpi_awareness(self) -> str:
        return "per_monitor_aware_v2"


class FakeUi:
    def __init__(self) -> None:
        self.previews: list[Plan] = []
        self.progress: list[tuple[str, int]] = []
        self.finished: list[Plan] = []
        self.force_answer = False
        self.force_asked: list[tuple[str, int]] = []

    def show_preview(self, plan: Plan) -> None:
        self.previews.append(plan)

    def plan_progress(self, plan: Plan, step: Step) -> None:
        self.progress.append((plan.run_id, step.index))

    def plan_finished(self, plan: Plan) -> None:
        self.finished.append(plan)

    def confirm_force(self, exe: str, pid: int) -> bool:
        self.force_asked.append((exe, pid))
        return self.force_answer


def sample_section(base: Path) -> dict[str, Any]:
    """8 種類のアクションを全部含む "game" と、無効になるべきモードを含む設定(偽の exe を base に作る)。"""
    exe = base / "apps" / "editor.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"")
    folder = base / "docs"
    folder.mkdir(parents=True, exist_ok=True)
    return {
        "enabled": True,
        "preview": "unconfirmed_only",
        "allow_force_kill": False,
        "undo_hotkey": "Ctrl+Alt+Z",
        "modes": [
            {"name": "game", "label": "ゲーム", "hotkey": "Ctrl+Alt+G", "confirmed_hash": None, "actions": [
                {"type": "power_plan", "guid": GUID_B},
                {"type": "master_volume", "level": 0.3, "mute": False},
                {"type": "app_volume", "exe": "chat.exe", "level": 0.4},
                {"type": "close_app", "exe": "mail.exe", "timeout_s": 1, "force_on_timeout": False},
                {"type": "open_path", "path": str(folder)},
                {"type": "open_url", "url": "https://example.com/page?token=secret"},
                {"type": "launch_app", "path": str(exe), "args": ["--quiet"], "cwd": None, "skip_if_running": True},
                {"type": "layout_apply", "layout": "game", "wait_s": 3},
            ]},
            {"name": "study", "label": "勉強", "hotkey": "Ctrl+Alt+S", "actions": [
                {"type": "master_volume", "level": 0.1},
            ]},
            {"name": "bad_type", "label": "未知の種別", "actions": [{"type": "wallpaper", "path": "x"}]},
            {"name": "bad_close", "label": "ゲームを閉じる", "actions": [{"type": "close_app", "exe": "game.exe"}]},
            {"name": "bad_url", "label": "FTP", "actions": [{"type": "open_url", "url": "ftp://example.com/"}]},
        ],
        "auto_switch": {"enabled": False, "poll_interval_s": None, "rules": []},
    }


def populate(sysm: FakeSystem) -> None:
    """chat.exe(音あり)と mail.exe(WM_CLOSE で終わる)を動かしておく。"""
    chat = sysm.procs.add("chat.exe", r"C:\Apps\chat.exe")
    sysm.procs.add("mail.exe", r"C:\Apps\mail.exe")
    sysm.audio.add_session("sess-chat-1", chat, 1.0)
