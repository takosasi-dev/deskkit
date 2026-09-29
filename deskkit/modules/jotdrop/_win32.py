# JotDrop が使う Win32 API(ctypes)。Win32Api(Protocol)の背後に置き、テストでは fakes.FakeWin32 に差し替える。
# 追記のハンドルは FILE_APPEND_DATA だけで書く(FILE_WRITE_DATA・GENERIC_WRITE を持たない。INV-1)。切り詰めは取り消し(J-18)だけ。
# キーの状態・フック・入力の送信・前面の制限の回避に使う API は定義しない(INV-4)。argtypes / restype は必ず設定する。
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes as w
from typing import Protocol

# --- winnt.h: アクセス権
FILE_READ_DATA = 0x0001
FILE_WRITE_DATA = 0x0002
FILE_APPEND_DATA = 0x0004
FILE_READ_ATTRIBUTES = 0x0080
SYNCHRONIZE = 0x00100000
GENERIC_WRITE = 0x40000000
# --- 共有・作り方(fileapi.h)
FILE_SHARE_NONE = 0
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
FILE_SHARE_DELETE = 0x4
CREATE_NEW = 1
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
FILE_BEGIN = 0
FILE_END = 2
# --- winerror.h
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_ACCESS_DENIED = 5
ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33
ERROR_FILE_EXISTS = 80
# --- GetDriveTypeW
DRIVE_REMOTE = 4

# 追記に使う権限(J-8)。FILE_WRITE_DATA と GENERIC_WRITE を含めない(INV-1・AC-2)
APPEND_ACCESS = FILE_READ_DATA | FILE_APPEND_DATA | FILE_READ_ATTRIBUTES | SYNCHRONIZE
# 照合(J-19)は読むだけ。他のアプリの書き込みを妨げない
READ_ACCESS = FILE_READ_DATA | FILE_READ_ATTRIBUTES | SYNCHRONIZE
READ_SHARE = FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
# 取り消し(J-18)だけが使う。共有 0(排他)で開き、条件がそろったときだけ切り詰める
TRUNCATE_ACCESS = FILE_READ_DATA | FILE_WRITE_DATA | FILE_READ_ATTRIBUTES | SYNCHRONIZE

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class Win32Api(Protocol):
    """失敗は OSError(winerror 付き)で返す。handle は int。"""

    def create_file(self, path: str, access: int, share: int, disposition: int) -> int: ...
    def read_at(self, handle: int, offset: int, size: int) -> bytes: ...
    def append(self, handle: int, data: bytes) -> None: ...
    def size(self, handle: int) -> int: ...
    def truncate(self, handle: int, size: int) -> None: ...
    def close(self, handle: int) -> None: ...
    def is_dir(self, path: str) -> bool: ...
    def make_dir(self, path: str) -> None: ...
    def drive_type(self, root: str) -> int: ...
    def foreground_window(self) -> int: ...
    def set_foreground(self, hwnd: int) -> bool: ...
    def is_window(self, hwnd: int) -> bool: ...
    def window_pid(self, hwnd: int) -> int: ...
    def window_rect(self, hwnd: int) -> tuple[int, int, int, int] | None: ...


def _bind(dll: ctypes.WinDLL, name: str, args: list[object], res: object) -> None:
    fn = getattr(dll, name)
    fn.argtypes = args
    fn.restype = res


def _err() -> OSError:
    code = ctypes.get_last_error()
    return OSError(0, "win32", None, code)


