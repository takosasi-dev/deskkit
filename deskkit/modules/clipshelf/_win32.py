# ClipShelf が使う Win32 API(クリップボード・プロセス・foreground・SendInput・DPAPI)の ctypes 実装。
# Win32Api プロトコルの背後に置き、テストでは fakes.FakeWin32 に差し替える。argtypes/restype は必ず設定する。
# 本文を扱う関数は値を返すだけで、ログ・例外メッセージには本文も長さも入れない(INV-1)。
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
from typing import Any, Protocol

# --- winuser.h 標準クリップボード形式
CF_TEXT = 1
CF_UNICODETEXT = 13
STANDARD_FORMAT_NAMES: dict[int, str] = {
    1: "CF_TEXT", 2: "CF_BITMAP", 3: "CF_METAFILEPICT", 4: "CF_SYLK", 5: "CF_DIF", 6: "CF_TIFF",
    7: "CF_OEMTEXT", 8: "CF_DIB", 9: "CF_PALETTE", 10: "CF_PENDATA", 11: "CF_RIFF", 12: "CF_WAVE",
    13: "CF_UNICODETEXT", 14: "CF_ENHMETAFILE", 15: "CF_HDROP", 16: "CF_LOCALE", 17: "CF_DIBV5",
    0x0080: "CF_OWNERDISPLAY", 0x0081: "CF_DSPTEXT", 0x0082: "CF_DSPBITMAP",
    0x0083: "CF_DSPMETAFILEPICT", 0x008E: "CF_DSPENHMETAFILE",
}
WM_CLIPBOARDUPDATE = 0x031D
# 除外形式(登録形式名)と自己印の形式名
FMT_EXCLUDE_MONITOR = "ExcludeClipboardContentFromMonitorProcessing"
FMT_CAN_INCLUDE_HISTORY = "CanIncludeInClipboardHistory"
FMT_CAN_UPLOAD_CLOUD = "CanUploadToCloudClipboard"
FMT_ORIGIN = "DeskKit.ClipShelf.Origin"
EXCLUSION_FORMATS: tuple[str, ...] = (FMT_EXCLUDE_MONITOR, FMT_CAN_INCLUDE_HISTORY, FMT_CAN_UPLOAD_CLOUD)

# --- winbase.h / processthreadsapi.h / tlhelp32.h / winuser.h / dpapi.h
GMEM_MOVEABLE = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_V = 0x56
CRYPTPROTECT_UI_FORBIDDEN = 0x1
_MAX_SMALL_FORMAT_BYTES = 4096  # 除外形式・自己印の値として読む上限(本文形式には使わない)


class CryptoError(Exception):
    """DPAPI の失敗。メッセージには Win32 エラーコードだけを入れる(本文を入れない)。"""

    def __init__(self, op: str, code: int) -> None:
        super().__init__(f"{op} failed: win32 error {code}")
        self.code = code


class Win32Api(Protocol):
    # クリップボード
    def open_clipboard(self, hwnd: int) -> bool: ...
    def close_clipboard(self) -> None: ...
    def empty_clipboard(self) -> bool: ...
    def enum_formats(self) -> list[int]: ...
    def format_name(self, fmt: int) -> str: ...
    def register_format(self, name: str) -> int: ...
    def is_format_available(self, fmt: int) -> bool: ...
    def get_data_bytes(self, fmt: int) -> bytes | None: ...
    def get_unicode_text(self, limit: int) -> str | None: ...
    def set_data_bytes(self, fmt: int, data: bytes) -> bool: ...
    def set_unicode_text(self, text: str) -> bool: ...
    def clipboard_owner(self) -> int: ...
    def sequence_number(self) -> int: ...
    # プロセス・ウィンドウ
    def window_pid(self, hwnd: int) -> int: ...
    def process_image_path(self, pid: int) -> str | None: ...
    def current_pid(self) -> int: ...
    def running_exe_names(self) -> list[str]: ...
    def is_window(self, hwnd: int) -> bool: ...
    def set_foreground_window(self, hwnd: int) -> bool: ...
    def window_rect(self, hwnd: int) -> tuple[int, int, int, int] | None: ...
    # 入力(INV-6 の判定を通った後だけ呼ぶ)
    def send_ctrl_v(self) -> int: ...
    # DPAPI(ユーザースコープ)
    def protect(self, data: bytes) -> bytes: ...
    def unprotect(self, data: bytes) -> bytes: ...


