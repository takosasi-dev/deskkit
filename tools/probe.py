# 診断スニペット(共通仕様 §9.6)。読み取り専用で、書き込み・設定変更をしない。
# 使い方: python tools/probe.py dpi | fg | elev
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes as w
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskkit import win32  # noqa: E402
from deskkit.foreground import query_quns  # noqa: E402


def cmd_dpi() -> None:
    print("awareness:", win32.dpi_awareness_text())


def cmd_fg() -> None:
    for _ in range(10):
        hwnd = win32.user32.GetForegroundWindow()
        pid = w.DWORD()
        win32.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = win32.exe_of_pid(pid.value) if pid.value else None
        r = w.RECT()
        win32.user32.GetWindowRect(hwnd, ctypes.byref(r))
        mi = win32.MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        win32.user32.GetMonitorInfoW(win32.user32.MonitorFromWindow(hwnd, win32.MONITOR_DEFAULTTONEAREST), ctypes.byref(mi))
        m = mi.rcMonitor
        print(f"exe={Path(exe).name if exe else None} class={win32.class_name(hwnd)} "
              f"win=({r.left},{r.top},{r.right},{r.bottom}) mon=({m.left},{m.top},{m.right},{m.bottom}) quns={query_quns()}")
        time.sleep(1)


def cmd_elev() -> None:
    hwnd = win32.user32.GetForegroundWindow()
    pid = w.DWORD()
    win32.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    v = win32.is_pid_elevated(pid.value)
    print(f"pid={pid.value} elevated={v}" + ("" if v is not None else f" last_error={win32.last_error()}"))


if __name__ == "__main__":
    if "--dpi-aware" in sys.argv:
        win32.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(win32.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
    {"dpi": cmd_dpi, "fg": cmd_fg, "elev": cmd_elev}.get(sys.argv[1] if len(sys.argv) > 1 else "dpi", cmd_dpi)()
