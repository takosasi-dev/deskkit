# モジュールが使ってよい共通部品: 利用者のファイルを「ごみ箱へ送る」唯一の入口(C-8 / v0.3 共通 V-6・H-2〜H-4)。
# ごみ箱の無いドライブは送らずに返し、ごみ箱に入らない場合は Windows に確認を出させる(FOF_WANTNUKEWARNING)。
# パスはログに書かない。呼び出しは時間がかかるので GUI スレッドの外から呼ぶ。
from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Sequence
from ctypes import wintypes as w
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

SkipReason = Literal["skipped_no_recycle_bin", "not_found", "in_use", "aborted", "failed"]

# --- fileapi.h: GetDriveTypeW の戻り値
DRIVE_FIXED = 3
# --- shellapi.h: SHFileOperationW
FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400
FOF_WANTNUKEWARNING = 0x4000
RECYCLE_FLAGS = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_WANTNUKEWARNING | FOF_SILENT | FOF_NOERRORUI
# --- winerror.h(SHFileOperationW が返す値のうち、理由を分けるもの)
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33
DE_ERROR_SHARING = 0x20  # shellapi の旧来の値(ERROR_SHARING_VIOLATION と同じ数値)


@dataclass
class RecycleResult:
    sent: list[Path] = field(default_factory=list)
    skipped: list[tuple[Path, SkipReason]] = field(default_factory=list)

    @property
    def sent_count(self) -> int:
        return len(self.sent)

    def count(self, reason: SkipReason) -> int:
        return sum(1 for _, r in self.skipped if r == reason)


class RecycleApi(Protocol):
    def drive_type(self, path: Path) -> int: ...
    def delete_to_recycle_bin(self, path: Path, parent_hwnd: int | None) -> tuple[int, bool]: ...  # (戻り値, 中断されたか)
    def exists(self, path: Path) -> bool: ...


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", w.HWND),
        ("wFunc", w.UINT),
        ("pFrom", w.LPCWSTR),
        ("pTo", w.LPCWSTR),
        ("fFlags", w.WORD),
        ("fAnyOperationsAborted", w.BOOL),
        ("hNameMappings", w.LPVOID),
        ("lpszProgressTitle", w.LPCWSTR),
    ]


class Win32RecycleApi:
    def __init__(self) -> None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        sh = ctypes.WinDLL("shell32", use_last_error=True)
        self._get_volume_path = k32.GetVolumePathNameW
        self._get_volume_path.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD]
        self._get_volume_path.restype = w.BOOL
        self._get_drive_type = k32.GetDriveTypeW
        self._get_drive_type.argtypes = [w.LPCWSTR]
        self._get_drive_type.restype = w.UINT
        self._shfileop = sh.SHFileOperationW
        self._shfileop.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
        self._shfileop.restype = ctypes.c_int

    def drive_type(self, path: Path) -> int:
        buf = ctypes.create_unicode_buffer(1024)
        if not self._get_volume_path(str(path), buf, len(buf)):
            return 0  # DRIVE_UNKNOWN
        return int(self._get_drive_type(buf.value))

    def delete_to_recycle_bin(self, path: Path, parent_hwnd: int | None) -> tuple[int, bool]:
        # pFrom は NUL 区切り・二重 NUL 終端。ctypes の文字列に末尾の NUL を1つ足して二重にする
        op = _SHFILEOPSTRUCTW()
        op.hwnd = parent_hwnd or None
        op.wFunc = FO_DELETE
        op.pFrom = str(path) + "\0"
        op.pTo = None
        op.fFlags = RECYCLE_FLAGS
        rc = int(self._shfileop(ctypes.byref(op)))
        return rc, bool(op.fAnyOperationsAborted)

    def exists(self, path: Path) -> bool:
        return os.path.lexists(path)


def _default_api() -> RecycleApi:
    if sys.platform != "win32":  # pragma: no cover - Windows 専用
        raise OSError("ごみ箱への移動は Windows でだけ使えます")
    return Win32RecycleApi()


def recycle(paths: Sequence[Path], parent_hwnd: int | None = None, api: RecycleApi | None = None) -> RecycleResult:
    """paths を1件ずつごみ箱へ送る。ごみ箱の無いドライブ・消えたファイルは送らずに理由を返す。

    利用者が恒久削除の確認で「いいえ」を選んだら、そのファイルと残り全部を aborted にして止める。
    """
    api = api or _default_api()
    result = RecycleResult()
    items = list(paths)
    for i, raw in enumerate(items):
        p = Path(raw)
        s = str(p)
        if not p.is_absolute() or "*" in s or "?" in s:  # pFrom はワイルドカードを展開するため
            result.skipped.append((p, "failed"))
            continue
        if not api.exists(p):
            result.skipped.append((p, "not_found"))
            continue
        if api.drive_type(p) != DRIVE_FIXED:
            result.skipped.append((p, "skipped_no_recycle_bin"))
            continue
        rc, aborted = api.delete_to_recycle_bin(p, parent_hwnd)
        if aborted:
            result.skipped.append((p, "aborted"))
            result.skipped.extend((Path(q), "aborted") for q in items[i + 1:])
            break
        if rc == 0 and not api.exists(p):
            result.sent.append(p)
        elif rc in (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND):
            result.skipped.append((p, "not_found"))
        elif rc in (ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION):
            result.skipped.append((p, "in_use"))
        else:
            result.skipped.append((p, "failed"))
    return result
