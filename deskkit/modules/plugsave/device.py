# WM_DEVICECHANGE の lParam(DEV_BROADCAST_HDR へのポインタ)から、挿された・外れたドライブの文字の集合を作る(B-3・FR-7)。
# ハンドラの中で同期的に呼ぶ。読むのは構造体のメモリだけで、ファイルの I/O・Win32 の呼び出し・待ちをしない(INV-6)。
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
from dataclasses import dataclass

WM_DEVICECHANGE = 0x0219
DBT_DEVICEARRIVAL = 0x8000
DBT_DEVICEREMOVECOMPLETE = 0x8004
DBT_DEVNODES_CHANGED = 0x0007
DBT_DEVTYP_VOLUME = 0x00000002
DBTF_MEDIA = 0x0001
DBTF_NET = 0x0002


class DEV_BROADCAST_HDR(ctypes.Structure):  # noqa: N801 - Dbt.h の名前のまま
    _fields_ = [("dbch_size", w.DWORD), ("dbch_devicetype", w.DWORD), ("dbch_reserved", w.DWORD)]


class DEV_BROADCAST_VOLUME(ctypes.Structure):  # noqa: N801 - Dbt.h の名前のまま
    _fields_ = [("dbcv_size", w.DWORD), ("dbcv_devicetype", w.DWORD), ("dbcv_reserved", w.DWORD),
                ("dbcv_unitmask", w.DWORD), ("dbcv_flags", w.WORD)]


@dataclass(frozen=True)
class DeviceEvent:
    arrived: bool                 # True = DBT_DEVICEARRIVAL / False = DBT_DEVICEREMOVECOMPLETE
    letters: frozenset[str]       # "E" など(大文字1文字)
    flags: int                    # dbcv_flags(記録用。DBTF_MEDIA など)


def letters_from_mask(mask: int) -> frozenset[str]:
    return frozenset(chr(ord("A") + i) for i in range(26) if mask & (1 << i))


def parse(wparam: int, lparam: int) -> DeviceEvent | None:
    """FR-7: wParam が 0x8000/0x8004・lParam が 0 でない・devicetype が 2・DBTF_NET が無い、のときだけ文字の集合を返す。"""
    if wparam not in (DBT_DEVICEARRIVAL, DBT_DEVICEREMOVECOMPLETE) or not lparam:
        return None
    hdr = DEV_BROADCAST_HDR.from_address(lparam)
    if int(hdr.dbch_devicetype) != DBT_DEVTYP_VOLUME:
        return None
    if int(hdr.dbch_size) < ctypes.sizeof(DEV_BROADCAST_VOLUME) - 2:  # 末尾の詰め物を除いた 18 バイトは要る
        return None
    vol = DEV_BROADCAST_VOLUME.from_address(lparam)
    flags = int(vol.dbcv_flags)
    if flags & DBTF_NET:
        return None
    letters = letters_from_mask(int(vol.dbcv_unitmask))
    if not letters:
        return None
    return DeviceEvent(wparam == DBT_DEVICEARRIVAL, letters, flags)