class RealWin32:
    """実機の Win32Api。DLL の読み込みと型定義はインスタンスを作るときに行う。"""

    def __init__(self) -> None:
        if sys.platform != "win32":  # pragma: no cover - Windows 専用
            raise OSError("JotDrop は Windows 専用です")
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.u32 = ctypes.WinDLL("user32", use_last_error=True)
        k, u = self.k32, self.u32
        _bind(k, "CreateFileW", [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE)
        _bind(k, "ReadFile", [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p], w.BOOL)
        _bind(k, "WriteFile", [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p], w.BOOL)
        _bind(k, "SetFilePointerEx", [w.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), w.DWORD], w.BOOL)
        _bind(k, "GetFileSizeEx", [w.HANDLE, ctypes.POINTER(ctypes.c_longlong)], w.BOOL)
        _bind(k, "SetEndOfFile", [w.HANDLE], w.BOOL)
        _bind(k, "CloseHandle", [w.HANDLE], w.BOOL)
        _bind(k, "GetDriveTypeW", [w.LPCWSTR], w.UINT)
        _bind(k, "GetFileAttributesW", [w.LPCWSTR], w.DWORD)
        _bind(k, "CreateDirectoryW", [w.LPCWSTR, ctypes.c_void_p], w.BOOL)
        _bind(u, "GetForegroundWindow", [], w.HWND)
        _bind(u, "SetForegroundWindow", [w.HWND], w.BOOL)
        _bind(u, "IsWindow", [w.HWND], w.BOOL)
        _bind(u, "GetWindowThreadProcessId", [w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD)
        _bind(u, "GetWindowRect", [w.HWND, ctypes.POINTER(w.RECT)], w.BOOL)

    # ---- ファイル
    def create_file(self, path: str, access: int, share: int, disposition: int) -> int:
        h = self.k32.CreateFileW(path, access, share, None, disposition, FILE_ATTRIBUTE_NORMAL, None)
        if h is None or h == INVALID_HANDLE_VALUE:
            raise _err()
        return int(h)

    def _seek(self, handle: int, offset: int, whence: int) -> None:
        if not self.k32.SetFilePointerEx(handle, offset, None, whence):
            raise _err()

    def read_at(self, handle: int, offset: int, size: int) -> bytes:
        self._seek(handle, offset, FILE_BEGIN)
        buf = ctypes.create_string_buffer(max(1, size))
        out = bytearray()
        while len(out) < size:
            got = w.DWORD(0)
            if not self.k32.ReadFile(handle, ctypes.byref(buf), size - len(out), ctypes.byref(got), None):
                raise _err()
            if got.value == 0:
                break
            out += buf.raw[: got.value]
        return bytes(out)

    def append(self, handle: int, data: bytes) -> None:
        """末尾へ書く。FILE_APPEND_DATA だけのハンドルなので、ローカルのファイルでは既存のデータを上書きできない(J-8)。"""
        self._seek(handle, 0, FILE_END)
        buf = ctypes.create_string_buffer(data, len(data))
        done = 0
        while done < len(data):
            got = w.DWORD(0)
            ptr = ctypes.c_void_p(ctypes.addressof(buf) + done)
            if not self.k32.WriteFile(handle, ptr, len(data) - done, ctypes.byref(got), None):
                raise _err()
            if got.value == 0:
                raise OSError(0, "short write", None, 0)
            done += got.value

    def size(self, handle: int) -> int:
        v = ctypes.c_longlong(0)
        if not self.k32.GetFileSizeEx(handle, ctypes.byref(v)):
            raise _err()
        return int(v.value)

    def truncate(self, handle: int, size: int) -> None:
        self._seek(handle, size, FILE_BEGIN)
        if not self.k32.SetEndOfFile(handle):
            raise _err()

    def close(self, handle: int) -> None:
        self.k32.CloseHandle(handle)

    def is_dir(self, path: str) -> bool:
        a = int(self.k32.GetFileAttributesW(path))
        return a != 0xFFFFFFFF and bool(a & 0x10)

    def make_dir(self, path: str) -> None:
        """フォルダを1つだけ作る(既定の「ドキュメント\\JotDrop」だけに使う。J-16)。"""
        if not self.k32.CreateDirectoryW(path, None):
            raise _err()

    def drive_type(self, root: str) -> int:
        return int(self.k32.GetDriveTypeW(root))

    # ---- 窓(前面の窓を覚えて戻すだけ。J-4)
    def foreground_window(self) -> int:
        return int(self.u32.GetForegroundWindow() or 0)

    def set_foreground(self, hwnd: int) -> bool:
        return bool(hwnd) and bool(self.u32.SetForegroundWindow(hwnd))

    def is_window(self, hwnd: int) -> bool:
        return bool(hwnd) and bool(self.u32.IsWindow(hwnd))

    def window_pid(self, hwnd: int) -> int:
        pid = w.DWORD(0)
        self.u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def window_rect(self, hwnd: int) -> tuple[int, int, int, int] | None:
        r = w.RECT()
        if not hwnd or not self.u32.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        return int(r.left), int(r.top), int(r.right), int(r.bottom)
