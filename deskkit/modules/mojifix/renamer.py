# フォルダの中の、濁点が分かれた名前(NFD)の走査・名前の変更・メモリだけの取り消し(M-14・M-15・FR-25〜FR-30)。
# 名前を変えるのは、利用者が「直す」「元に戻す」を押したときの os.rename だけ(INV-2)。上書きはしない(P-11)。
# 前後の名前はメモリにだけ持ち、ディスクにもログにも残さない(M-15・INV-5)。
from __future__ import annotations

import os
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from deskkit.modules.mojifix.compose import compose

MAX_ITEMS = 200_000
ATTR_SYSTEM = stat.FILE_ATTRIBUTE_SYSTEM if hasattr(stat, "FILE_ATTRIBUTE_SYSTEM") else 0x4
ATTR_REPARSE = stat.FILE_ATTRIBUTE_REPARSE_POINT if hasattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT") else 0x400
FORBIDDEN_ENV = ("WINDIR", "ProgramFiles", "ProgramFiles(x86)", "ProgramData", "APPDATA", "LOCALAPPDATA")

MSG_FORBIDDEN = "このフォルダは調べられません"
MSG_ROOT = "時間がかかります。続けますか"
MSG_TOO_MANY = "項目が多すぎます(20 万まで)。フォルダを分けてください"
MSG_WARN = "ほかのソフトが前の名前で覚えていると、見つからなくなることがあります"
MSG_UNDO_NOTE = "DeskKit を閉じると元に戻せなくなります"

ST_OK = "直せます"
ST_EXISTS = "同じ名前がすでにあります"
ST_CLASH = "直す名前同士が重なります"

_WINERR = {32: "ほかのソフトが使っています", 5: "変える権限がありません", 2: "見つかりません", 3: "見つかりません",
           183: "同じ名前がすでにあります"}


class CancelledError(Exception):
    pass


class ScanError(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def forbidden_roots(env: dict[str, str] | None = None) -> list[str]:
    e = env if env is not None else dict(os.environ)
    out: list[str] = []
    for k in FORBIDDEN_ENV:
        v = e.get(k) or e.get(k.upper())
        if v:
            out.append(os.path.normcase(os.path.normpath(v)))
    return out


def is_forbidden(folder: Path, env: dict[str, str] | None = None) -> bool:
    """FR-25: Windows・Program Files・ProgramData・AppData とその中は調べない。"""
    f = os.path.normcase(os.path.normpath(os.path.abspath(str(folder))))
    return any(f == r or f.startswith(r.rstrip("\\/") + os.sep) for r in forbidden_roots(env))


def is_drive_root(folder: Path) -> bool:
    p = Path(os.path.abspath(str(folder)))
    return p.parent == p


def winerror_text(e: OSError) -> str:
    """FR-29 の理由の文。"""
    code = getattr(e, "winerror", None)
    if code is None and isinstance(e, FileExistsError):
        code = 183
    if code is None and isinstance(e, FileNotFoundError):
        code = 2
    if code is None and isinstance(e, PermissionError):
        code = 5
    return _WINERR.get(int(code), "変えられませんでした") if code is not None else "変えられませんでした"


@dataclass
class Item:
    path: Path              # 今の場所(フルパス)
    rel_parent: str         # 選んだフォルダからの相対の場所(画面用)。直下なら ""
    old: str
    new: str
    is_dir: bool
    depth: int
    status: str             # ST_OK / ST_EXISTS / ST_CLASH
    result: str = ""        # 変えたあとの状態(「直しました」や FR-29 の理由)

    @property
    def fixable(self) -> bool:
        return self.status == ST_OK


@dataclass
class ScanResult:
    folder: Path
    checked: int
    items: list[Item]


def _attrs(entry: os.DirEntry[str]) -> int:
    try:
        return int(getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0))
    except OSError:
        return 0


