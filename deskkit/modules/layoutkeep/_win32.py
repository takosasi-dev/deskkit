# LayoutKeep が使う Win32 API の ctypes ラッパ。Win32Api Protocol の背後に置き、テストは偽物に差し替える。
# 配置を変える呼び出しは SetWindowPlacement だけ(INV-4)。他プロセスへのメッセージ送信・入力送信は書かない。
# argtypes / restype は全関数に設定する(64bit で HANDLE が切れるのを防ぐ)。
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes as w
from typing import Protocol

from .model import ID_SOURCES, Placement, RawMonitor, RawWindow, Rect


class Win32Api(Protocol):
    def enum_monitors(self) -> list[RawMonitor]: ...
    def enum_windows(self) -> list[int]: ...
    def describe_window(self, hwnd: int) -> RawWindow | None: ...
    def get_placement(self, hwnd: int) -> Placement | None: ...
    def set_placement(self, hwnd: int, placement: Placement) -> int: ...  # 0=成功 / それ以外は Win32 エラーコード
    def is_window(self, hwnd: int) -> bool: ...
    def foreground_window(self) -> int: ...
    def current_pid(self) -> int: ...
    def is_elevated(self, pid: int) -> bool | None: ...  # 読めなければ None(昇格とみなして動かさない)


# ---------------------------------------------------------------- 定数(Windows SDK のヘッダ値)
GWL_EXSTYLE = -20
GW_OWNER = 4
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_CLOAKED = 14
MONITORINFOF_PRIMARY = 0x1
MDT_EFFECTIVE_DPI = 0
EDD_GET_DEVICE_INTERFACE_NAME = 0x1
DISPLAY_DEVICE_ACTIVE = 0x1
QDC_ONLY_ACTIVE_PATHS = 0x2
DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME = 1
DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME = 2
ERROR_SUCCESS = 0
ERROR_INSUFFICIENT_BUFFER = 122
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION_CLASS = 20  # TOKEN_INFORMATION_CLASS.TokenElevation


# ---------------------------------------------------------------- 構造体
class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", w.DWORD),
                ("szDevice", w.WCHAR * 32)]


class DISPLAY_DEVICEW(ctypes.Structure):  # noqa: N801
    _fields_ = [("cb", w.DWORD), ("DeviceName", w.WCHAR * 32), ("DeviceString", w.WCHAR * 128),
                ("StateFlags", w.DWORD), ("DeviceID", w.WCHAR * 128), ("DeviceKey", w.WCHAR * 128)]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", w.UINT), ("flags", w.UINT), ("showCmd", w.UINT), ("ptMinPosition", w.POINT),
                ("ptMaxPosition", w.POINT), ("rcNormalPosition", w.RECT)]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", w.DWORD), ("HighPart", w.LONG)]


class DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):  # noqa: N801
    _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                ("statusFlags", ctypes.c_uint32)]


class DISPLAYCONFIG_RATIONAL(ctypes.Structure):  # noqa: N801
    _fields_ = [("Numerator", ctypes.c_uint32), ("Denominator", ctypes.c_uint32)]


class DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):  # noqa: N801
    _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                ("outputTechnology", ctypes.c_uint32), ("rotation", ctypes.c_uint32), ("scaling", ctypes.c_uint32),
                ("refreshRate", DISPLAYCONFIG_RATIONAL), ("scanLineOrdering", ctypes.c_uint32),
                ("targetAvailable", w.BOOL), ("statusFlags", ctypes.c_uint32)]


class DISPLAYCONFIG_PATH_INFO(ctypes.Structure):  # noqa: N801
    _fields_ = [("sourceInfo", DISPLAYCONFIG_PATH_SOURCE_INFO), ("targetInfo", DISPLAYCONFIG_PATH_TARGET_INFO),
                ("flags", ctypes.c_uint32)]


class DISPLAYCONFIG_MODE_INFO(ctypes.Structure):  # noqa: N801
    # 共用体部分(targetMode / sourceMode / desktopImageInfo)は中身を使わないので 48 バイトの領域として持つ
    _fields_ = [("infoType", ctypes.c_uint32), ("id", ctypes.c_uint32), ("adapterId", LUID),
                ("union", ctypes.c_uint64 * 6)]


class DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):  # noqa: N801
    _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32), ("adapterId", LUID), ("id", ctypes.c_uint32)]


class DISPLAYCONFIG_SOURCE_DEVICE_NAME(ctypes.Structure):  # noqa: N801
    _fields_ = [("header", DISPLAYCONFIG_DEVICE_INFO_HEADER), ("viewGdiDeviceName", w.WCHAR * 32)]


class DISPLAYCONFIG_TARGET_DEVICE_NAME(ctypes.Structure):  # noqa: N801
    _fields_ = [("header", DISPLAYCONFIG_DEVICE_INFO_HEADER), ("flags", ctypes.c_uint32),
                ("outputTechnology", ctypes.c_uint32), ("edidManufactureId", ctypes.c_uint16),
                ("edidProductCodeId", ctypes.c_uint16), ("connectorInstance", ctypes.c_uint32),
                ("monitorFriendlyDeviceName", w.WCHAR * 64), ("monitorDevicePath", w.WCHAR * 128)]


assert ctypes.sizeof(DISPLAYCONFIG_PATH_INFO) == 72
assert ctypes.sizeof(DISPLAYCONFIG_MODE_INFO) == 64
assert ctypes.sizeof(DISPLAYCONFIG_TARGET_DEVICE_NAME) == 420
assert ctypes.sizeof(WINDOWPLACEMENT) == 44

WNDENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
MONITORENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HMONITOR, w.HDC, ctypes.POINTER(w.RECT), w.LPARAM)


def _rect(r: w.RECT) -> Rect:
    return (int(r.left), int(r.top), int(r.right), int(r.bottom))


def decode_edid_manufacturer(raw: int) -> str:
    """DISPLAYCONFIG_TARGET_DEVICE_NAME.edidManufactureId(バイト順が入れ替わった 5bit×3 文字)を 'DEL' 等にする。"""
    v = ((raw & 0xFF) << 8) | ((raw >> 8) & 0xFF)
    chars = [((v >> 10) & 0x1F), ((v >> 5) & 0x1F), (v & 0x1F)]
    if all(1 <= c <= 26 for c in chars):
        return "".join(chr(c + 64) for c in chars)
    return f"{raw:04X}"


