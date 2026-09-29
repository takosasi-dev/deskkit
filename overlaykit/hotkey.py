# RegisterHotKey / UnregisterHotKey を名前付きで管理し、WM_HOTKEY を triggered(name) に変換する。
# 競合時は HotkeyConflictError。別キーへの自動振り替えはしない(INV-4)。押下内容はログに出さない。
# (v0.4)probe: 専用スレッドで組み合わせを1つずつ「登録してすぐ外す」で調べる(KeyFree 仕様書 §2 の (a)〜(g))。
from __future__ import annotations

import ctypes
import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from ctypes import wintypes as w
from dataclasses import dataclass
from typing import Protocol

from PySide6.QtCore import QCoreApplication, QObject, Signal

from overlaykit.errors import HotkeyConflictError, HotkeyError

log = logging.getLogger("overlaykit")
ERROR_HOTKEY_ALREADY_REGISTERED = 1409  # winerror.h
MOD_NOREPEAT = 0x4000  # winuser.h
MOD_MASK = 0x000F  # MOD_ALT | MOD_CONTROL | MOD_SHIFT | MOD_WIN
WM_HOTKEY = 0x0312
PM_NOREMOVE, PM_REMOVE = 0x0000, 0x0001
# probe が使う id の範囲(アプリの id は 0x0000〜0xBFFF。隠しウィンドウの登録は hWnd が違うので混ざらないが、読みやすさのため上の方を使う)
PROBE_ID_FIRST, PROBE_ID_LAST = 0x8000, 0xBFFF
_DRAIN_MAX = 1000  # 1回に取り出す WM_HOTKEY の上限(押しっぱなしでも止まらないように)


class Win32Api(Protocol):
    def register_hotkey(self, hwnd: int, hid: int, mods: int, vk: int) -> bool:
        """hwnd=0(NULL)なら呼んだスレッドに結び付き、WM_HOTKEY はそのスレッドのキューに届く(probe が使う)。"""
        ...

    def unregister_hotkey(self, hwnd: int, hid: int) -> bool: ...
    def get_last_error(self) -> int: ...

    def peek_message(self, msg_min: int, msg_max: int, remove: bool) -> tuple[int, int, int] | None:
        """呼んだスレッドのキューを PeekMessageW(hWnd=NULL)で見る。(message, wParam, lParam) か None。
        初めて呼ぶとスレッドのメッセージキューが作られる。"""
        ...


class _RealApi:
    def __init__(self) -> None:
        self._u = ctypes.WinDLL("user32", use_last_error=True)
        self._u.RegisterHotKey.argtypes = [w.HWND, ctypes.c_int, w.UINT, w.UINT]
        self._u.RegisterHotKey.restype = w.BOOL
        self._u.UnregisterHotKey.argtypes = [w.HWND, ctypes.c_int]
        self._u.UnregisterHotKey.restype = w.BOOL
        self._u.PeekMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
        self._u.PeekMessageW.restype = w.BOOL

    def register_hotkey(self, hwnd: int, hid: int, mods: int, vk: int) -> bool:
        return bool(self._u.RegisterHotKey(hwnd or None, hid, mods, vk))

    def unregister_hotkey(self, hwnd: int, hid: int) -> bool:
        return bool(self._u.UnregisterHotKey(hwnd or None, hid))

    def get_last_error(self) -> int:
        return ctypes.get_last_error()

    def peek_message(self, msg_min: int, msg_max: int, remove: bool) -> tuple[int, int, int] | None:
        msg = w.MSG()
        if not self._u.PeekMessageW(ctypes.byref(msg), None, msg_min, msg_max, PM_REMOVE if remove else PM_NOREMOVE):
            return None
        return int(msg.message), int(msg.wParam or 0), int(msg.lParam or 0)


@dataclass(frozen=True)
class RegisterResult:
    ok: tuple[str, ...]
    failed: tuple[tuple[str, Exception], ...]


# ---- probe(v0.4。形は docs/INTERFACES_v0.4.md §2.1。deskkit.hotkeys からも同じ物を出す)

