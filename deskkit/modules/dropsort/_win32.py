# DropSort が使う Win32 API(ctypes)と定数、それを隠す Protocol(Win32Api / WatchApi)。
# 定数値は Windows SDK のヘッダ(KnownFolders.h / winnt.h / fileapi.h / winbase.h / winerror.h)に合わせる。
# ファイル本文は読まない。読むのは属性・サイズ・時刻と Zone.Identifier ストリームだけ(INV-6)。
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes as w
from dataclasses import dataclass
from typing import Protocol

# --- winnt.h: ファイル属性
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
# --- winnt.h: GetVolumeInformationW のフラグ
FILE_NAMED_STREAMS = 0x00040000
# --- fileapi.h: GetDriveTypeW の戻り値
DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6
# --- winbase.h: MoveFileExW のフラグ。書き込み完了を待つフラグだけを使う(別ボリュームへの複製・上書きのフラグは定義しない。INV-3/INV-5)
MOVEFILE_WRITE_THROUGH = 0x00000008
# --- winbase.h: CopyFileExW のフラグ(移動先に同名があれば失敗させる。INV-5)
COPY_FILE_FAIL_IF_EXISTS = 0x00000001
# --- winnt.h / fileapi.h: CreateFileW
GENERIC_READ = 0x80000000
FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
VOLUME_NAME_DOS = 0x0
# --- winnt.h: ReadDirectoryChangesW の通知フィルタ
FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
FILE_NOTIFY_CHANGE_DIR_NAME = 0x00000002
FILE_NOTIFY_CHANGE_ATTRIBUTES = 0x00000004
FILE_NOTIFY_CHANGE_SIZE = 0x00000008
FILE_NOTIFY_CHANGE_LAST_WRITE = 0x00000010
# --- winnt.h: OpenThread のアクセス権(CancelSynchronousIo に必要)
THREAD_TERMINATE = 0x0001
# --- winerror.h
ERROR_SUCCESS = 0
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_HANDLE = 6
ERROR_NOT_SAME_DEVICE = 17
ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33
ERROR_FILE_EXISTS = 80
ERROR_ALREADY_EXISTS = 183
ERROR_FILENAME_EXCED_RANGE = 206
ERROR_OPERATION_ABORTED = 995
ERROR_NOTIFY_ENUM_DIR = 1022
S_OK = 0
# --- KnownFolders.h: FOLDERID_Downloads {374DE290-123F-4565-9164-39C4925E467B}
FOLDERID_Downloads_STR = "{374DE290-123F-4565-9164-39C4925E467B}"

ZONE_STREAM = "Zone.Identifier"
MAX_STREAM_BYTES = 64 * 1024  # Zone.Identifier は数百バイト。上限を超える分は読まない

EXISTS_ERRORS = frozenset({ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS})


@dataclass(frozen=True)
class FileInfo:
    """ディレクトリ直下の1項目(リパースポイントはたどらない)。"""

    name: str
    path: str
    size: int
    mtime: float
    ctime: float  # 作成時刻
    atime: float
    attrs: int
    is_dir: bool

    @property
    def is_reparse(self) -> bool:
        return bool(self.attrs & FILE_ATTRIBUTE_REPARSE_POINT)


@dataclass(frozen=True)
class VolumeInfo:
    root: str
    fs_name: str
    flags: int
    serial: int
    drive_type: int

    @property
    def named_streams(self) -> bool:
        return self.fs_name.upper() == "NTFS" and bool(self.flags & FILE_NAMED_STREAMS)

    @property
    def is_remote(self) -> bool:
        return self.drive_type == DRIVE_REMOTE


@dataclass(frozen=True)
class StreamRead:
    """Zone.Identifier の読み取り結果。present は「ストリームがある(読めなくても)」。"""

    present: bool
    data: bytes | None


