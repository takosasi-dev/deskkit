# KeyFree が使う Win32 は MapVirtualKeyW だけ(記号キーの今の配列の文字。K-10)。Protocol の背後に置き、テストでは偽物に替える。
# ホットキーの登録・解除はここでは呼ばない(INV-1・V4INV-2)。試すのも預かるのも ctx.hotkeys が行う。
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
from typing import Protocol

MAPVK_VK_TO_CHAR = 2


class KeyboardApi(Protocol):
    def vk_to_char(self, vk: int) -> int:
        """MapVirtualKeyW(vk, MAPVK_VK_TO_CHAR)。シフトなしの文字のコード(無ければ 0。デッドキーは最上位ビットが立つ)。"""
        ...


class RealKeyboard:
    def __init__(self) -> None:
        self._u = ctypes.WinDLL("user32", use_last_error=True)
        self._u.MapVirtualKeyW.argtypes = [w.UINT, w.UINT]
        self._u.MapVirtualKeyW.restype = w.UINT

    def vk_to_char(self, vk: int) -> int:
        return int(self._u.MapVirtualKeyW(vk, MAPVK_VK_TO_CHAR))
