# StartupWatch の Win32 の窓口。レジストリは読むための権限(KEY_READ)だけで開き、変更の知らせ・フォルダの変更の知らせ・
# event・待ち・既知フォルダ・ドライブの種類・動いているプログラムのファイル名を ctypes で読む。Api の Protocol の背後に置き、
# テストでは fakes.FakeApi に替える。書き込み系の API は宣言もしない(INV-1・INV-2)。
from __future__ import annotations

import ctypes
import os
import winreg
from ctypes import wintypes as w
from typing import Any, Protocol

HKCU = winreg.HKEY_CURRENT_USER
HKLM = winreg.HKEY_LOCAL_MACHINE
KEY_READ = 0x20019          # STANDARD_RIGHTS_READ | KEY_QUERY_VALUE | KEY_ENUMERATE_SUB_KEYS | KEY_NOTIFY
KEY_WOW64_64KEY = 0x0100
KEY_WOW64_32KEY = 0x0200
REG_NOTIFY_CHANGE_LAST_SET = 0x4
FILE_NOTIFY_CHANGE_FILE_NAME = 0x1
FILE_NOTIFY_CHANGE_LAST_WRITE = 0x10
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
WAIT_FAILED = 0xFFFFFFFF
INFINITE = 0xFFFFFFFF
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
DRIVE_REMOTE = 4
TH32CS_SNAPPROCESS = 0x2

ERROR_FILE_NOT_FOUND = 2
ERROR_ACCESS_DENIED = 5

# 待ちの結果(wait_any の戻り値): 0 以上は合図の来た添字
WAIT_RESULT_TIMEOUT = -1
WAIT_RESULT_FAILED = -2

FOLDER_IDS = {
    "startup": "{B97D20BB-F46A-4C97-BA10-5E3608430854}",         # FOLDERID_Startup
    "common_startup": "{82A5EA35-D9CD-47C5-9629-E15D2F714E6E}",  # FOLDERID_CommonStartup
}

RegValue = tuple[str, int, Any]  # (値の名前, 型, 中身)


class Api(Protocol):
    def reg_read(self, root: int, subkey: str, view: int) -> tuple[int, list[RegValue]]:
        """鍵を KEY_READ | view で開いて値を全部読む。(0, 値) / (エラー番号, [])。"""
        ...

    def reg_open_notify(self, root: int, subkey: str, view: int) -> tuple[int, int]:
        """変更の知らせ用に KEY_READ | view で開く。(0, ハンドル) / (エラー番号, 0)。"""
        ...

    def reg_notify(self, hkey: int, event: int) -> int: ...
    def reg_close(self, hkey: int) -> None: ...
    def create_event(self) -> int: ...
    def set_event(self, h: int) -> None: ...
    def close_handle(self, h: int) -> None: ...
    def find_first_change(self, path: str) -> int | None: ...
    def find_next_change(self, h: int) -> bool: ...
    def find_close_change(self, h: int) -> None: ...
    def wait_any(self, handles: list[int], timeout_ms: int) -> int: ...
    def known_folder(self, which: str) -> str | None: ...
    def is_remote(self, path: str) -> bool: ...
    def list_dir(self, path: str) -> list[tuple[str, bool]]:
        """(名前, フォルダか)。読めなければ OSError。"""
        ...

    def lnk_target(self, path: str) -> str: ...
    def expand(self, text: str) -> str: ...
    def process_names(self) -> set[str] | None:
        """動いているプログラムのファイル名(小文字)。読めなければ None。"""
        ...


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD), ("Data4", ctypes.c_ubyte * 8)]


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", w.LONG),
        ("dwFlags", w.DWORD), ("szExeFile", w.WCHAR * 260),
    ]


_libs: dict[str, Any] = {}


