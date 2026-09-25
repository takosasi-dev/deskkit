# host が使う Win32 API の ctypes 定義と定数(値は Windows SDK のヘッダ定義に合わせる)。
# モジュールは各自の _win32.py を持ち、これを直接 import しない(host 内部用)。
from __future__ import annotations

import ctypes
from ctypes import wintypes as w

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

# --- winuser.h
WM_DISPLAYCHANGE = 0x007E
WM_POWERBROADCAST = 0x0218
WM_CLIPBOARDUPDATE = 0x031D
WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
MONITOR_DEFAULTTONEAREST = 0x00000002
# --- winerror.h
ERROR_HOTKEY_ALREADY_REGISTERED = 1409
ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183
# --- winnt.h / processthreadsapi.h
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION_CLASS = 20  # TOKEN_INFORMATION_CLASS.TokenElevation
# --- windef.h DPI_AWARENESS_CONTEXT
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
DPI_AWARENESS_NAMES = {-1: "invalid", 0: "unaware", 1: "system_aware", 2: "per_monitor_aware"}
# --- shellapi.h QUERY_USER_NOTIFICATION_STATE
QUNS_NAMES = {
    1: "not_present", 2: "busy", 3: "running_d3d_full_screen",
    4: "presentation_mode", 5: "accepts_notifications", 6: "quiet_time", 7: "app",
}


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", w.HWND), ("message", w.UINT), ("wParam", w.WPARAM), ("lParam", w.LPARAM),
        ("time", w.DWORD), ("pt", w.POINT), ("lPrivate", w.DWORD),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", w.DWORD)]


user32.RegisterHotKey.argtypes = [w.HWND, ctypes.c_int, w.UINT, w.UINT]
user32.RegisterHotKey.restype = w.BOOL
user32.UnregisterHotKey.argtypes = [w.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = w.BOOL
user32.AddClipboardFormatListener.argtypes = [w.HWND]
user32.AddClipboardFormatListener.restype = w.BOOL
user32.RemoveClipboardFormatListener.argtypes = [w.HWND]
user32.RemoveClipboardFormatListener.restype = w.BOOL
user32.GetForegroundWindow.restype = w.HWND
user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
user32.GetWindowThreadProcessId.restype = w.DWORD
user32.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
user32.GetWindowRect.restype = w.BOOL
user32.MonitorFromWindow.argtypes = [w.HWND, w.DWORD]
user32.MonitorFromWindow.restype = w.HMONITOR
user32.GetMonitorInfoW.argtypes = [w.HMONITOR, ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = w.BOOL
user32.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.SendMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.SendMessageW.restype = w.LPARAM
user32.RegisterWindowMessageW.argtypes = [w.LPCWSTR]
user32.RegisterWindowMessageW.restype = w.UINT
user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
user32.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.AreDpiAwarenessContextsEqual.restype = w.BOOL
user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetProcessDpiAwarenessContext.restype = w.BOOL
user32.AllowSetForegroundWindow.argtypes = [w.DWORD]
user32.AllowSetForegroundWindow.restype = w.BOOL

kernel32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
kernel32.OpenProcess.restype = w.HANDLE
kernel32.CloseHandle.argtypes = [w.HANDLE]
kernel32.CloseHandle.restype = w.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = w.BOOL
kernel32.GetCurrentProcessId.restype = w.DWORD
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, w.BOOL, w.LPCWSTR]
kernel32.CreateMutexW.restype = w.HANDLE
kernel32.AttachConsole.argtypes = [w.DWORD]
kernel32.AttachConsole.restype = w.BOOL

advapi32.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
advapi32.OpenProcessToken.restype = w.BOOL
advapi32.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
advapi32.GetTokenInformation.restype = w.BOOL

shell32.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]
shell32.SHQueryUserNotificationState.restype = ctypes.c_long


def last_error() -> int:
    return ctypes.get_last_error()


def dpi_awareness_text() -> str:
    """現在スレッドの DPI awareness を 'per_monitor_aware_v2' 等の文字列で返す。"""
    try:
        ctx = user32.GetThreadDpiAwarenessContext()
        if user32.AreDpiAwarenessContextsEqual(ctx, ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
            return "per_monitor_aware_v2"
        return DPI_AWARENESS_NAMES.get(user32.GetAwarenessFromDpiAwarenessContext(ctx), "unknown")
    except (AttributeError, OSError):
        return "unknown"


def exe_of_pid(pid: int) -> str | None:
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        n = w.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return buf.value
        return None
    finally:
        kernel32.CloseHandle(h)


def is_pid_elevated(pid: int) -> bool | None:
    """トークンの昇格状態。読めなければ None(D-10)。"""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        tok = w.HANDLE()
        if not advapi32.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(tok)):
            return None
        try:
            val = w.DWORD()
            ret = w.DWORD()
            if not advapi32.GetTokenInformation(tok, TOKEN_ELEVATION_CLASS, ctypes.byref(val),
                                                ctypes.sizeof(val), ctypes.byref(ret)):
                return None
            return bool(val.value)
        finally:
            kernel32.CloseHandle(tok)
    finally:
        kernel32.CloseHandle(h)


def class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    n = user32.GetClassNameW(hwnd, buf, 256)
    return buf.value if n else ""
