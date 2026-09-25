# foreground ウィンドウの hwnd / pid / exe / is_game / is_fullscreen / is_elevated を返す(§9.1)。
# 全画面判定(D-11): foreground の矩形がそのモニタの矩形を覆うかを基本にし、QUNS の値は診断用に併記する。
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes as w
from dataclasses import dataclass
from pathlib import PureWindowsPath

from deskkit import win32

_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


@dataclass(frozen=True)
class ForegroundInfo:
    hwnd: int | None
    pid: int | None
    exe: str | None  # 小文字のファイル名
    is_game: bool
    is_fullscreen: bool | None
    is_elevated: bool | None
    exe_path: str | None = None
    quns: str | None = None

    def unsafe_for_input(self) -> bool:
        return self.is_game or self.is_fullscreen is not False or self.is_elevated is not False

    def reason(self) -> str | None:
        """作用しない理由(game / fullscreen / elevated / unknown)。安全なら None。"""
        if self.is_game:
            return "game"
        if self.is_fullscreen is None:
            return "fullscreen_unknown"
        if self.is_fullscreen:
            return "fullscreen"
        if self.is_elevated is None:
            return "elevated_unknown"
        if self.is_elevated:
            return "elevated"
        return None


def query_quns() -> str | None:
    v = ctypes.c_int()
    try:
        if win32.shell32.SHQueryUserNotificationState(ctypes.byref(v)) != 0:
            return None
    except OSError:
        return None
    return win32.QUNS_NAMES.get(v.value, str(v.value))


def _is_fullscreen(hwnd: int) -> bool | None:
    if win32.class_name(hwnd) in _SHELL_CLASSES:
        return False
    r = w.RECT()
    if not win32.user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    mon = win32.user32.MonitorFromWindow(hwnd, win32.MONITOR_DEFAULTTONEAREST)
    mi = win32.MONITORINFO()
    mi.cbSize = ctypes.sizeof(mi)
    if not mon or not win32.user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        return None
    m = mi.rcMonitor
    return bool(r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom)


def query_foreground(game_processes: frozenset[str], fullscreen_mode: str = "rect") -> ForegroundInfo:
    hwnd = int(win32.user32.GetForegroundWindow() or 0)
    if not hwnd:
        return ForegroundInfo(None, None, None, False, False, False, None, query_quns())
    pid_d = w.DWORD()
    win32.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_d))
    pid = int(pid_d.value) or None
    if pid == os.getpid():
        return ForegroundInfo(hwnd, pid, "deskkit", False, False, False, None, None)
    exe_path = win32.exe_of_pid(pid) if pid else None
    exe = PureWindowsPath(exe_path).name.lower() if exe_path else None
    is_game = bool(exe and exe in game_processes)
    if fullscreen_mode == "off":
        fs: bool | None = False
    elif fullscreen_mode == "none":
        fs = None
    else:
        fs = _is_fullscreen(hwnd)
    elevated = win32.is_pid_elevated(pid) if pid else None
    return ForegroundInfo(hwnd, pid, exe, is_game, fs, elevated, exe_path, query_quns())
