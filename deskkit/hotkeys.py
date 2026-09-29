# host が OverlayKit の HotkeyRegistry を1つ保持し、モジュールへ "<module>.<name>" の名前空間で貸す(D-5)。
# "Ctrl+Shift+Space" 形式の表記と (modifiers, vk) の相互変換もここに置く。
# (v0.4)試して外す(probe)・DeskKit が持つキーの一覧(snapshot)・format / parse(docs/INTERFACES_v0.4.md §2.1)。
from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from overlaykit import HotkeyConflictError, HotkeyError, HotkeyRegistry, ProbeBatch, ProbeDone, ProbeResult, ProbeTask

if TYPE_CHECKING:
    from deskkit.context import ModuleContextImpl

__all__ = [
    "HOTKEY_LABELS", "MOD_ALT", "MOD_CONTROL", "MOD_SHIFT", "MOD_WIN", "VK_NAMES", "FailedKey", "HeldKey", "HotkeyHub",
    "HotkeySnapshot", "ModuleHotkeys", "ProbeBatch", "ProbeDone", "ProbeHandle", "ProbeResult", "format_hotkey", "hotkey_label",
    "parse_hotkey", "try_parse_hotkey",
]

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x1, 0x2, 0x4, 0x8
_MOD_MASK = MOD_ALT | MOD_CONTROL | MOD_SHIFT | MOD_WIN

