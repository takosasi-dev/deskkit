# RegisterHotKey / UnregisterHotKey を名前付きで管理し、WM_HOTKEY を triggered(name) に変換する。
# 競合時は HotkeyConflictError。別キーへの自動振り替えはしない(INV-4)。押下内容はログに出さない。
from __future__ import annotations

import ctypes
import logging
from collections.abc import Mapping
from ctypes import wintypes as w
from dataclasses import dataclass
from typing import Protocol

from PySide6.QtCore import QCoreApplication, QObject, Signal

from overlaykit.errors import HotkeyConflictError, HotkeyError

log = logging.getLogger("overlaykit")
ERROR_HOTKEY_ALREADY_REGISTERED = 1409  # winerror.h
MOD_NOREPEAT = 0x4000  # winuser.h


class Win32Api(Protocol):
    def register_hotkey(self, hwnd: int, hid: int, mods: int, vk: int) -> bool: ...
    def unregister_hotkey(self, hwnd: int, hid: int) -> bool: ...
    def get_last_error(self) -> int: ...


class _RealApi:
    def __init__(self) -> None:
        self._u = ctypes.WinDLL("user32", use_last_error=True)
        self._u.RegisterHotKey.argtypes = [w.HWND, ctypes.c_int, w.UINT, w.UINT]
        self._u.RegisterHotKey.restype = w.BOOL
        self._u.UnregisterHotKey.argtypes = [w.HWND, ctypes.c_int]
        self._u.UnregisterHotKey.restype = w.BOOL

    def register_hotkey(self, hwnd: int, hid: int, mods: int, vk: int) -> bool:
        return bool(self._u.RegisterHotKey(hwnd, hid, mods, vk))

    def unregister_hotkey(self, hwnd: int, hid: int) -> bool:
        return bool(self._u.UnregisterHotKey(hwnd, hid))

    def get_last_error(self) -> int:
        return ctypes.get_last_error()


@dataclass(frozen=True)
class RegisterResult:
    ok: tuple[str, ...]
    failed: tuple[tuple[str, Exception], ...]


class HotkeyRegistry(QObject):
    triggered = Signal(str)

    def __init__(self, hwnd: int, api: Win32Api | None = None) -> None:
        super().__init__()
        self._hwnd = hwnd
        self._api: Win32Api = api or _RealApi()
        self._by_name: dict[str, tuple[int, int, int]] = {}
        self._by_id: dict[int, str] = {}
        self._next_id = 1
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.unregister_all)

    def register(self, name: str, modifiers: int, vk: int) -> int:
        if name in self._by_name:
            raise HotkeyError(f"'{name}' は登録済みです。先に unregister してください")
        hid = self._next_id
        self._next_id += 1
        if not self._api.register_hotkey(self._hwnd, hid, modifiers | MOD_NOREPEAT, vk):
            code = self._api.get_last_error()
            log.info("hotkey register failed name=%s code=%s", name, code)
            if code == ERROR_HOTKEY_ALREADY_REGISTERED:
                raise HotkeyConflictError(f"'{name}' は他のアプリと競合しています", code)
            raise HotkeyError(f"'{name}' を登録できません(エラー {code})", code)
        self._by_name[name] = (hid, modifiers, vk)
        self._by_id[hid] = name
        log.info("hotkey registered name=%s", name)
        return hid

    def register_all(self, table: Mapping[str, tuple[int, int]]) -> RegisterResult:
        ok: list[str] = []
        failed: list[tuple[str, Exception]] = []
        for name, (mods, vk) in table.items():
            try:
                self.register(name, mods, vk)
                ok.append(name)
            except HotkeyError as e:
                failed.append((name, e))
        return RegisterResult(tuple(ok), tuple(failed))

    def unregister(self, name: str) -> None:
        entry = self._by_name.pop(name, None)
        if entry is None:
            return
        self._by_id.pop(entry[0], None)
        self._api.unregister_hotkey(self._hwnd, entry[0])

    def unregister_all(self) -> None:
        for name in list(self._by_name):
            self.unregister(name)

    def names(self) -> list[str]:
        return list(self._by_name)

    def handle_wm_hotkey(self, hid: int) -> None:
        """WM_HOTKEY の wParam を受けて triggered を発火する(host の隠しウィンドウから呼ぶ)。"""
        name = self._by_id.get(hid)
        if name is not None:
            self.triggered.emit(name)
