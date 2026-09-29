# EyeBreak の Win32 の窓口。入力に関わる API は GetLastInputInfo と GetTickCount の2つだけ(E-1・INV-1)。
# どのキーが押されたか・どこを押したかは分からない(最後の入力の時刻1つだけ)。Protocol の背後に置き、テストで偽物に替える。
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
from typing import Any, Protocol

MASK32 = 0xFFFFFFFF
HALF32 = 0x80000000


class Api(Protocol):
    def last_input_tick(self) -> int | None:
        """最後の入力の tick count(DWORD)。GetLastInputInfo が失敗したら None。"""
        ...

    def tick_count(self) -> int:
        """起動からのミリ秒(DWORD。49.7 日で 0 に戻る)。"""
        ...


def idle_ms(last_input: int, now_tick: int) -> int:
    """入力なしのミリ秒。32 ビットの符号なしで引き、2^31 以上(dwTime の方が新しい)なら 0(E-1)。"""
    d = (int(now_tick) - int(last_input)) & MASK32
    return 0 if d >= HALF32 else d


def read_idle_seconds(api: Api) -> float | None:
    last = api.last_input_tick()
    if last is None:
        return None
    return idle_ms(last, api.tick_count()) / 1000.0


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", w.UINT), ("dwTime", w.DWORD)]


_fns: dict[str, Any] = {}


def _load() -> dict[str, Any]:
    if not _fns:
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
        u32.GetLastInputInfo.restype = w.BOOL
        k32.GetTickCount.argtypes = []
        k32.GetTickCount.restype = w.DWORD
        _fns.update(last=u32.GetLastInputInfo, tick=k32.GetTickCount)
    return _fns


class RealApi:
    def __init__(self) -> None:
        self._info = _LASTINPUTINFO()
        self._info.cbSize = ctypes.sizeof(_LASTINPUTINFO)

    def last_input_tick(self) -> int | None:
        if not _load()["last"](ctypes.byref(self._info)):
            return None
        return int(self._info.dwTime) & MASK32

    def tick_count(self) -> int:
        return int(_load()["tick"]()) & MASK32


class FakeApi:
    """テストと自己検査用: 入力なしの秒数を直接決める。"""

    def __init__(self) -> None:
        self.tick = 1_000_000
        self.last: int | None = self.tick
        self.fail = False

    def last_input_tick(self) -> int | None:
        return None if self.fail else self.last

    def tick_count(self) -> int:
        return self.tick & MASK32

    def set_idle(self, seconds: float) -> None:
        self.last = (self.tick - int(seconds * 1000)) & MASK32

    def advance(self, seconds: float, *, active: bool) -> None:
        self.tick = (self.tick + int(seconds * 1000)) & MASK32
        if active:
            self.last = self.tick
