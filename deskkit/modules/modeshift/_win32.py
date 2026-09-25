# ModeShift が使う Win32(ctypes): プロセス一覧(Toolhelp。exe 名と PID のみ。INV-12)、可視トップレベル
# ウィンドウへの WM_CLOSE、終了待ち、FR-14 の確認済み強制終了(確認前に開いたハンドル経由の1関数だけ)。argtypes/restype は必ず設定する。
# host 内部の Win32 定義は import しない(自前の WinDLL インスタンスを使う)。
from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes as w
from pathlib import PureWindowsPath

from deskkit.modules.modeshift.system import CloseRequest, ProcInfo

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_u32 = ctypes.WinDLL("user32", use_last_error=True)

# --- winnt.h / processthreadsapi.h / tlhelp32.h / winuser.h の値
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE_RIGHT = 0x0001   # PROCESS_TERMINATE
SYNCHRONIZE = 0x00100000
STILL_ACTIVE = 259
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WM_CLOSE = 0x0010
GW_OWNER = 4
ERROR_ACCESS_DENIED = 5
MAX_PATH = 260


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
        ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD),
        ("szExeFile", w.WCHAR * MAX_PATH),
    ]


class SYSTEM_POWER_STATUS(ctypes.Structure):  # noqa: N801 - winbase.h の名前のまま
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte), ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte), ("BatteryLifeTime", w.DWORD), ("BatteryFullLifeTime", w.DWORD),
    ]


WNDENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

_k32.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
_k32.CreateToolhelp32Snapshot.restype = w.HANDLE
_k32.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_k32.Process32FirstW.restype = w.BOOL
_k32.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_k32.Process32NextW.restype = w.BOOL
_k32.CloseHandle.argtypes = [w.HANDLE]
_k32.CloseHandle.restype = w.BOOL
_k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
_k32.OpenProcess.restype = w.HANDLE
_k32.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
_k32.QueryFullProcessImageNameW.restype = w.BOOL
_k32.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
_k32.GetExitCodeProcess.restype = w.BOOL
_k32.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
_k32.WaitForSingleObject.restype = w.DWORD
_k32.GetSystemPowerStatus.argtypes = [ctypes.POINTER(SYSTEM_POWER_STATUS)]
_k32.GetSystemPowerStatus.restype = w.BOOL

_u32.EnumWindows.argtypes = [WNDENUMPROC, w.LPARAM]
_u32.EnumWindows.restype = w.BOOL
_u32.IsWindowVisible.argtypes = [w.HWND]
_u32.IsWindowVisible.restype = w.BOOL
_u32.GetWindow.argtypes = [w.HWND, w.UINT]
_u32.GetWindow.restype = w.HWND
_u32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
_u32.GetWindowThreadProcessId.restype = w.DWORD
_u32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
_u32.PostMessageW.restype = w.BOOL


def list_processes() -> list[ProcInfo]:
    """プロセス一覧(PID と exe 名だけ)。メモリ読み取り・モジュール列挙はしない(INV-12)。"""
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return []
    out: list[ProcInfo] = []
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = _k32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            out.append(ProcInfo(int(pe.th32ProcessID), pe.szExeFile.lower()))
            ok = _k32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        _k32.CloseHandle(snap)
    return out


def ac_line_status() -> bool | None:
    """AC 電源につながっていれば True、バッテリー駆動なら False、不明(255)・読めなければ None。"""
    st = SYSTEM_POWER_STATUS()
    if not _k32.GetSystemPowerStatus(ctypes.byref(st)):
        return None
    if st.ACLineStatus == 1:
        return True
    if st.ACLineStatus == 0:
        return False
    return None


def exe_path(pid: int) -> str | None:
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        n = w.DWORD(len(buf))
        if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return buf.value
        return None
    finally:
        _k32.CloseHandle(h)


def is_alive(pid: int) -> bool:
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        # 開けない(昇格プロセス等)ときは一覧に居るかで判断する
        return any(p.pid == pid for p in list_processes())
    try:
        code = w.DWORD()
        if _k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return True
    finally:
        _k32.CloseHandle(h)


