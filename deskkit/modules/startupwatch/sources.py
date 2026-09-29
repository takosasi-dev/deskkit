# 自動起動の8か所の定義と読み取り(§2 の表・W-1・W-2)。Run と RunOnce は winreg で読み、スタートアップ フォルダは
# 一覧とショートカットの先(QFileInfo)を読む。開けない場所は unreadable、鍵やフォルダが無い場所は missing として先へ進む。
# ここは読むだけで、何も書かない(INV-1)。
from __future__ import annotations

import hashlib
import ntpath
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from deskkit.modules.startupwatch import _win32
from deskkit.modules.startupwatch.cmdline import same_program

RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUNONCE = r"Software\Microsoft\Windows\CurrentVersion\RunOnce"
BINARY_TEXT = "(文字でない値)"
LNK_UNKNOWN_TEXT = "(ショートカットの先を読めませんでした)"
_TEXT_TYPES = (1, 2)  # REG_SZ / REG_EXPAND_SZ(画面は書かれたまま。W-13 の比べは展開してから)


@dataclass(frozen=True)
class Location:
    key: str             # 場所の記号(hkcu_run など)
    label: str           # 画面の名前
    official: str        # 正式の場所
    kind: str            # "reg" | "folder"
    root: int = 0
    subkey: str = ""
    view: int = 0        # 0 / KEY_WOW64_64KEY / KEY_WOW64_32KEY
    folder: str = ""     # "startup" | "common_startup"
    runonce: bool = False


LOCATIONS: tuple[Location, ...] = (
    Location("hkcu_run", "サインインのたび(あなた)", r"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run",
             "reg", _win32.HKCU, RUN),
    Location("hkcu_runonce", "次のサインインで1回だけ(あなた)",
             r"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\RunOnce", "reg", _win32.HKCU, RUNONCE, runonce=True),
    Location("hklm_run64", "サインインのたび(全員)",
             r"HKEY_LOCAL_MACHINE\Software\Microsoft\Windows\CurrentVersion\Run(64 ビット)",
             "reg", _win32.HKLM, RUN, _win32.KEY_WOW64_64KEY),
    Location("hklm_run32", "サインインのたび(全員)",
             r"HKEY_LOCAL_MACHINE\Software\Microsoft\Windows\CurrentVersion\Run(32 ビット)",
             "reg", _win32.HKLM, RUN, _win32.KEY_WOW64_32KEY),
    Location("hklm_runonce64", "次に管理者がサインインしたとき1回だけ(全員)",
             r"HKEY_LOCAL_MACHINE\Software\Microsoft\Windows\CurrentVersion\RunOnce(64 ビット)",
             "reg", _win32.HKLM, RUNONCE, _win32.KEY_WOW64_64KEY, runonce=True),
    Location("hklm_runonce32", "次に管理者がサインインしたとき1回だけ(全員)",
             r"HKEY_LOCAL_MACHINE\Software\Microsoft\Windows\CurrentVersion\RunOnce(32 ビット)",
             "reg", _win32.HKLM, RUNONCE, _win32.KEY_WOW64_32KEY, runonce=True),
    Location("startup_user", "スタートアップ フォルダ(あなた)", "shell:startup", "folder", folder="startup"),
    Location("startup_common", "スタートアップ フォルダ(全員)", "shell:common startup", "folder", folder="common_startup"),
)
BY_KEY: dict[str, Location] = {loc.key: loc for loc in LOCATIONS}


@dataclass(frozen=True)
class StartupItem:
    loc: str
    name: str            # 値の名前か、フォルダの中のファイル名
    command: str         # 画面に出す「何を起動するか」(値の中身か、ショートカットの先)
    runonce: bool
    is_deskkit: bool
    compare: bytes       # 比べる中身(コマンドの行・ショートカットの先・文字でない値のバイト列)
    folder_path: str = ""  # スタートアップ フォルダの物だけ: 置かれているフォルダ
    text_value: bool = True  # 文字の値(REG_SZ / REG_EXPAND_SZ・フォルダの物)か

    @property
    def display_name(self) -> str:
        if self.folder_path and self.name.lower().endswith(".lnk"):
            return self.name[:-4]
        return self.name or "(既定)"

    def cmd_hash(self) -> str:
        return hashlib.sha256(self.compare).hexdigest()