def _dlls() -> dict[str, Any]:
    if _libs:
        return _libs
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    sh = ctypes.WinDLL("shell32", use_last_error=True)
    ole = ctypes.WinDLL("ole32", use_last_error=True)
    adv.RegOpenKeyExW.argtypes = [w.HKEY, w.LPCWSTR, w.DWORD, w.DWORD, ctypes.POINTER(w.HKEY)]
    adv.RegOpenKeyExW.restype = w.LONG
    adv.RegNotifyChangeKeyValue.argtypes = [w.HKEY, w.BOOL, w.DWORD, w.HANDLE, w.BOOL]
    adv.RegNotifyChangeKeyValue.restype = w.LONG
    adv.RegCloseKey.argtypes = [w.HKEY]
    adv.RegCloseKey.restype = w.LONG
    k32.CreateEventW.argtypes = [ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR]
    k32.CreateEventW.restype = w.HANDLE
    k32.SetEvent.argtypes = [w.HANDLE]
    k32.SetEvent.restype = w.BOOL
    k32.CloseHandle.argtypes = [w.HANDLE]
    k32.CloseHandle.restype = w.BOOL
    k32.FindFirstChangeNotificationW.argtypes = [w.LPCWSTR, w.BOOL, w.DWORD]
    k32.FindFirstChangeNotificationW.restype = w.HANDLE
    k32.FindNextChangeNotification.argtypes = [w.HANDLE]
    k32.FindNextChangeNotification.restype = w.BOOL
    k32.FindCloseChangeNotification.argtypes = [w.HANDLE]
    k32.FindCloseChangeNotification.restype = w.BOOL
    k32.WaitForMultipleObjects.argtypes = [w.DWORD, ctypes.POINTER(w.HANDLE), w.BOOL, w.DWORD]
    k32.WaitForMultipleObjects.restype = w.DWORD
    k32.GetDriveTypeW.argtypes = [w.LPCWSTR]
    k32.GetDriveTypeW.restype = w.UINT
    k32.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    k32.CreateToolhelp32Snapshot.restype = w.HANDLE
    k32.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    k32.Process32FirstW.restype = w.BOOL
    k32.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    k32.Process32NextW.restype = w.BOOL
    sh.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(_GUID), w.DWORD, w.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
    sh.SHGetKnownFolderPath.restype = ctypes.c_long
    ole.CLSIDFromString.argtypes = [w.LPCWSTR, ctypes.POINTER(_GUID)]
    ole.CLSIDFromString.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    _libs.update(adv=adv, k32=k32, sh=sh, ole=ole)
    return _libs


def _handle_value(h: Any) -> int:
    return int(h or 0)