class Win32Api(Protocol):
    """DropSort が触る Win32 の窓口。テストでは偽物(fakewin32.FakeWin32)に差し替える。"""

    def known_folder_downloads(self) -> str | None: ...
    def list_dir(self, path: str) -> list[FileInfo]: ...
    def stat(self, path: str) -> FileInfo | None: ...
    def exists(self, path: str) -> bool: ...
    def is_dir(self, path: str) -> bool: ...
    def try_exclusive_open(self, path: str) -> int: ...
    def read_zone_identifier(self, path: str) -> StreamRead: ...
    def volume_root(self, path: str) -> str | None: ...
    def volume_info(self, root: str) -> VolumeInfo | None: ...
    def final_path(self, path: str) -> str | None: ...
    def MoveFileExW(self, src: str, dst: str) -> int: ...
    def CopyFileExW(self, src: str, dst: str) -> int: ...
    def DeleteFileW(self, path: str) -> int: ...
    def create_directory(self, path: str) -> int: ...


class WatchApi(Protocol):
    """ReadDirectoryChangesW の窓口(watcher.py 用)。"""

    def open_dir(self, path: str) -> tuple[int | None, int]: ...
    def read_changes(self, handle: int, buf: ctypes.Array[ctypes.c_char]) -> tuple[bool, int, int]: ...
    def cancel(self, handle: int, native_thread_id: int | None) -> None: ...
    def close(self, handle: int) -> None: ...


# ------------------------------------------------------------------ 実物(ctypes)
class GUID(ctypes.Structure):
    _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD), ("Data4", ctypes.c_ubyte * 8)]


def _guid(s: str) -> GUID:
    h = s.strip("{}").replace("-", "")
    g = GUID()
    g.Data1 = int(h[0:8], 16)
    g.Data2 = int(h[8:12], 16)
    g.Data3 = int(h[12:16], 16)
    for i in range(8):
        g.Data4[i] = int(h[16 + i * 2: 18 + i * 2], 16)
    return g


_bound = False
kernel32: ctypes.WinDLL
shell32: ctypes.WinDLL
ole32: ctypes.WinDLL


def _bind() -> None:
    """ctypes の関数に argtypes / restype を設定する(64bit で HANDLE が切れないように)。"""
    global _bound, kernel32, shell32, ole32
    if _bound:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)

    shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), w.DWORD, w.HANDLE, ctypes.POINTER(w.LPWSTR)]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None

    kernel32.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    kernel32.CreateFileW.restype = w.HANDLE
    kernel32.CloseHandle.argtypes = [w.HANDLE]
    kernel32.CloseHandle.restype = w.BOOL
    kernel32.GetVolumePathNameW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD]
    kernel32.GetVolumePathNameW.restype = w.BOOL
    kernel32.GetVolumeInformationW.argtypes = [
        w.LPCWSTR, w.LPWSTR, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD),
        ctypes.POINTER(w.DWORD), w.LPWSTR, w.DWORD,
    ]
    kernel32.GetVolumeInformationW.restype = w.BOOL
    kernel32.GetDriveTypeW.argtypes = [w.LPCWSTR]
    kernel32.GetDriveTypeW.restype = w.UINT
    kernel32.GetFinalPathNameByHandleW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD]
    kernel32.GetFinalPathNameByHandleW.restype = w.DWORD
    kernel32.MoveFileExW.argtypes = [w.LPCWSTR, w.LPCWSTR, w.DWORD]
    kernel32.MoveFileExW.restype = w.BOOL
    kernel32.CopyFileExW.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.POINTER(w.BOOL), w.DWORD]
    kernel32.CopyFileExW.restype = w.BOOL
    kernel32.DeleteFileW.argtypes = [w.LPCWSTR]
    kernel32.DeleteFileW.restype = w.BOOL
    kernel32.CreateDirectoryW.argtypes = [w.LPCWSTR, ctypes.c_void_p]
    kernel32.CreateDirectoryW.restype = w.BOOL
    kernel32.ReadDirectoryChangesW.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD, w.BOOL, w.DWORD,
                                               ctypes.POINTER(w.DWORD), ctypes.c_void_p, ctypes.c_void_p]
    kernel32.ReadDirectoryChangesW.restype = w.BOOL
    kernel32.CancelIoEx.argtypes = [w.HANDLE, ctypes.c_void_p]
    kernel32.CancelIoEx.restype = w.BOOL
    kernel32.OpenThread.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel32.OpenThread.restype = w.HANDLE
    kernel32.CancelSynchronousIo.argtypes = [w.HANDLE]
    kernel32.CancelSynchronousIo.restype = w.BOOL
    _bound = True