@dataclass
class LocResult:
    loc: str
    status: str = "ok"       # ok | missing | unreadable
    error: int = 0
    items: list[StartupItem] = field(default_factory=list)
    path: str = ""           # フォルダの実際のパス(フォルダの場所だけ)
    remote: bool = False


@dataclass
class ScanResult:
    locs: dict[str, LocResult]
    ms: int

    def items(self) -> list[StartupItem]:
        return [it for r in self.locs.values() for it in r.items]


def _value_to_bytes(data: object) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, list):
        return "\0".join(str(x) for x in data).encode("utf-8")
    return repr(data).encode("utf-8")


def _read_reg(api: _win32.Api, loc: Location, deskkit_programs: set[str]) -> LocResult:
    rc, values = api.reg_read(loc.root, loc.subkey, loc.view)
    if rc == _win32.ERROR_FILE_NOT_FOUND:
        return LocResult(loc.key, "missing", rc)
    if rc != 0:
        return LocResult(loc.key, "unreadable", rc)
    res = LocResult(loc.key)
    for name, typ, data in values:
        if typ in _TEXT_TYPES and isinstance(data, str):
            if not name and not data:
                continue  # §10: 名前も中身も空の値は無視する
            is_dk = same_program(data, deskkit_programs, api.expand)
            res.items.append(StartupItem(loc.key, name, data, loc.runonce, is_dk, data.encode("utf-8")))
        else:
            if not name and (data is None or data in (b"", "")):
                continue
            res.items.append(StartupItem(loc.key, name, BINARY_TEXT, loc.runonce, False, _value_to_bytes(data),
                                         text_value=False))
    return res


def _read_folder(api: _win32.Api, loc: Location, deskkit_programs: set[str]) -> LocResult:
    path = api.known_folder(loc.folder)
    if not path:
        return LocResult(loc.key, "missing", _win32.ERROR_FILE_NOT_FOUND)
    res = LocResult(loc.key, path=path)
    try:
        res.remote = api.is_remote(path)
    except OSError:
        res.remote = False
    try:
        entries = api.list_dir(path)
    except FileNotFoundError:
        res.status, res.error = "missing", _win32.ERROR_FILE_NOT_FOUND
        return res
    except OSError as e:
        res.status, res.error = "unreadable", int(getattr(e, "winerror", 0) or _win32.ERROR_ACCESS_DENIED)
        return res
    for name, is_dir in sorted(entries, key=lambda e: e[0].lower()):
        if is_dir or name.lower() == "desktop.ini":
            continue  # §10: サブフォルダの中は見ない・desktop.ini は出さない
        full = ntpath.join(path, name)
        if name.lower().endswith(".lnk"):
            target = api.lnk_target(full)
            if target:
                res.items.append(StartupItem(loc.key, name, target, False, same_program(f'"{target}"', deskkit_programs),
                                             target.encode("utf-8"), folder_path=path))
            else:  # 先が取れない: ファイル名だけで比べる(§10)
                res.items.append(StartupItem(loc.key, name, LNK_UNKNOWN_TEXT, False, False, b"", folder_path=path))
        else:
            res.items.append(StartupItem(loc.key, name, full, False, same_program(f'"{full}"', deskkit_programs),
                                         full.encode("utf-8"), folder_path=path))
    return res


def read_location(api: _win32.Api, loc: Location, deskkit_programs: set[str]) -> LocResult:
    try:
        if loc.kind == "reg":
            return _read_reg(api, loc, deskkit_programs)
        return _read_folder(api, loc, deskkit_programs)
    except OSError as e:
        return LocResult(loc.key, "unreadable", int(getattr(e, "winerror", 0) or 0))


def read_all(api: _win32.Api, deskkit_programs: set[str], cancelled: Callable[[], bool] = lambda: False,
             clock: Callable[[], float] = time.perf_counter) -> ScanResult | None:
    """8か所を順に読む。途中で cancelled() が真になったら None(stop の途中。記録は書かない)。"""
    t0 = clock()
    out: dict[str, LocResult] = {}
    for loc in LOCATIONS:
        if cancelled():
            return None
        out[loc.key] = read_location(api, loc, deskkit_programs)
    return ScanResult(out, int((clock() - t0) * 1000))