class RealApi:
    """本物の Win32。読むだけ。"""

    def reg_read(self, root: int, subkey: str, view: int) -> tuple[int, list[RegValue]]:
        try:
            key = winreg.OpenKeyEx(root, subkey, 0, KEY_READ | view)
        except OSError as e:
            return int(getattr(e, "winerror", 0) or ERROR_FILE_NOT_FOUND), []
        out: list[RegValue] = []
        try:
            i = 0
            while True:
                try:
                    name, data, typ = winreg.EnumValue(key, i)
                except OSError:
                    break  # ERROR_NO_MORE_ITEMS(259)
                out.append((str(name), int(typ), data))
                i += 1
        finally:
            key.Close()
        return 0, out

    def reg_open_notify(self, root: int, subkey: str, view: int) -> tuple[int, int]:
        h = w.HKEY()
        rc = int(_dlls()["adv"].RegOpenKeyExW(w.HKEY(root), subkey, 0, KEY_READ | view, ctypes.byref(h)))
        if rc != 0:
            return rc, 0
        return 0, _handle_value(h.value)

    def reg_notify(self, hkey: int, event: int) -> int:
        # サブキーは見ない(FALSE)・値の追加/削除/変更・event で非同期(TRUE)
        return int(_dlls()["adv"].RegNotifyChangeKeyValue(w.HKEY(hkey), False, REG_NOTIFY_CHANGE_LAST_SET, w.HANDLE(event), True))

    def reg_close(self, hkey: int) -> None:
        if hkey:
            _dlls()["adv"].RegCloseKey(w.HKEY(hkey))

    def create_event(self) -> int:
        h = _dlls()["k32"].CreateEventW(None, False, False, None)  # 自動リセット
        if not h:
            raise OSError(ctypes.get_last_error(), "CreateEventW")
        return _handle_value(h)

    def set_event(self, h: int) -> None:
        if h:
            _dlls()["k32"].SetEvent(w.HANDLE(h))

    def close_handle(self, h: int) -> None:
        if h:
            _dlls()["k32"].CloseHandle(w.HANDLE(h))

    def find_first_change(self, path: str) -> int | None:
        h = _dlls()["k32"].FindFirstChangeNotificationW(path, False,
                                                        FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_LAST_WRITE)
        v = _handle_value(h)
        if not v or h == INVALID_HANDLE_VALUE or v == INVALID_HANDLE_VALUE:
            return None
        return v

    def find_next_change(self, h: int) -> bool:
        return bool(_dlls()["k32"].FindNextChangeNotification(w.HANDLE(h)))

    def find_close_change(self, h: int) -> None:
        if h:
            _dlls()["k32"].FindCloseChangeNotification(w.HANDLE(h))

    def wait_any(self, handles: list[int], timeout_ms: int) -> int:
        arr = (w.HANDLE * len(handles))(*[w.HANDLE(h) for h in handles])
        rc = int(_dlls()["k32"].WaitForMultipleObjects(len(handles), arr, False, timeout_ms & 0xFFFFFFFF))
        if rc == WAIT_TIMEOUT:
            return WAIT_RESULT_TIMEOUT
        if WAIT_OBJECT_0 <= rc < WAIT_OBJECT_0 + len(handles):
            return rc - WAIT_OBJECT_0
        return WAIT_RESULT_FAILED

    def known_folder(self, which: str) -> str | None:
        gid = FOLDER_IDS.get(which)
        if gid is None:
            return None
        d = _dlls()
        guid = _GUID()
        if d["ole"].CLSIDFromString(gid, ctypes.byref(guid)) != 0:
            return None
        p = ctypes.c_wchar_p()
        hr = int(d["sh"].SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(p)))
        try:
            if hr != 0 or not p.value:
                return None
            return str(p.value)
        finally:
            if p:
                d["ole"].CoTaskMemFree(p)

    def is_remote(self, path: str) -> bool:
        if path.startswith("\\\\"):
            return True
        drive = os.path.splitdrive(path)[0]
        if not drive:
            return False
        return int(_dlls()["k32"].GetDriveTypeW(drive + "\\")) == DRIVE_REMOTE

    def list_dir(self, path: str) -> list[tuple[str, bool]]:
        out: list[tuple[str, bool]] = []
        with os.scandir(path) as it:
            for e in it:
                try:
                    out.append((e.name, e.is_dir(follow_symlinks=False)))
                except OSError:
                    continue
        return out

    def lnk_target(self, path: str) -> str:
        from PySide6.QtCore import QFileInfo

        try:
            return str(QFileInfo(path).symLinkTarget() or "")
        except Exception:  # noqa: BLE001 - 読めなければ空(§10)
            return ""

    def expand(self, text: str) -> str:
        return os.path.expandvars(text)

    def process_names(self) -> set[str] | None:
        k32 = _dlls()["k32"]
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        v = _handle_value(snap)
        if not v or v == INVALID_HANDLE_VALUE:
            return None
        names: set[str] = set()
        try:
            pe = _PROCESSENTRY32W()
            pe.dwSize = ctypes.sizeof(pe)
            ok = k32.Process32FirstW(snap, ctypes.byref(pe))
            while ok:
                names.add(str(pe.szExeFile).lower())
                ok = k32.Process32NextW(snap, ctypes.byref(pe))
        finally:
            k32.CloseHandle(snap)
        return names
