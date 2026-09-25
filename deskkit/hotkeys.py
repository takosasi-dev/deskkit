# host が OverlayKit の HotkeyRegistry を1つ保持し、モジュールへ "<module>.<name>" の名前空間で貸す(D-5)。
# "Ctrl+Shift+Space" 形式の表記と (modifiers, vk) の相互変換もここに置く。
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from overlaykit import HotkeyConflictError, HotkeyError, HotkeyRegistry

if TYPE_CHECKING:
    from deskkit.context import ModuleContextImpl

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x1, 0x2, 0x4, 0x8

_MOD_NAMES = [(MOD_WIN, "Win"), (MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift")]
_MOD_ALIASES = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT,
                "win": MOD_WIN, "windows": MOD_WIN, "meta": MOD_WIN}

# 仮想キーコード(winuser.h VK_*)
VK_NAMES: dict[int, str] = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x13: "Pause", 0x1B: "Esc", 0x20: "Space",
    0x21: "PageUp", 0x22: "PageDown", 0x23: "End", 0x24: "Home", 0x25: "Left", 0x26: "Up",
    0x27: "Right", 0x28: "Down", 0x2C: "PrintScreen", 0x2D: "Insert", 0x2E: "Delete",
    0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/", 0xC0: "`",
    0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
}
for _i in range(26):
    VK_NAMES[0x41 + _i] = chr(ord("A") + _i)
for _i in range(10):
    VK_NAMES[0x30 + _i] = str(_i)
    VK_NAMES[0x60 + _i] = f"Num{_i}"
for _i in range(24):
    VK_NAMES[0x70 + _i] = f"F{_i + 1}"
_NAME_TO_VK = {v.lower(): k for k, v in VK_NAMES.items()}
_NAME_TO_VK.update({"escape": 0x1B, "return": 0x0D, "del": 0x2E, "ins": 0x2D, "pgup": 0x21, "pgdn": 0x22})


def parse_hotkey(text: str) -> tuple[int, int]:
    """'Ctrl+Shift+Space' → (modifiers, vk)。解釈できなければ ValueError。"""
    parts = [p.strip() for p in text.replace(" + ", "+").split("+") if p.strip()]
    if not parts:
        raise ValueError("ホットキーが空です")
    mods = 0
    for p in parts[:-1]:
        m = _MOD_ALIASES.get(p.lower())
        if m is None:
            raise ValueError(f"修飾キー '{p}' を解釈できません")
        mods |= m
    key = parts[-1]
    vk = _NAME_TO_VK.get(key.lower())
    if vk is None:
        raise ValueError(f"キー '{key}' を解釈できません")
    if mods == 0:
        raise ValueError("修飾キー(Ctrl / Alt / Shift / Win)を1つ以上含めてください")
    return mods, vk


def format_hotkey(mods: int, vk: int) -> str:
    names = [n for m, n in _MOD_NAMES if mods & m]
    names.append(VK_NAMES.get(vk, f"VK{vk:02X}"))
    return "+".join(names)


class HotkeyHub:
    """host 全体で1つ。WM_HOTKEY → registry → 名前ごとのコールバック。"""

    def __init__(self, hwnd: int) -> None:
        self.registry = HotkeyRegistry(hwnd)
        self._callbacks: dict[str, list[Callable[[], None]]] = {}
        self.registry.triggered.connect(self._on_triggered)
        self.conflicts: list[tuple[str, str]] = []  # (完全名, 表記)

    def _on_triggered(self, full_name: str) -> None:
        for cb in list(self._callbacks.get(full_name, [])):
            cb()  # コールバックは ModuleContext.safe で包まれている

    def add_callback(self, full_name: str, cb: Callable[[], None]) -> None:
        self._callbacks.setdefault(full_name, []).append(cb)

    def drop(self, full_name: str) -> None:
        self.registry.unregister(full_name)
        self._callbacks.pop(full_name, None)

    def unregister_all(self) -> None:
        self.registry.unregister_all()
        self._callbacks.clear()
        self.conflicts.clear()


class _TriggeredProxy:
    def __init__(self, owner: ModuleHotkeys, name: str) -> None:
        self._owner = owner
        self._name = name

    def connect(self, cb: Callable[[], None]) -> None:
        self._owner._connect(self._name, cb)


class ModuleHotkeys:
    """モジュールに貸すホットキー窓口。register(name, modifiers, vk) / unregister / triggered(name)。"""

    def __init__(self, hub: HotkeyHub, ctx: ModuleContextImpl) -> None:
        self._hub = hub
        self._ctx = ctx
        self._names: set[str] = set()

    def _full(self, name: str) -> str:
        return f"{self._ctx.name}.{name}" if not name.startswith(self._ctx.name + ".") else name

    def register(self, name: str, modifiers: int, vk: int) -> None:
        full = self._full(name)
        try:
            self._hub.registry.register(full, modifiers, vk)
        except HotkeyConflictError:
            self._hub.conflicts.append((full, format_hotkey(modifiers, vk)))
            raise
        self._names.add(full)

    def register_text(self, name: str, text: str | None) -> bool:
        """表記文字列で登録する。空なら何もしない。競合・解釈不能は False(通知は host がまとめて出す)。"""
        if not text:
            return False
        try:
            mods, vk = parse_hotkey(text)
        except ValueError as e:
            self._ctx.log.warning("hotkey parse error name=%s: %s", name, e)
            self._hub.conflicts.append((self._full(name), f"{text}(解釈不能)"))
            return False
        try:
            self.register(name, mods, vk)
            return True
        except HotkeyError:
            return False

    def unregister(self, name: str) -> None:
        full = self._full(name)
        self._hub.drop(full)
        self._names.discard(full)

    def triggered(self, name: str) -> _TriggeredProxy:
        return _TriggeredProxy(self, name)

    def _connect(self, name: str, cb: Callable[[], None]) -> None:
        full = self._full(name)
        self._hub.add_callback(full, self._ctx.safe(cb, f"hotkey:{name}"))
        self._names.add(full)

    def release_all(self) -> None:
        for full in list(self._names):
            self._hub.drop(full)
        self._names.clear()
