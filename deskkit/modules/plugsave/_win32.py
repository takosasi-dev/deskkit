# PlugSave の Win32 の窓口(ctypes)。GetLogicalDrives・GetDriveTypeW・GetVolumeInformationW・SetThreadErrorMode と空き容量。
# DriveApi の Protocol の背後に置き、テストと自己検査では fakes.FakeDriveApi に替える(本物のドライブに触れない)。
# 読み取りだけの API しか宣言しない。ドライブの「根」はここで作る(本物は "E:\"、偽物は一時フォルダ)。
from __future__ import annotations

import ctypes
import shutil
from ctypes import wintypes as w
from dataclasses import dataclass
from typing import Protocol

# --- WinBase.h: GetDriveTypeW の戻り値
DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6
BACKUP_DRIVE_TYPES = (DRIVE_REMOVABLE, DRIVE_FIXED)
SEM_FAILCRITICALERRORS = 0x0001
_BUF = 261  # MAX_PATH + 1


@dataclass(frozen=True)
class VolumeInfo:
    label: str
    serial: int
    fs: str          # "NTFS" / "exFAT" / "FAT32" / "FAT" など(GetVolumeInformationW のまま)


class DriveApi(Protocol):
    def logical_drives(self) -> int:
        """GetLogicalDrives のビット(ビット0 が A:)。"""
        ...

    def root(self, letter: str) -> str:
        """ドライブの根のパス(末尾に区切りあり)。本物は "E:\\"。"""
        ...

    def drive_type(self, root: str) -> int: ...

    def volume_info(self, root: str) -> VolumeInfo | None:
        """失敗は None(媒体が無い・鍵が掛かっている・ネットワークにつながらないなど)。"""
        ...

    def set_thread_error_mode(self, mode: int) -> int:
        """呼んだスレッドのエラーモードを変え、前の値を返す(失敗は -1)。"""
        ...

    def disk_usage(self, root: str) -> tuple[int, int]:
        """(全体, 空き) バイト。読めなければ OSError。"""
        ...


def letters_of(mask: int) -> list[str]:
    return [chr(ord("A") + i) for i in range(26) if mask & (1 << i)]


class RealDriveApi:
    """本物の Win32。読み取りだけ。"""

    def __init__(self) -> None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32 = k32
        k32.GetLogicalDrives.argtypes = []
        k32.GetLogicalDrives.restype = w.DWORD
        k32.GetDriveTypeW.argtypes = [w.LPCWSTR]
        k32.GetDriveTypeW.restype = w.UINT
        k32.GetVolumeInformationW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD, ctypes.POINTER(w.DWORD),
                                              ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD), w.LPWSTR, w.DWORD]
        k32.GetVolumeInformationW.restype = w.BOOL
        k32.SetThreadErrorMode.argtypes = [w.DWORD, ctypes.POINTER(w.DWORD)]
        k32.SetThreadErrorMode.restype = w.BOOL

    def logical_drives(self) -> int:
        return int(self._k32.GetLogicalDrives())

    def root(self, letter: str) -> str:
        return f"{letter.upper()}:\\"

    def drive_type(self, root: str) -> int:
        return int(self._k32.GetDriveTypeW(root))

    def volume_info(self, root: str) -> VolumeInfo | None:
        label = ctypes.create_unicode_buffer(_BUF)
        fs = ctypes.create_unicode_buffer(_BUF)
        serial = w.DWORD()
        maxc = w.DWORD()
        flags = w.DWORD()
        ok = self._k32.GetVolumeInformationW(root, label, _BUF, ctypes.byref(serial), ctypes.byref(maxc),
                                             ctypes.byref(flags), fs, _BUF)
        if not ok:
            return None
        return VolumeInfo(label.value, int(serial.value), fs.value)

    def set_thread_error_mode(self, mode: int) -> int:
        old = w.DWORD()
        if not self._k32.SetThreadErrorMode(mode, ctypes.byref(old)):
            return -1
        return int(old.value)

    def disk_usage(self, root: str) -> tuple[int, int]:
        u = shutil.disk_usage(root)
        return int(u.total), int(u.free)
