# テストと自己検査のための偽の Win32(DriveApi)。ドライブの「根」は一時フォルダで、種類・ボリューム情報・空き容量を決めて返す。
# 呼ばれた回数を数える(AC-9: WM_DEVICECHANGE のハンドラの中で呼ばれないことを確かめる)。本物のドライブには触れない。
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from deskkit.modules.plugsave._win32 import DRIVE_FIXED, VolumeInfo


@dataclass
class FakeDrive:
    root: str
    drive_type: int = DRIVE_FIXED
    volume: VolumeInfo | None = None
    total: int = 64 * 1024**3
    free: int | Callable[[], int] = 32 * 1024**3


class FakeDriveApi:
    def __init__(self) -> None:
        self.drives: dict[str, FakeDrive] = {}
        self.calls: list[str] = []
        self.error_mode = 0
        self.volume_fail_left: dict[str, int] = {}   # 文字 → あと何回 volume_info を失敗させるか

    def add(self, letter: str, root: str, *, drive_type: int = DRIVE_FIXED, label: str = "BACKUP", serial: int = 0x1234ABCD,
            fs: str = "NTFS", total: int = 64 * 1024**3, free: int | Callable[[], int] = 32 * 1024**3,
            volume: bool = True) -> FakeDrive:
        d = FakeDrive(root if root.endswith(os.sep) else root + os.sep, drive_type,
                      VolumeInfo(label, serial, fs) if volume else None, total, free)
        self.drives[letter.upper()] = d
        return d

    def remove(self, letter: str) -> None:
        self.drives.pop(letter.upper(), None)

    def _by_root(self, root: str) -> FakeDrive | None:
        n = os.path.normcase(root.rstrip("\\/"))
        for d in self.drives.values():
            if os.path.normcase(d.root.rstrip("\\/")) == n:
                return d
        return None

    def logical_drives(self) -> int:
        self.calls.append("logical_drives")
        mask = 0
        for letter in self.drives:
            mask |= 1 << (ord(letter) - ord("A"))
        return mask

    def root(self, letter: str) -> str:
        d = self.drives.get(letter.upper())
        return d.root if d is not None else f"{letter.upper()}:\\"

    def drive_type(self, root: str) -> int:
        self.calls.append("drive_type")
        d = self._by_root(root)
        return d.drive_type if d is not None else DRIVE_FIXED

    def volume_info(self, root: str) -> VolumeInfo | None:
        self.calls.append("volume_info")
        d = self._by_root(root)
        if d is None:
            return None
        for letter, dd in self.drives.items():
            if dd is d and self.volume_fail_left.get(letter, 0) > 0:
                self.volume_fail_left[letter] -= 1
                return None
        return d.volume

    def set_thread_error_mode(self, mode: int) -> int:
        self.calls.append("set_thread_error_mode")
        old = self.error_mode
        self.error_mode = mode
        return old

    def disk_usage(self, root: str) -> tuple[int, int]:
        self.calls.append("disk_usage")
        d = self._by_root(root)
        if d is None:
            for dd in self.drives.values():
                if os.path.normcase(root).startswith(os.path.normcase(dd.root)):
                    d = dd
                    break
        if d is None:
            return 1 << 40, 1 << 40
        free = d.free() if callable(d.free) else d.free
        return d.total, int(free)