@dataclass(frozen=True)
class ProbeResult:
    mods: int            # MOD_ALT=1 / MOD_CONTROL=2 / MOD_SHIFT=4 / MOD_WIN=8 の和(MOD_NOREPEAT は含めない)
    vk: int
    state: str           # "free" | "used"(GetLastError 1409)| "error"(それ以外)| "deskkit"(registry 自身が持つ組。試さない)
    error: int = 0       # state == "error" のときの GetLastError


@dataclass(frozen=True)
class ProbeBatch:
    results: list[ProbeResult]
    pressed: list[tuple[int, int]]   # このバッチの間に押された組(mods, vk)。WM_HOTKEY を取り出した物


@dataclass(frozen=True)
class ProbeDone:
    reason: str          # "finished" | "cancelled" | "error" | "release_failed" | "busy"
    checked: int         # 試し終えた組の数
    error: int = 0       # release_failed / error のときの GetLastError(無ければ 0)


class ProbeTask:
    """probe 1回分の操作ハンドル(ProbeHandle)。cancel() はどのスレッドからでも呼べ、次の組の前で止まる。"""

    def __init__(self) -> None:
        self._cancel = threading.Event()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def done(self) -> bool:
        """試すスレッドが終わり、預かりを0に戻したら True(on_done より前に立つ)。"""
        return self._done.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    def _finish(self) -> None:
        self._done.set()