def scan(folder: Path, recursive: bool = True, cancel: Callable[[], bool] = lambda: False,
         progress: Callable[[int], None] = lambda _n: None) -> ScanResult:
    """FR-26・FR-27: リパースポイントとシステム属性の項目は一覧に入れず、中もたどらない。20 万項目で止める。"""
    checked = 0
    items: list[Item] = []
    stack: list[tuple[Path, str, int]] = [(folder, "", 0)]
    while stack:
        cur, rel, depth = stack.pop()
        if cancel():
            raise CancelledError()
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except OSError:
            continue  # 読めないフォルダは飛ばす
        existing = {e.name.casefold() for e in entries}
        found: list[Item] = []
        for e in entries:
            a = _attrs(e)
            if a & (ATTR_REPARSE | ATTR_SYSTEM):
                continue
            checked += 1
            if checked > MAX_ITEMS:
                raise ScanError("too_many_items", MSG_TOO_MANY)
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            new = compose(e.name)
            if new != e.name:
                st = ST_EXISTS if new.casefold() in existing else ST_OK
                found.append(Item(Path(e.path), rel, e.name, new, is_dir, depth, st))
            if is_dir and recursive:
                stack.append((Path(e.path), f"{rel}\\{e.name}" if rel else e.name, depth + 1))
        keys: dict[str, int] = {}
        for x in found:
            keys[x.new.casefold()] = keys.get(x.new.casefold(), 0) + 1
        for x in found:
            if keys[x.new.casefold()] > 1:
                x.status = ST_CLASH
        items.extend(found)
        progress(checked)
    items.sort(key=lambda x: (x.rel_parent.casefold(), x.old.casefold()))
    return ScanResult(folder, checked, items)


@dataclass(frozen=True)
class Done:
    before: Path
    after: Path


@dataclass
class RenameResult:
    renamed: int
    failed: int
    skipped: int
    done: list[Done]


def rename_items(items: Sequence[Item], cancel: Callable[[], bool] = lambda: False,
                 progress: Callable[[int, int], None] = lambda _d, _t: None,
                 rename: Callable[[Path, Path], None] = os.rename) -> RenameResult:
    """M-14: 深い場所から順に変える。1件ずつ直前に「元の名前がある・行き先が無い」を確かめる。中止は今の1件を終えてから。"""
    order = sorted((x for x in items if x.fixable), key=lambda x: -x.depth)
    done: list[Done] = []
    failed = skipped = 0
    for i, x in enumerate(order):
        if cancel():
            break
        src = x.path
        dst = x.path.with_name(x.new)
        if not os.path.lexists(src):
            x.result = "見つかりません"
            skipped += 1
            continue
        if _exists_exact(dst):
            x.result = ST_EXISTS
            skipped += 1
            continue
        try:
            rename(src, dst)
        except OSError as e:
            x.result = winerror_text(e)
            failed += 1
            continue
        x.result = "直しました"
        done.append(Done(src, dst))
        progress(i + 1, len(order))
    return RenameResult(len(done), failed, skipped, done)


def _exists_exact(p: Path) -> bool:
    """行き先と同じ名前(大文字小文字を区別しない)があるか。NFC と NFD は別の名前として並べる(P-11)。"""
    try:
        with os.scandir(p.parent) as it:
            key = p.name.casefold()
            return any(e.name.casefold() == key for e in it)
    except OSError:
        return os.path.lexists(p)


def undo(done: Sequence[Done], cancel: Callable[[], bool] = lambda: False,
         rename: Callable[[Path, Path], None] = os.rename) -> RenameResult:
    """FR-30: 逆の順に戻す(後の名前があり、前の名前が無いものだけ)。"""
    back: list[Done] = []
    failed = skipped = 0
    for d in reversed(done):
        if cancel():
            break
        if not os.path.lexists(d.after) or _exists_exact(d.before):
            skipped += 1
            continue
        try:
            rename(d.after, d.before)
        except OSError:
            failed += 1
            continue
        back.append(Done(d.after, d.before))
    return RenameResult(len(back), failed, skipped, back)