# ------------------------------------------------------------------ ctypes 構造体
ULONG_PTR = ctypes.c_size_t


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD), ("dwFlags", w.DWORD), ("time", w.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("u", _INPUTUNION)]


class _DATA_BLOB(ctypes.Structure):  # noqa: N801 - Win32 の構造体名に合わせる
    _fields_ = [("cbData", w.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD), ("th32DefaultHeapID", ULONG_PTR),
        ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
        ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD), ("szExeFile", w.WCHAR * 260),
    ]


def _bind(dll: Any, name: str, argtypes: list[Any], restype: Any) -> None:
    fn = getattr(dll, name)
    fn.argtypes = argtypes
    fn.restype = restype


class RealWin32:
    """実機の Win32Api。DLL の読み込みと型定義はインスタンス生成時に行う。"""

    def __init__(self) -> None:
        self.u32 = ctypes.WinDLL("user32", use_last_error=True)
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        u, k, c = self.u32, self.k32, self.crypt32
        _bind(u, "OpenClipboard", [w.HWND], w.BOOL)
        _bind(u, "CloseClipboard", [], w.BOOL)
        _bind(u, "EmptyClipboard", [], w.BOOL)
        _bind(u, "EnumClipboardFormats", [w.UINT], w.UINT)
        _bind(u, "GetClipboardFormatNameW", [w.UINT, w.LPWSTR, ctypes.c_int], ctypes.c_int)
        _bind(u, "RegisterClipboardFormatW", [w.LPCWSTR], w.UINT)
        _bind(u, "IsClipboardFormatAvailable", [w.UINT], w.BOOL)
        _bind(u, "GetClipboardData", [w.UINT], w.HANDLE)
        _bind(u, "SetClipboardData", [w.UINT, w.HANDLE], w.HANDLE)
        _bind(u, "GetClipboardOwner", [], w.HWND)
        _bind(u, "GetClipboardSequenceNumber", [], w.DWORD)
        _bind(u, "GetWindowThreadProcessId", [w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD)
        _bind(u, "IsWindow", [w.HWND], w.BOOL)
        _bind(u, "SetForegroundWindow", [w.HWND], w.BOOL)
        _bind(u, "GetWindowRect", [w.HWND, ctypes.POINTER(w.RECT)], w.BOOL)
        _bind(u, "SendInput", [w.UINT, ctypes.POINTER(_INPUT), ctypes.c_int], w.UINT)
        _bind(k, "GlobalAlloc", [w.UINT, ctypes.c_size_t], w.HGLOBAL)
        _bind(k, "GlobalFree", [w.HGLOBAL], w.HGLOBAL)
        _bind(k, "GlobalLock", [w.HGLOBAL], ctypes.c_void_p)
        _bind(k, "GlobalUnlock", [w.HGLOBAL], w.BOOL)
        _bind(k, "GlobalSize", [w.HGLOBAL], ctypes.c_size_t)
        _bind(k, "OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)
        _bind(k, "CloseHandle", [w.HANDLE], w.BOOL)
        _bind(k, "QueryFullProcessImageNameW", [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)], w.BOOL)
        _bind(k, "GetCurrentProcessId", [], w.DWORD)
        _bind(k, "LocalFree", [w.HLOCAL], w.HLOCAL)
        _bind(k, "CreateToolhelp32Snapshot", [w.DWORD, w.DWORD], ctypes.c_void_p)
        _bind(k, "Process32FirstW", [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)], w.BOOL)
        _bind(k, "Process32NextW", [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)], w.BOOL)
        _bind(c, "CryptProtectData", [ctypes.POINTER(_DATA_BLOB), w.LPCWSTR, ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p,
                                      ctypes.c_void_p, w.DWORD, ctypes.POINTER(_DATA_BLOB)], w.BOOL)
        _bind(c, "CryptUnprotectData", [ctypes.POINTER(_DATA_BLOB), ctypes.POINTER(w.LPWSTR), ctypes.POINTER(_DATA_BLOB),
                                        ctypes.c_void_p, ctypes.c_void_p, w.DWORD, ctypes.POINTER(_DATA_BLOB)], w.BOOL)

    # ------------------------------------------------------------ クリップボード
    def open_clipboard(self, hwnd: int) -> bool:
        return bool(self.u32.OpenClipboard(hwnd or None))

    def close_clipboard(self) -> None:
        self.u32.CloseClipboard()

    def empty_clipboard(self) -> bool:
        return bool(self.u32.EmptyClipboard())

    def enum_formats(self) -> list[int]:
        out: list[int] = []
        fmt = 0
        while True:
            fmt = int(self.u32.EnumClipboardFormats(fmt))
            if fmt == 0:
                break
            out.append(fmt)
            if len(out) > 512:  # 異常な形式数で無限に回らないように
                break
        return out

    def format_name(self, fmt: int) -> str:
        if fmt in STANDARD_FORMAT_NAMES:
            return STANDARD_FORMAT_NAMES[fmt]
        buf = ctypes.create_unicode_buffer(256)
        n = self.u32.GetClipboardFormatNameW(fmt, buf, 256)
        if n > 0:
            return buf.value
        if 0x0200 <= fmt <= 0x02FF:
            return f"CF_PRIVATE+{fmt - 0x0200}"
        if 0x0300 <= fmt <= 0x03FF:
            return f"CF_GDIOBJ+{fmt - 0x0300}"
        return f"#{fmt}"

    def register_format(self, name: str) -> int:
        return int(self.u32.RegisterClipboardFormatW(name))

    def is_format_available(self, fmt: int) -> bool:
        return bool(fmt) and bool(self.u32.IsClipboardFormatAvailable(fmt))

    def get_data_bytes(self, fmt: int) -> bytes | None:
        """除外形式・自己印の小さな値だけを読む(CF_UNICODETEXT には使わない)。"""
        if fmt == CF_UNICODETEXT:
            raise ValueError("本文形式は get_unicode_text で読む")
        h = self.u32.GetClipboardData(fmt)
        if not h:
            return None
        size = int(self.k32.GlobalSize(h))
        p = self.k32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.string_at(p, min(size, _MAX_SMALL_FORMAT_BYTES))
        finally:
            self.k32.GlobalUnlock(h)

    def get_unicode_text(self, limit: int) -> str | None:
        """CF_UNICODETEXT を最大 limit+1 文字まで読む(超過は呼び出し側が len で判定して捨てる)。"""
        h = self.u32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        size = int(self.k32.GlobalSize(h))
        p = self.k32.GlobalLock(h)
        if not p:
            return None
        try:
            n_chars = min(size // 2, max(0, limit) + 2)
            raw = ctypes.wstring_at(p, n_chars)
        finally:
            self.k32.GlobalUnlock(h)
        nul = raw.find("\x00")
        return raw if nul < 0 else raw[:nul]

    def _set_global(self, fmt: int, data: bytes) -> bool:
        h = self.k32.GlobalAlloc(GMEM_MOVEABLE, max(1, len(data)))
        if not h:
            return False
        p = self.k32.GlobalLock(h)
        if not p:
            self.k32.GlobalFree(h)
            return False
        try:
            ctypes.memmove(p, data, len(data))
        finally:
            self.k32.GlobalUnlock(h)
        if not self.u32.SetClipboardData(fmt, h):
            self.k32.GlobalFree(h)
            return False
        return True  # 成功したらメモリの所有権はシステムに移る

    def set_data_bytes(self, fmt: int, data: bytes) -> bool:
        return self._set_global(fmt, data)

    def set_unicode_text(self, text: str) -> bool:
        return self._set_global(CF_UNICODETEXT, text.encode("utf-16-le") + b"\x00\x00")

    def clipboard_owner(self) -> int:
        return int(self.u32.GetClipboardOwner() or 0)

    def sequence_number(self) -> int:
        return int(self.u32.GetClipboardSequenceNumber())

    # ------------------------------------------------------------ プロセス・ウィンドウ
    def window_pid(self, hwnd: int) -> int:
        pid = w.DWORD()
        self.u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def process_image_path(self, pid: int) -> str | None:
        if not pid:
            return None
        h = self.k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            buf = ctypes.create_unicode_buffer(32768)
            n = w.DWORD(len(buf))
            if self.k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return buf.value
            return None
        finally:
            self.k32.CloseHandle(h)

    def current_pid(self) -> int:
        return int(self.k32.GetCurrentProcessId())

    def running_exe_names(self) -> list[str]:
        snap = self.k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == ctypes.c_void_p(-1).value:
            return []
        names: set[str] = set()
        try:
            pe = _PROCESSENTRY32W()
            pe.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            ok = self.k32.Process32FirstW(snap, ctypes.byref(pe))
            while ok:
                if pe.szExeFile:
                    names.add(str(pe.szExeFile).lower())
                ok = self.k32.Process32NextW(snap, ctypes.byref(pe))
        finally:
            self.k32.CloseHandle(snap)
        return sorted(names)

    def is_window(self, hwnd: int) -> bool:
        return bool(hwnd) and bool(self.u32.IsWindow(hwnd))

    def set_foreground_window(self, hwnd: int) -> bool:
        return bool(hwnd) and bool(self.u32.SetForegroundWindow(hwnd))

    def window_rect(self, hwnd: int) -> tuple[int, int, int, int] | None:
        r = w.RECT()
        if not hwnd or not self.u32.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        return int(r.left), int(r.top), int(r.right), int(r.bottom)

    # ------------------------------------------------------------ 入力
    def send_ctrl_v(self) -> int:
        seq = [(VK_CONTROL, 0), (VK_V, 0), (VK_V, KEYEVENTF_KEYUP), (VK_CONTROL, KEYEVENTF_KEYUP)]
        arr = (_INPUT * len(seq))()
        for i, (vk, flags) in enumerate(seq):
            arr[i].type = INPUT_KEYBOARD
            arr[i].u.ki = _KEYBDINPUT(vk, 0, flags, 0, 0)
        return int(self.u32.SendInput(len(seq), arr, ctypes.sizeof(_INPUT)))

    # ------------------------------------------------------------ DPAPI
    def protect(self, data: bytes) -> bytes:
        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
        blob_out = _DATA_BLOB()
        try:
            ok = self.crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None, None,
                                               CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
            if not ok:
                raise CryptoError("CryptProtectData", ctypes.get_last_error())
            try:
                return ctypes.string_at(blob_out.pbData, blob_out.cbData)
            finally:
                self.k32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))
        finally:
            ctypes.memset(buf, 0, len(data))  # 平文の作業領域を消す(Python 側のコピーは消せない。§4)

    def unprotect(self, data: bytes) -> bytes:
        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
        blob_out = _DATA_BLOB()
        ok = self.crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None,
                                             CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
        if not ok:
            raise CryptoError("CryptUnprotectData", ctypes.get_last_error())
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.memset(blob_out.pbData, 0, blob_out.cbData)
            self.k32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))