_MOD_NAMES = [(MOD_WIN, "Win"), (MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift")]
_MOD_ALIASES = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT,
                "win": MOD_WIN, "windows": MOD_WIN, "meta": MOD_WIN}

# 仮想キーコード(winuser.h VK_*)。記号キーの名前は米国配列の刻印(配列ごとの文字は画面側で出す。KeyFree K-10)
VK_NAMES: dict[int, str] = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x13: "Pause", 0x1B: "Esc", 0x20: "Space",
    0x21: "PageUp", 0x22: "PageDown", 0x23: "End", 0x24: "Home", 0x25: "Left", 0x26: "Up",
    0x27: "Right", 0x28: "Down", 0x2C: "PrintScreen", 0x2D: "Insert", 0x2E: "Delete",
    0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/", 0xC0: "`",
    0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
    0xE2: "OEM102",  # (v0.4)VK_OEM_102。米国配列には無いキー(JIS の「\ ろ」など)
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


def _vk_from_name(key: str) -> int | None:
    vk = _NAME_TO_VK.get(key.lower())
    if vk is not None:
        return vk
    k = key.lower()
    if k.startswith("vk") and len(k) in (3, 4):  # format_hotkey が名前の無いキーに使う "VK" + 16進2桁
        try:
            n = int(k[2:], 16)
        except ValueError:
            return None
        return n if 0 < n < 0xFF else None
    return None


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
    vk = _vk_from_name(key)
    if vk is None:
        raise ValueError(f"キー '{key}' を解釈できません")
    if mods == 0:
        raise ValueError("修飾キー(Ctrl / Alt / Shift / Win)を1つ以上含めてください")
    return mods, vk


def try_parse_hotkey(text: str) -> tuple[int, int] | None:
    """parse_hotkey の例外を出さない版。解釈できなければ(修飾キーが無いときも)None。"""
    try:
        return parse_hotkey(text)
    except (ValueError, AttributeError, TypeError):
        return None


# ホットキーの表示名(登録名 → 画面に出す名前)。知らない名前は登録名をそのまま読みやすくして出す
# (v0.4.0 で ui.main_window から移した。モジュールの画面からも使うため。KeyFree P-2)
HOTKEY_LABELS = {
    "host.quick": "クイックアクション",
    "clipshelf.open_palette": "パレットを開く",
    "clipshelf.plain_text": "書式なしで貼り付け",
    "clipshelf.toggle_pause": "記録の一時停止/再開",
    "dropsort.undo_last": "直前の移動を元に戻す",
    "modeshift.undo": "モードを元に戻す",
    "layoutkeep.save": "今の配置を保存",
    "layoutkeep.apply": "配置を適用",
    "jotdrop.open_input": "一行メモを書く",  # v0.4 H4-7
}


def hotkey_label(full: str, modes: dict[str, str]) -> str:
    """'modeshift.mode.game' → 'モード「ゲーム」に切り替え' など、ホットキーの登録名を画面向けの名前にする。"""
    if full in HOTKEY_LABELS:
        return HOTKEY_LABELS[full]
    mod, _, rest = full.partition(".")
    if mod == "modeshift" and rest.startswith("mode."):
        name = rest[len("mode."):]
        return f"モード「{modes.get(name, name)}」に切り替え"
    return rest.replace("_", " ").replace(".", " › ") or full


def format_hotkey(mods: int, vk: int) -> str:
    """(modifiers, vk) → 'Ctrl+Alt+Space'。register_text / parse_hotkey がそのまま読める書き方(名前の無いキーは VKxx)。"""
    names = [n for m, n in _MOD_NAMES if mods & m]
    names.append(VK_NAMES.get(vk, f"VK{vk:02X}"))
    return "+".join(names)


# ---- v0.4 の型(docs/INTERFACES_v0.4.md §2.1。ProbeResult / ProbeBatch / ProbeDone は overlaykit の物をそのまま出す)

class ProbeHandle(Protocol):
    def cancel(self) -> None: ...        # どのスレッドからでも呼べる。次の組の前で止まる

    @property
    def done(self) -> bool: ...


@dataclass(frozen=True)
class HeldKey:
    name: str      # 登録名(host.quick / jotdrop.open_input など)
    mods: int
    vk: int


@dataclass(frozen=True)
class FailedKey:
    name: str
    mods: int
    vk: int
    error: int     # RegisterHotKey の GetLastError(1409 = ほかのアプリが使っている)


@dataclass(frozen=True)
class HotkeySnapshot:
    held: list[HeldKey]
    failed: list[FailedKey]


class HotkeyHub:
    """host 全体で1つ。WM_HOTKEY → registry → 名前ごとのコールバック。"""

    def __init__(self, hwnd: int) -> None:
        self.registry = HotkeyRegistry(hwnd)
        self._callbacks: dict[str, list[Callable[[], None]]] = {}
        self.registry.triggered.connect(self._on_triggered)
        self.conflicts: list[tuple[str, str]] = []  # (完全名, 表記)。起動・再読み込みごとに通知して空にする
        self.combos: dict[str, tuple[int, int]] = {}  # 完全名 → (modifiers, vk)。表示・診断用
        self.failed: dict[str, str] = {}  # 登録できなかったもの(完全名 → 表記)。診断用に次の全解除まで残す
        self.failed_keys: dict[str, tuple[int, int, int]] = {}  # (v0.4)登録できなかった組(完全名 → mods, vk, エラー番号)。snapshot 用

    def register(self, full_name: str, modifiers: int, vk: int) -> None:
        """registry へ登録し、表示用に組み合わせを覚える。失敗は HotkeyError(競合は HotkeyConflictError)。"""
        try:
            self.registry.register(full_name, modifiers, vk)
        except HotkeyError as e:
            if full_name not in self.combos:  # 「登録済み」の二重登録は失敗として残さない
                self.failed_keys[full_name] = (modifiers & _MOD_MASK, vk, int(e.code))
            raise
        self.combos[full_name] = (modifiers, vk)
        self.failed.pop(full_name, None)
        self.failed_keys.pop(full_name, None)

    def combo_text(self, full_name: str) -> str | None:
        c = self.combos.get(full_name)
        return format_hotkey(*c) if c else None

    def note_conflict(self, full_name: str, text: str) -> None:
        self.conflicts.append((full_name, text))
        self.failed[full_name] = text

    def snapshot(self) -> HotkeySnapshot:
        """DeskKit(host と全モジュール)が今持っている組と、登録できなかった組(エラー番号つき。表記を読めなかった物は除く)。"""
        held = [HeldKey(n, m & _MOD_MASK, v) for n, (m, v) in sorted(self.combos.items())]
        failed = [FailedKey(n, m, v, e) for n, (m, v, e) in sorted(self.failed_keys.items()) if n not in self.combos]
        return HotkeySnapshot(held, failed)

    def _on_triggered(self, full_name: str) -> None:
        for cb in list(self._callbacks.get(full_name, [])):
            cb()  # コールバックは ModuleContext.safe で包まれている

    def add_callback(self, full_name: str, cb: Callable[[], None]) -> None:
        self._callbacks.setdefault(full_name, []).append(cb)

    def drop(self, full_name: str) -> None:
        self.registry.unregister(full_name)
        self._callbacks.pop(full_name, None)
        self.combos.pop(full_name, None)
        self.failed.pop(full_name, None)   # 外した名前は「登録できなかったキー」にも残さない(KeyFree の試しの登録。P-1)
        self.failed_keys.pop(full_name, None)
        self.conflicts[:] = [c for c in self.conflicts if c[0] != full_name]

    def unregister_all(self) -> None:
        self.registry.unregister_all()
        self._callbacks.clear()
        self.conflicts.clear()
        self.combos.clear()
        self.failed.clear()
        self.failed_keys.clear()


class _TriggeredProxy:
    def __init__(self, owner: ModuleHotkeys, name: str) -> None:
        self._owner = owner
        self._name = name

    def connect(self, cb: Callable[[], None]) -> None:
        self._owner._connect(self._name, cb)


PROBE_STOP_WAIT_S = 2.0  # モジュールを止めるとき、動いている probe が預かりを戻すのを待つ上限


class ModuleHotkeys:
    """モジュールに貸すホットキー窓口。register(name, modifiers, vk) / unregister / triggered(name)、
    (v0.4)probe / snapshot / format / parse。"""

    def __init__(self, hub: HotkeyHub, ctx: ModuleContextImpl) -> None:
        self._hub = hub
        self._ctx = ctx
        self._names: set[str] = set()
        self._probes: list[ProbeTask] = []
        self._probe_lock = threading.Lock()

    def _full(self, name: str) -> str:
        return f"{self._ctx.name}.{name}" if not name.startswith(self._ctx.name + ".") else name

    def register(self, name: str, modifiers: int, vk: int) -> None:
        full = self._full(name)
        try:
            self._hub.register(full, modifiers, vk)
        except HotkeyConflictError:
            self._hub.note_conflict(full, format_hotkey(modifiers, vk))
            raise
        self._names.add(full)

    def register_text(self, name: str, text: str | None) -> bool:
        """表記文字列で登録する。空なら何もしない。競合・解釈不能は False(通知は host がまとめて出す)。"""
        if not text:
            return False
        try:
            mods, vk = parse_hotkey(text)
        except ValueError as e:
            self._ctx.log.warning("hotkey parse error name=%s: %s", name, type(e).__name__)  # 文は書かない(入力をそのまま含むため)
            self._hub.note_conflict(self._full(name), f"{text}(解釈不能)")
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

    # ---- v0.4(H4-2〜H4-4)
    def probe(self, combos: Sequence[tuple[int, int]], on_batch: Callable[[ProbeBatch], None],
              on_done: Callable[[ProbeDone], None], batch: int = 16) -> ProbeHandle:
        """combos を host の専用スレッドで1つずつ「登録してすぐ外す」で調べる。on_batch / on_done は GUI スレッドで呼ぶ
        (ctx の safe で包む。呼ぶのは必ず probe() から戻った後)。同時に2本目は試さずに on_done(ProbeDone("busy", 0))。"""
        ctx = self._ctx
        batch_cb = ctx.safe(on_batch, "hotkey:probe")
        done_cb = ctx.safe(on_done, "hotkey:probe")

        def post_batch(b: ProbeBatch) -> None:
            ctx.post(lambda: batch_cb(b))

        def post_done(d: ProbeDone) -> None:
            ctx.post(lambda: done_cb(d))

        task = self._hub.registry.probe(list(combos), post_batch, post_done, batch)
        with self._probe_lock:
            self._probes = [p for p in self._probes if not p.done]
            if not task.done:
                self._probes.append(task)
        return task

    def snapshot(self) -> HotkeySnapshot:
        return self._hub.snapshot()

    def format(self, mods: int, vk: int) -> str:
        """register_text が受け付ける書き方("Ctrl+Alt+Space")。"""
        return format_hotkey(mods & _MOD_MASK, vk)

    def parse(self, text: str) -> tuple[int, int] | None:
        """register_text と同じ規則で読む。読めなければ(修飾キーが1つも無いときも)None。"""
        return try_parse_hotkey(text)

    def cancel_probes(self, wait_s: float = PROBE_STOP_WAIT_S) -> bool:
        """このモジュールが始めた probe を止め、預かりが0に戻るまで最大 wait_s 秒待つ。全部終わっていれば True。"""
        with self._probe_lock:
            probes, self._probes = self._probes, []
        ok = True
        for p in probes:
            p.cancel()
        for p in probes:
            if not p.done:
                th = p._thread
                if th is not None and th is not threading.current_thread():
                    th.join(wait_s)
            ok = ok and p.done
        return ok

    def release_all(self) -> None:
        if not self.cancel_probes():
            self._ctx.log.warning("hotkey probe did not stop in time")
        for full in list(self._names):
            self._hub.drop(full)
        self._names.clear()