class HotkeyRegistry(QObject):
    triggered = Signal(str)

    def __init__(self, hwnd: int, api: Win32Api | None = None) -> None:
        super().__init__()
        self._hwnd = hwnd
        self._api: Win32Api = api or _RealApi()
        self._by_name: dict[str, tuple[int, int, int]] = {}
        self._by_id: dict[int, str] = {}
        self._next_id = 1
        self._held: frozenset[tuple[int, int]] = frozenset()  # 持っている組(mods, vk)。probe のスレッドが読む(差し替えは原子的)
        self._probe_lock = threading.Lock()
        self._probe: ProbeTask | None = None
        self._probe_held: set[int] = set()  # probe が今預かっている id(登録と解除の間だけ1つ)
        self._probe_next = PROBE_ID_FIRST
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.cancel_probe)
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
        self._refresh_held()
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
        self._refresh_held()
        self._api.unregister_hotkey(self._hwnd, entry[0])

    def unregister_all(self) -> None:
        for name in list(self._by_name):
            self.unregister(name)

    def names(self) -> list[str]:
        return list(self._by_name)

    def combo(self, name: str) -> tuple[int, int] | None:
        e = self._by_name.get(name)
        return (e[1], e[2]) if e else None

    def handle_wm_hotkey(self, hid: int) -> None:
        """WM_HOTKEY の wParam を受けて triggered を発火する(host の隠しウィンドウから呼ぶ)。"""
        name = self._by_id.get(hid)
        if name is not None:
            self.triggered.emit(name)

    def _refresh_held(self) -> None:
        self._held = frozenset((m & MOD_MASK, v) for _h, m, v in self._by_name.values())

    # ---- probe(試して外す)
    def probe(self, combos: Iterable[tuple[int, int]], on_batch: Callable[[ProbeBatch], None],
              on_done: Callable[[ProbeDone], None], batch: int = 16) -> ProbeTask:
        """専用スレッドで combos を1つずつ試す。on_batch / on_done は **試すスレッドから** 呼ぶ(GUI へ運ぶのは呼び出し側)。
        同時に1本だけ。動いている間の2本目は、試さずに呼んだスレッドで on_done(ProbeDone("busy", 0)) を呼ぶ。"""
        items = [(int(m) & MOD_MASK, int(v)) for m, v in combos]
        size = max(1, int(batch))
        with self._probe_lock:
            busy = self._probe is not None and not self._probe.done
            task = ProbeTask()
            if not busy:
                self._probe = task
        if busy:
            task._finish()
            on_done(ProbeDone("busy", 0))
            return task
        th = threading.Thread(target=self._probe_run, args=(task, items, on_batch, on_done, size),
                              name="overlaykit-hotkey-probe", daemon=True)
        task._thread = th
        th.start()
        return task

    def probe_active(self) -> bool:
        p = self._probe
        return p is not None and not p.done

    def probe_holding(self) -> int:
        """probe が今預かっている組の数(登録と解除の間だけ 1。終わった後は 0 のはず)。"""
        return len(self._probe_held)

    def cancel_probe(self, wait_s: float = 2.0) -> bool:
        """動いている probe を止め、終わるまで最大 wait_s 秒待つ。終わっていれば True。"""
        p = self._probe
        if p is None:
            return True
        p.cancel()
        th = p._thread
        if th is not None and th is not threading.current_thread():
            th.join(wait_s)
        return p.done

    def _next_probe_id(self) -> int:
        hid = self._probe_next
        self._probe_next = PROBE_ID_FIRST if hid >= PROBE_ID_LAST else hid + 1
        return hid

    def _drain_pressed(self, api: Win32Api) -> list[tuple[int, int]]:
        pressed: list[tuple[int, int]] = []
        for _ in range(_DRAIN_MAX):
            m = api.peek_message(WM_HOTKEY, WM_HOTKEY, True)
            if m is None:
                break
            _msg, _wp, lp = m
            pressed.append((lp & 0xFFFF & MOD_MASK, (lp >> 16) & 0xFFFF))
        return pressed

    def _probe_run(self, task: ProbeTask, items: list[tuple[int, int]], on_batch: Callable[[ProbeBatch], None],
                   on_done: Callable[[ProbeDone], None], size: int) -> None:
        api = self._api
        reason, checked, err = "finished", 0, 0
        results: list[ProbeResult] = []
        try:
            api.peek_message(WM_HOTKEY, WM_HOTKEY, False)  # (d) このスレッドのメッセージキューを作らせる
            for mods, vk in items:
                if task.cancelled:
                    reason = "cancelled"
                    break
                if (mods, vk) in self._held:
                    results.append(ProbeResult(mods, vk, "deskkit"))
                else:
                    hid = self._next_probe_id()
                    # (a) 登録と解除の間に何も挟まない(記録は解除の後)
                    if api.register_hotkey(0, hid, mods | MOD_NOREPEAT, vk):
                        self._probe_held.add(hid)
                        if not api.unregister_hotkey(0, hid):
                            err = api.get_last_error()
                            reason = "release_failed"
                            break
                        self._probe_held.discard(hid)
                        results.append(ProbeResult(mods, vk, "free"))
                    else:
                        code = api.get_last_error()
                        if code == ERROR_HOTKEY_ALREADY_REGISTERED:
                            results.append(ProbeResult(mods, vk, "used"))
                        else:
                            results.append(ProbeResult(mods, vk, "error", code))
                checked += 1
                if len(results) >= size:
                    batch_results, results = results, []
                    on_batch(ProbeBatch(batch_results, self._drain_pressed(api)))
        except Exception:  # noqa: BLE001 - 試すスレッドの例外を外へ出さず、預かりを戻して error で終える
            log.exception("hotkey probe failed")
            reason = "error"
        finally:
            for hid in list(self._probe_held):  # (f) 終わり方によらず預かりを0に戻す
                try:
                    if api.unregister_hotkey(0, hid):
                        self._probe_held.discard(hid)
                    else:
                        log.warning("hotkey probe release failed")
                except Exception:  # noqa: BLE001
                    log.exception("hotkey probe release failed")
            try:
                pressed = self._drain_pressed(api)
            except Exception:  # noqa: BLE001
                log.exception("hotkey probe drain failed")
                pressed = []
            if results or pressed:
                try:
                    on_batch(ProbeBatch(results, pressed))
                except Exception:  # noqa: BLE001
                    log.exception("hotkey probe on_batch failed")
                    if reason == "finished":
                        reason = "error"
            log.info("hotkey probe done reason=%s checked=%d", reason, checked)  # 件数だけ(組の名前は書かない)
            task._finish()
            try:
                on_done(ProbeDone(reason, checked, err))
            except Exception:  # noqa: BLE001
                log.exception("hotkey probe on_done failed")