def _valid(h: int | None) -> bool:
    return h is not None and h != 0 and h != INVALID_HANDLE_VALUE


def strip_long_prefix(p: str) -> str:
    if p.startswith("\\\\?\\UNC\\"):
        return "\\\\" + p[8:]
    if p.startswith("\\\\?\\"):
        return p[4:]
    return p


def _info_from_stat(name: str, path: str, st: os.stat_result, is_dir: bool) -> FileInfo:
    ctime = float(getattr(st, "st_birthtime", st.st_ctime))
    return FileInfo(name=name, path=path, size=int(st.st_size), mtime=float(st.st_mtime), ctime=ctime,
                    atime=float(st.st_atime), attrs=int(getattr(st, "st_file_attributes", 0)), is_dir=is_dir)


class RealWin32:
    """実機の Win32 実装。"""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("DropSort は Windows 専用です")
        _bind()

    def known_folder_downloads(self) -> str | None:
        p = w.LPWSTR()
        g = _guid(FOLDERID_Downloads_STR)
        hr = shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(p))
        try:
            if hr != S_OK or not p.value:
                return None
            return str(p.value)
        finally:
            if p:
                ole32.CoTaskMemFree(ctypes.cast(p, ctypes.c_void_p))

    def list_dir(self, path: str) -> list[FileInfo]:
        out: list[FileInfo] = []
        with os.scandir(path) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                    is_dir = e.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                out.append(_info_from_stat(e.name, e.path, st, is_dir))
        return out

    def stat(self, path: str) -> FileInfo | None:
        try:
            st = os.lstat(path)
        except OSError:
            return None
        is_dir = bool(int(getattr(st, "st_file_attributes", 0)) & FILE_ATTRIBUTE_DIRECTORY)
        return _info_from_stat(os.path.basename(path), path, st, is_dir)

    def exists(self, path: str) -> bool:
        return os.path.lexists(path)

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(path)

    def try_exclusive_open(self, path: str) -> int:
        """共有なし(dwShareMode=0)で開いて直ちに閉じる。ReadFile はしない(FR-4)。0 なら成功。"""
        h = kernel32.CreateFileW(path, GENERIC_READ, 0, None, OPEN_EXISTING,
                                 FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT, None)
        if not _valid(h):
            return ctypes.get_last_error() or ERROR_ACCESS_DENIED
        kernel32.CloseHandle(h)
        return ERROR_SUCCESS

    def read_zone_identifier(self, path: str) -> StreamRead:
        """Zone.Identifier ストリームだけを読む(本文は読まない)。ストリーム以外の失敗は「MOTW あり・中身不明」(安全側)。"""
        try:
            with open(path + ":" + ZONE_STREAM, "rb") as f:
                return StreamRead(True, f.read(MAX_STREAM_BYTES))
        except FileNotFoundError:
            return StreamRead(False, None)
        except OSError:
            return StreamRead(True, None)

    def volume_root(self, path: str) -> str | None:
        buf = ctypes.create_unicode_buffer(1024)
        if not kernel32.GetVolumePathNameW(path, buf, len(buf)):
            return None
        return buf.value

    def volume_info(self, root: str) -> VolumeInfo | None:
        name = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(261)
        serial = w.DWORD()
        maxlen = w.DWORD()
        flags = w.DWORD()
        dt = int(kernel32.GetDriveTypeW(root))
        if not kernel32.GetVolumeInformationW(root, name, len(name), ctypes.byref(serial), ctypes.byref(maxlen),
                                              ctypes.byref(flags), fs, len(fs)):
            # 未接続のネットワークドライブでも「ネットワーク」と分かるようにする(FR-11)
            return VolumeInfo(root=root, fs_name="", flags=0, serial=0, drive_type=dt) if dt == DRIVE_REMOTE else None
        return VolumeInfo(root=root, fs_name=fs.value, flags=int(flags.value), serial=int(serial.value), drive_type=dt)

    def final_path(self, path: str) -> str | None:
        """リンク・ジャンクションを解決した最終パス(INV-11)。フォルダ・ファイルどちらも可。"""
        h = kernel32.CreateFileW(path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, None,
                                 OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
        if not _valid(h):
            return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = kernel32.GetFinalPathNameByHandleW(h, buf, len(buf), VOLUME_NAME_DOS)
            if n == 0 or n >= len(buf):
                return None
            return strip_long_prefix(buf.value)
        finally:
            kernel32.CloseHandle(h)

    def MoveFileExW(self, src: str, dst: str) -> int:
        """同一ボリュームの改名。フラグは書き込み完了待ちだけ(上書き・別ボリューム複製は許さない)。"""
        if kernel32.MoveFileExW(src, dst, MOVEFILE_WRITE_THROUGH):
            return ERROR_SUCCESS
        return ctypes.get_last_error() or ERROR_ACCESS_DENIED

    def CopyFileExW(self, src: str, dst: str) -> int:
        """移動先に同名があれば失敗するコピー(代替データストリームも複製される)。"""
        cancel = w.BOOL(False)
        if kernel32.CopyFileExW(src, dst, None, None, ctypes.byref(cancel), COPY_FILE_FAIL_IF_EXISTS):
            return ERROR_SUCCESS
        return ctypes.get_last_error() or ERROR_ACCESS_DENIED

    def DeleteFileW(self, path: str) -> int:
        """§9.5 の検証済みコピーがある場合の元ファイル専用(呼び出し元は mover.py の1か所だけ)。"""
        if kernel32.DeleteFileW(path):
            return ERROR_SUCCESS
        return ctypes.get_last_error() or ERROR_ACCESS_DENIED

    def create_directory(self, path: str) -> int:
        if kernel32.CreateDirectoryW(path, None):
            return ERROR_SUCCESS
        return ctypes.get_last_error() or ERROR_ACCESS_DENIED


class RealWatchApi:
    """ReadDirectoryChangesW を同期呼び出しで使う(専用スレッドから)。"""

    FILTER = (FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_DIR_NAME | FILE_NOTIFY_CHANGE_ATTRIBUTES
              | FILE_NOTIFY_CHANGE_SIZE | FILE_NOTIFY_CHANGE_LAST_WRITE)

    def __init__(self) -> None:
        _bind()

    def open_dir(self, path: str) -> tuple[int | None, int]:
        h = kernel32.CreateFileW(path, FILE_LIST_DIRECTORY, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                                 None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
        if not _valid(h):
            return None, ctypes.get_last_error()
        return int(h), 0

    def read_changes(self, handle: int, buf: ctypes.Array[ctypes.c_char]) -> tuple[bool, int, int]:
        n = w.DWORD(0)
        ok = kernel32.ReadDirectoryChangesW(handle, buf, len(buf), False, self.FILTER, ctypes.byref(n), None, None)
        if not ok:
            return False, 0, ctypes.get_last_error()
        return True, int(n.value), 0

    def cancel(self, handle: int, native_thread_id: int | None) -> None:
        kernel32.CancelIoEx(handle, None)
        if native_thread_id:
            th = kernel32.OpenThread(THREAD_TERMINATE, False, native_thread_id)
            if _valid(th):
                try:
                    kernel32.CancelSynchronousIo(th)
                finally:
                    kernel32.CloseHandle(th)

    def close(self, handle: int) -> None:
        kernel32.CloseHandle(handle)