def visible_top_windows(pids: set[int]) -> list[int]:
    """対象 PID の、可視・オーナーなしのトップレベルウィンドウ(保存確認などの子ダイアログには触らない)。"""
    found: list[int] = []

    def cb(hwnd: int, _lp: int) -> bool:
        try:
            if not _u32.IsWindowVisible(hwnd) or _u32.GetWindow(hwnd, GW_OWNER):
                return True
            pid = w.DWORD()
            _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if int(pid.value) in pids:
                found.append(int(hwnd))
        except Exception:  # noqa: BLE001 - コールバックから例外を漏らさない
            pass
        return True

    proc = WNDENUMPROC(cb)
    _u32.EnumWindows(proc, 0)
    return found


def post_close(pids: set[int]) -> CloseRequest:
    posted = denied = 0
    last = 0
    for hwnd in visible_top_windows(pids):
        if _u32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
            posted += 1
        else:
            denied += 1
            last = ctypes.get_last_error()
    return CloseRequest(posted, denied, last)


def wait_exit(pids: set[int], timeout_s: float, abort: threading.Event) -> set[int]:
    """timeout_s まで終了を待ち、まだ動いている PID を返す。abort が立ったら待つのをやめる。"""
    deadline = time.monotonic() + max(0.0, timeout_s)
    handles: dict[int, int] = {}
    for pid in pids:
        h = _k32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if h:
            handles[pid] = h
    try:
        remaining = set(pids)
        while remaining:
            for pid in list(remaining):
                h = handles.get(pid)
                if h is not None:
                    if _k32.WaitForSingleObject(h, 0) == WAIT_OBJECT_0:
                        remaining.discard(pid)
                elif not any(p.pid == pid for p in list_processes()):
                    remaining.discard(pid)
            if not remaining or time.monotonic() >= deadline or abort.is_set():
                break
            abort.wait(0.1)
        return remaining
    finally:
        for h in handles.values():
            _k32.CloseHandle(h)


class ForceHandle:
    """FR-14 の強制終了の候補(ForceTarget の実物)。確認ダイアログを出す前に
    PROCESS_TERMINATE | SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION で開き、承認後はこの同じハンドルで終了させる。
    ハンドルを持っている間はプロセスオブジェクトが残るため、待っている間に対象が終了しても PID は再利用されない。"""

    def __init__(self, pid: int, handle: int) -> None:
        self.pid = pid
        self._h: int | None = handle

    def alive(self) -> bool:
        if not self._h:
            return False
        return bool(_k32.WaitForSingleObject(self._h, 0) == WAIT_TIMEOUT)

    def terminate_confirmed(self) -> tuple[bool, str]:
        """確認ダイアログで利用者が承認した後にだけ呼ぶ強制終了。この関数以外から強制終了しない(INV-1)。"""
        if not self._h:
            return False, "ハンドルが閉じられている"
        if not self.alive():
            return False, "既に終了していた"
        fn = _k32.TerminateProcess
        fn.argtypes = [w.HANDLE, w.UINT]
        fn.restype = w.BOOL
        if fn(self._h, 1):
            return True, "確認のうえ強制終了しました"
        return False, f"強制終了に失敗(Win32 エラー {ctypes.get_last_error()})"

    def close(self) -> None:
        h, self._h = self._h, None
        if h:
            _k32.CloseHandle(h)


def open_for_force(pid: int, exe: str) -> ForceHandle | None:
    """強制終了の候補としてハンドルを開く。開けない(昇格プロセス等)・既に別の exe になっている → None。"""
    h = _k32.OpenProcess(PROCESS_TERMINATE_RIGHT | SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    fh = ForceHandle(pid, h)
    try:
        buf = ctypes.create_unicode_buffer(32768)
        n = w.DWORD(len(buf))
        ok = _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
        if not ok or exe_basename(buf.value) != exe.lower() or not fh.alive():
            fh.close()
            return None
    except BaseException:
        fh.close()
        raise
    return fh


class Win32Processes:
    """ProcessApi の実物。"""

    def list_processes(self) -> list[ProcInfo]:
        return list_processes()

    def exe_path(self, pid: int) -> str | None:
        return exe_path(pid)

    def own_pid(self) -> int:
        return os.getpid()

    def is_alive(self, pid: int) -> bool:
        return is_alive(pid)

    def post_close(self, pids: set[int]) -> CloseRequest:
        return post_close(pids)

    def wait_exit(self, pids: set[int], timeout_s: float, abort: threading.Event) -> set[int]:
        return wait_exit(pids, timeout_s, abort)

    def open_for_force(self, pid: int, exe: str) -> ForceHandle | None:
        return open_for_force(pid, exe)


def exe_basename(path: str) -> str:
    return PureWindowsPath(path).name.lower()