class RealWin32:
    """実機の Win32Api。"""

    def __init__(self) -> None:
        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        a = ctypes.WinDLL("advapi32", use_last_error=True)
        self._u, self._k, self._a = u, k, a
        try:
            self._dwm: ctypes.WinDLL | None = ctypes.WinDLL("dwmapi", use_last_error=True)
        except OSError:
            self._dwm = None
        try:
            self._shcore: ctypes.WinDLL | None = ctypes.WinDLL("shcore", use_last_error=True)
        except OSError:
            self._shcore = None

        u.EnumDisplayMonitors.argtypes = [w.HDC, ctypes.POINTER(w.RECT), MONITORENUMPROC, w.LPARAM]
        u.EnumDisplayMonitors.restype = w.BOOL
        u.GetMonitorInfoW.argtypes = [w.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
        u.GetMonitorInfoW.restype = w.BOOL
        u.EnumDisplayDevicesW.argtypes = [w.LPCWSTR, w.DWORD, ctypes.POINTER(DISPLAY_DEVICEW), w.DWORD]
        u.EnumDisplayDevicesW.restype = w.BOOL
        u.GetDisplayConfigBufferSizes.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                                                  ctypes.POINTER(ctypes.c_uint32)]
        u.GetDisplayConfigBufferSizes.restype = w.LONG
        u.QueryDisplayConfig.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                                         ctypes.POINTER(DISPLAYCONFIG_PATH_INFO), ctypes.POINTER(ctypes.c_uint32),
                                         ctypes.POINTER(DISPLAYCONFIG_MODE_INFO), ctypes.c_void_p]
        u.QueryDisplayConfig.restype = w.LONG
        u.DisplayConfigGetDeviceInfo.argtypes = [ctypes.POINTER(DISPLAYCONFIG_DEVICE_INFO_HEADER)]
        u.DisplayConfigGetDeviceInfo.restype = w.LONG
        u.EnumWindows.argtypes = [WNDENUMPROC, w.LPARAM]
        u.EnumWindows.restype = w.BOOL
        u.IsWindow.argtypes = [w.HWND]
        u.IsWindow.restype = w.BOOL
        u.IsWindowVisible.argtypes = [w.HWND]
        u.IsWindowVisible.restype = w.BOOL
        u.IsIconic.argtypes = [w.HWND]
        u.IsIconic.restype = w.BOOL
        self._get_long = getattr(u, "GetWindowLongPtrW", None) or u.GetWindowLongW
        self._get_long.argtypes = [w.HWND, ctypes.c_int]
        self._get_long.restype = ctypes.c_ssize_t
        u.GetWindow.argtypes = [w.HWND, w.UINT]
        u.GetWindow.restype = w.HWND
        u.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        u.GetClassNameW.restype = ctypes.c_int
        u.GetWindowTextLengthW.argtypes = [w.HWND]
        u.GetWindowTextLengthW.restype = ctypes.c_int
        u.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        u.GetWindowTextW.restype = ctypes.c_int
        u.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        u.GetWindowThreadProcessId.restype = w.DWORD
        u.GetWindowPlacement.argtypes = [w.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
        u.GetWindowPlacement.restype = w.BOOL
        u.SetWindowPlacement.argtypes = [w.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
        u.SetWindowPlacement.restype = w.BOOL
        u.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
        u.GetWindowRect.restype = w.BOOL
        u.GetForegroundWindow.argtypes = []
        u.GetForegroundWindow.restype = w.HWND

        k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k.OpenProcess.restype = w.HANDLE
        k.CloseHandle.argtypes = [w.HANDLE]
        k.CloseHandle.restype = w.BOOL
        k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
        k.QueryFullProcessImageNameW.restype = w.BOOL
        k.GetProcessTimes.argtypes = [w.HANDLE, ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME),
                                      ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME)]
        k.GetProcessTimes.restype = w.BOOL
        a.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
        a.OpenProcessToken.restype = w.BOOL
        a.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
        a.GetTokenInformation.restype = w.BOOL

        if self._dwm is not None:
            self._dwm.DwmGetWindowAttribute.argtypes = [w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD]
            self._dwm.DwmGetWindowAttribute.restype = ctypes.c_long
        if self._shcore is not None:
            self._shcore.GetDpiForMonitor.argtypes = [w.HMONITOR, ctypes.c_int, ctypes.POINTER(w.UINT),
                                                      ctypes.POINTER(w.UINT)]
            self._shcore.GetDpiForMonitor.restype = ctypes.c_long

    # ------------------------------------------------------------ モニタ
    def enum_monitors(self) -> list[RawMonitor]:
        handles: list[int] = []

        def cb(hmon: int | None, _hdc: int | None, _rc: object, _lp: int) -> bool:
            if hmon:
                handles.append(int(hmon))
            return True

        proc = MONITORENUMPROC(cb)
        self._u.EnumDisplayMonitors(None, None, proc, 0)
        dc = self._displayconfig_ids()
        out: list[RawMonitor] = []
        for h in handles:
            mi = MONITORINFOEXW()
            mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
            if not self._u.GetMonitorInfoW(h, ctypes.byref(mi)):
                continue
            device = mi.szDevice
            ids: dict[str, str | None] = dict.fromkeys(ID_SOURCES)
            ids["device_interface"] = self._device_interface(device)
            path, edid = dc.get(device.upper(), (None, None))
            ids["displayconfig_path"] = path
            ids["edid"] = edid
            out.append(RawMonitor(device=device, rect=_rect(mi.rcMonitor), work=_rect(mi.rcWork),
                                  primary=bool(mi.dwFlags & MONITORINFOF_PRIMARY), dpi=self._dpi(h), ids=ids))
        return out

    def _dpi(self, hmon: int) -> int | None:
        if self._shcore is None:
            return None
        x, y = w.UINT(), w.UINT()
        if self._shcore.GetDpiForMonitor(hmon, MDT_EFFECTIVE_DPI, ctypes.byref(x), ctypes.byref(y)) != 0:
            return None
        return int(x.value)

    def _device_interface(self, device: str) -> str | None:
        dd = DISPLAY_DEVICEW()
        first: str | None = None
        i = 0
        while True:
            dd.cb = ctypes.sizeof(DISPLAY_DEVICEW)
            if not self._u.EnumDisplayDevicesW(device, i, ctypes.byref(dd), EDD_GET_DEVICE_INTERFACE_NAME):
                break
            did: str | None = str(dd.DeviceID) or None
            if did and dd.StateFlags & DISPLAY_DEVICE_ACTIVE:
                return did
            if first is None and did:
                first = did
            i += 1
            if i > 16:
                break
        return first

    def _displayconfig_ids(self) -> dict[str, tuple[str | None, str | None]]:
        """GDI デバイス名(大文字)→(モニタデバイスパス, EDID 製造者-製品コード)。"""
        out: dict[str, tuple[str | None, str | None]] = {}
        for _ in range(3):
            np_, nm = ctypes.c_uint32(), ctypes.c_uint32()
            if self._u.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(np_), ctypes.byref(nm)) != 0:
                return out
            paths = (DISPLAYCONFIG_PATH_INFO * max(1, np_.value))()
            modes = (DISPLAYCONFIG_MODE_INFO * max(1, nm.value))()
            rc = self._u.QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(np_), paths, ctypes.byref(nm), modes, None)
            if rc == ERROR_INSUFFICIENT_BUFFER:
                continue
            if rc != ERROR_SUCCESS:
                return out
            for i in range(np_.value):
                p = paths[i]
                src = DISPLAYCONFIG_SOURCE_DEVICE_NAME()
                src.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME
                src.header.size = ctypes.sizeof(src)
                src.header.adapterId = p.sourceInfo.adapterId
                src.header.id = p.sourceInfo.id
                if self._u.DisplayConfigGetDeviceInfo(ctypes.byref(src.header)) != 0:
                    continue
                tgt = DISPLAYCONFIG_TARGET_DEVICE_NAME()
                tgt.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME
                tgt.header.size = ctypes.sizeof(tgt)
                tgt.header.adapterId = p.targetInfo.adapterId
                tgt.header.id = p.targetInfo.id
                if self._u.DisplayConfigGetDeviceInfo(ctypes.byref(tgt.header)) != 0:
                    continue
                path = tgt.monitorDevicePath or None
                edid = None
                if tgt.flags & 0x4:  # edidIdsValid
                    edid = f"{decode_edid_manufacturer(tgt.edidManufactureId)}-{tgt.edidProductCodeId:04X}"
                key = src.viewGdiDeviceName.upper()
                if key not in out:  # 複製表示は最初のパスを使う
                    out[key] = (path, edid)
            return out
        return out

    # ------------------------------------------------------------ ウィンドウ
    def enum_windows(self) -> list[int]:
        hwnds: list[int] = []

        def cb(hwnd: int | None, _lp: int) -> bool:
            if hwnd:
                hwnds.append(int(hwnd))
            return True

        proc = WNDENUMPROC(cb)
        self._u.EnumWindows(proc, 0)
        return hwnds

    def describe_window(self, hwnd: int) -> RawWindow | None:
        u = self._u
        if not u.IsWindow(hwnd):
            return None
        pid_d = w.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_d))
        pid = int(pid_d.value)
        buf = ctypes.create_unicode_buffer(256)
        n = u.GetClassNameW(hwnd, buf, 256)
        cls = buf.value if n else ""
        tl = u.GetWindowTextLengthW(hwnd)
        tbuf = ctypes.create_unicode_buffer(max(1, tl + 1))
        u.GetWindowTextW(hwnd, tbuf, len(tbuf))
        exstyle = int(self._get_long(hwnd, GWL_EXSTYLE))
        owner = u.GetWindow(hwnd, GW_OWNER)
        exe, err, start = self._process_info(pid)
        rc = w.RECT()
        srect = _rect(rc) if u.GetWindowRect(hwnd, ctypes.byref(rc)) else None
        return RawWindow(
            hwnd=hwnd, pid=pid, cls=cls, title=tbuf.value, visible=bool(u.IsWindowVisible(hwnd)),
            iconic=bool(u.IsIconic(hwnd)), toolwindow=bool(exstyle & WS_EX_TOOLWINDOW), owned=bool(owner),
            cloaked=self._cloaked(hwnd), exe=exe, exe_error=err, proc_start=start,
            placement=self.get_placement(hwnd), screen_rect=srect,
        )

    def _cloaked(self, hwnd: int) -> bool:
        if self._dwm is None:
            return False
        v = w.DWORD()
        if self._dwm.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v)) != 0:
            return False
        return v.value != 0

    def _process_info(self, pid: int) -> tuple[str | None, int, int | None]:
        """(exe フルパス, 失敗時の Win32 エラー, プロセス開始時刻 FILETIME)。"""
        if pid <= 0:
            return None, 0, None
        h = self._k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None, ctypes.get_last_error(), None
        try:
            buf = ctypes.create_unicode_buffer(32768)
            n = w.DWORD(len(buf))
            exe: str | None = None
            err = 0
            if self._k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                exe = buf.value
            else:
                err = ctypes.get_last_error()
            c, e, kt, ut = w.FILETIME(), w.FILETIME(), w.FILETIME(), w.FILETIME()
            start: int | None = None
            if self._k.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)):
                start = (int(c.dwHighDateTime) << 32) | int(c.dwLowDateTime)
            return exe, err, start
        finally:
            self._k.CloseHandle(h)

    def get_placement(self, hwnd: int) -> Placement | None:
        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(WINDOWPLACEMENT)
        if not self._u.GetWindowPlacement(hwnd, ctypes.byref(wp)):
            return None
        return Placement(show_cmd=int(wp.showCmd), normal_rect=_rect(wp.rcNormalPosition),
                         min_pos=(int(wp.ptMinPosition.x), int(wp.ptMinPosition.y)),
                         max_pos=(int(wp.ptMaxPosition.x), int(wp.ptMaxPosition.y)), flags=int(wp.flags))

    def set_placement(self, hwnd: int, placement: Placement) -> int:
        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(WINDOWPLACEMENT)
        wp.flags = placement.flags
        wp.showCmd = placement.show_cmd
        wp.ptMinPosition = w.POINT(*placement.min_pos)
        wp.ptMaxPosition = w.POINT(*placement.max_pos)
        wp.rcNormalPosition = w.RECT(*placement.normal_rect)
        ctypes.set_last_error(0)
        if self._u.SetWindowPlacement(hwnd, ctypes.byref(wp)):
            return 0
        return ctypes.get_last_error() or -1

    def is_window(self, hwnd: int) -> bool:
        return bool(self._u.IsWindow(hwnd))

    def foreground_window(self) -> int:
        return int(self._u.GetForegroundWindow() or 0)

    def current_pid(self) -> int:
        return os.getpid()

    def is_elevated(self, pid: int) -> bool | None:
        """プロセスのトークンが昇格しているか。開けない・読めないなら None(読むだけ。権限は変えない)。"""
        if pid <= 0:
            return None
        h = self._k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            tok = w.HANDLE()
            if not self._a.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(tok)):
                return None
            try:
                val = w.DWORD()
                ret = w.DWORD()
                if not self._a.GetTokenInformation(tok, TOKEN_ELEVATION_CLASS, ctypes.byref(val), ctypes.sizeof(val),
                                                   ctypes.byref(ret)):
                    return None
                return bool(val.value)
            finally:
                self._k.CloseHandle(tok)
        finally:
            self._k.CloseHandle(h)
