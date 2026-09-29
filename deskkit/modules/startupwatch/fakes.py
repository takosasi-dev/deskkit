# テストと自己検査で使う偽の Win32(_win32.Api と同じ形)。レジストリ・フォルダの中身を辞書で持ち、合図は台本
# (偽の時計の時刻と場所の記号)で送る。開いたハンドルを数えて、stop の後に全部閉じたかを確かめられる。
from __future__ import annotations

import ntpath
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from deskkit.modules.startupwatch import _win32
from deskkit.modules.startupwatch.sources import LOCATIONS

RegKey = tuple[int, str, int]  # (root, subkey, view)


def reg_key_of(loc_key: str) -> RegKey:
    loc = next(x for x in LOCATIONS if x.key == loc_key)
    return (loc.root, loc.subkey, loc.view)


@dataclass
class Signal:
    at: float                  # 偽の時計の秒
    loc: str                   # 場所の記号か "kick"
    action: Callable[[], None] | None = None


@dataclass
class FakeApi:
    reg: dict[RegKey, list[tuple[str, int, Any]]] = field(default_factory=dict)
    reg_errors: dict[RegKey, int] = field(default_factory=dict)
    notify_errors: dict[RegKey, int] = field(default_factory=dict)
    folders: dict[str, str] = field(default_factory=dict)          # "startup" / "common_startup" → パス
    files: dict[str, list[tuple[str, bool]]] = field(default_factory=dict)  # パス → (名前, フォルダか)
    folder_errors: dict[str, OSError] = field(default_factory=dict)
    remote: set[str] = field(default_factory=set)
    change_fail: set[str] = field(default_factory=set)
    lnk: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    procs: set[str] | None = field(default_factory=set)
    script: list[Signal] = field(default_factory=list)
    now: float = 0.0
    wait_fail_times: int = 0
    block_when_idle: bool = False   # 台本が尽きたら本物の event を待つ(スレッドで動かすテスト用)
    idle_timeouts: int = 0          # 台本が尽きたあとに返す「時間切れ」の回数(poll のテスト用)

    def __post_init__(self) -> None:
        self._next = 1000
        self.open: set[int] = set()
        self.kinds: dict[int, str] = {}
        self._hkey_loc: dict[int, RegKey] = {}
        self._wait_loc: dict[int, str] = {}
        self._signaled: set[int] = set()
        self._cv = threading.Condition()
        self.reads = 0
        self.proc_reads = 0
        self.waits: list[int] = []

    # ---- 共通
    def _new(self, kind: str) -> int:
        self._next += 1
        self.open.add(self._next)
        self.kinds[self._next] = kind
        return self._next

    def _close(self, h: int) -> None:
        self.open.discard(h)

    def clock(self) -> float:
        return self.now

    # ---- レジストリ
    def reg_read(self, root: int, subkey: str, view: int) -> tuple[int, list[tuple[str, int, Any]]]:
        k = (root, subkey, view)
        self.reads += 1
        if k in self.reg_errors:
            return self.reg_errors[k], []
        if k not in self.reg:
            return _win32.ERROR_FILE_NOT_FOUND, []
        return 0, list(self.reg[k])

    def reg_open_notify(self, root: int, subkey: str, view: int) -> tuple[int, int]:
        k = (root, subkey, view)
        if k in self.reg_errors:
            return self.reg_errors[k], 0
        if k not in self.reg:
            return _win32.ERROR_FILE_NOT_FOUND, 0
        h = self._new("key")
        self._hkey_loc[h] = k
        return 0, h

    def reg_notify(self, hkey: int, event: int) -> int:
        k = self._hkey_loc[hkey]
        if k in self.notify_errors:
            return self.notify_errors[k]
        loc = next(x.key for x in LOCATIONS if (x.root, x.subkey, x.view) == k)
        self._wait_loc[event] = loc
        return 0

    def reg_close(self, hkey: int) -> None:
        self._close(hkey)

    # ---- event
    def create_event(self) -> int:
        return self._new("event")

    def set_event(self, h: int) -> None:
        with self._cv:
            self._signaled.add(h)
            self._cv.notify_all()

    def close_handle(self, h: int) -> None:
        self._close(h)

    # ---- フォルダ
    def find_first_change(self, path: str) -> int | None:
        if path in self.change_fail:
            return None
        h = self._new("change")
        which = next((k for k, v in self.folders.items() if v == path), "")
        loc = {"startup": "startup_user", "common_startup": "startup_common"}.get(which, "")
        self._wait_loc[h] = loc
        return h

    def find_next_change(self, h: int) -> bool:
        return True

    def find_close_change(self, h: int) -> None:
        self._close(h)

    def known_folder(self, which: str) -> str | None:
        return self.folders.get(which)

    def is_remote(self, path: str) -> bool:
        return path in self.remote or path.startswith("\\\\")

    def list_dir(self, path: str) -> list[tuple[str, bool]]:
        if path in self.folder_errors:
            raise self.folder_errors[path]
        if path not in self.files:
            raise FileNotFoundError(2, "missing")
        return list(self.files[path])

    def lnk_target(self, path: str) -> str:
        return self.lnk.get(path, "")

    def expand(self, text: str) -> str:
        out = text
        for k, v in self.env.items():
            out = out.replace(f"%{k}%", v)
        return out

    def process_names(self) -> set[str] | None:
        self.proc_reads += 1
        return None if self.procs is None else set(self.procs)

    # ---- 待ち(台本)
    def wait_any(self, handles: list[int], timeout_ms: int) -> int:
        self.waits.append(timeout_ms)
        if self.wait_fail_times > 0:
            self.wait_fail_times -= 1
            return _win32.WAIT_RESULT_FAILED
        while True:
            with self._cv:
                for i, h in enumerate(handles[:2]):
                    if h in self._signaled:
                        self._signaled.discard(h)
                        return i
            if self.script:
                sig = self.script[0]
                limit = self.now + (timeout_ms / 1000.0 if timeout_ms != _win32.INFINITE else float("inf"))
                if sig.at <= limit:
                    self.script.pop(0)
                    self.now = max(self.now, sig.at)
                    if sig.action is not None:
                        sig.action()
                    if sig.loc == "kick":
                        return 1
                    for i, h in enumerate(handles):
                        if i >= 2 and self._wait_loc.get(h) == sig.loc:
                            return i
                    continue  # 知らせを置いていない場所への合図は届かない
                self.now = limit
                return _win32.WAIT_RESULT_TIMEOUT
            if timeout_ms < 60_000:  # settle の短い待ちは、台本が尽きていれば時間切れ
                self.now += timeout_ms / 1000.0
                return _win32.WAIT_RESULT_TIMEOUT
            if self.idle_timeouts > 0 and timeout_ms != _win32.INFINITE:
                self.idle_timeouts -= 1
                self.now += timeout_ms / 1000.0
                return _win32.WAIT_RESULT_TIMEOUT
            if not self.block_when_idle:
                return 0  # 台本が尽きた: 止める合図とみなす(同期で動かすテスト用)
            with self._cv:
                self._cv.wait(0.05)

    # ---- 便利
    def set_values(self, loc_key: str, values: list[tuple[str, str]]) -> None:
        self.reg[reg_key_of(loc_key)] = [(n, 1, v) for n, v in values]

    def add_value(self, loc_key: str, name: str, value: str, typ: int = 1) -> None:
        self.reg.setdefault(reg_key_of(loc_key), []).append((name, typ, value))

    def remove_value(self, loc_key: str, name: str) -> None:
        k = reg_key_of(loc_key)
        self.reg[k] = [v for v in self.reg.get(k, []) if v[0] != name]

    def set_folder(self, which: str, path: str, names: list[str]) -> None:
        self.folders[which] = path
        self.files[path] = [(n, False) for n in names]

    def add_file(self, path: str, name: str, target: str | None = None) -> None:
        self.files.setdefault(path, []).append((name, False))
        if target is not None:
            self.lnk[ntpath.join(path, name)] = target


def sample_api() -> FakeApi:
    """8か所がそろった、ふつうの PC に似せた偽物。"""
    api = FakeApi()
    api.set_values("hkcu_run", [("OneDriveSample", r'"C:\Users\sample\AppData\Local\OneDriveSample\od.exe" /background')])
    api.set_values("hkcu_runonce", [])
    api.set_values("hklm_run64", [("SecurityHealthSample", r"%windir%\system32\SecurityHealthSample.exe")])
    api.set_values("hklm_run32", [])
    api.set_values("hklm_runonce64", [])
    api.set_values("hklm_runonce32", [])
    api.set_folder("startup", r"C:\Users\sample\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup", ["desktop.ini"])
    api.set_folder("common_startup", r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\StartUp", ["desktop.ini"])
    api.env = {"windir": r"C:\Windows", "ProgramFiles": r"C:\Program Files"}
    return api
