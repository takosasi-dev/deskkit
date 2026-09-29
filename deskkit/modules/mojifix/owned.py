# その回に MojiFix 自身が作ったファイルとフォルダの一覧(作った順)と、中止・失敗のときの後片づけ(FR-4・INV-6)。
# 出力の名前の予約(M-16: O_EXCL / mkdir、` (2)` から 1,000 まで、書けなければ「ドキュメント\MojiFix」)もここに置く。
# 利用者のファイルを消す経路は持たない: 消すのは discard() が、この一覧に自分で足したものだけ。
from __future__ import annotations

import errno
import os
import threading
from collections.abc import Callable
from pathlib import Path

MAX_NAME_TRIES = 1000
WIN_DISK_FULL = (112, 39)       # ERROR_DISK_FULL / ERROR_HANDLE_DISK_FULL
WIN_DENIED = (5, 19, 32, 33)    # ACCESS_DENIED / WRITE_PROTECT / SHARING / LOCK


class OutputError(Exception):
    """出力の場所を用意できなかった。code は ops の reason(disk_full / names_exhausted)か None。"""

    def __init__(self, code: str | None) -> None:
        super().__init__(code or "failed")
        self.code = code


def is_disk_full(e: OSError) -> bool:
    return getattr(e, "winerror", None) in WIN_DISK_FULL or e.errno == errno.ENOSPC


def is_denied(e: OSError) -> bool:
    return isinstance(e, PermissionError) or getattr(e, "winerror", None) in WIN_DENIED or e.errno in (errno.EACCES, errno.EROFS)


class Owned:
    """作った順の一覧。ファイルの名前を変えたら renamed() で差し替える。"""

    def __init__(self) -> None:
        self._items: list[tuple[str, Path]] = []  # ("file" | "dir", path)
        self._mu = threading.Lock()

    def add_file(self, p: Path) -> None:
        with self._mu:
            self._items.append(("file", p))

    def add_dir(self, p: Path) -> None:
        with self._mu:
            self._items.append(("dir", p))

    def renamed(self, old: Path, new: Path) -> None:
        with self._mu:
            self._items = [(k, new if (k == "file" and p == old) else p) for k, p in self._items]

    def forget(self, p: Path) -> None:
        with self._mu:
            self._items = [(k, q) for k, q in self._items if q != p]

    def count(self) -> int:
        return len(self._items)

    def discard_one(self, p: Path) -> None:
        """一覧にある自分のファイル1つだけを消す(FR-23 の壊れていた項目の .part)。一覧に無ければ何もしない。"""
        with self._mu:
            if ("file", p) not in self._items:
                return
            self._items.remove(("file", p))
        _remove_own_file(p)

    def discard(self) -> int:
        """FR-4: 自分が作ったものを後ろから消す。消せなかった数を返す。"""
        with self._mu:
            items = list(reversed(self._items))
            self._items = []
        left = 0
        for kind, p in items:
            if kind == "file":
                left += 0 if _remove_own_file(p) else 1
            else:
                try:
                    os.rmdir(p)  # FR-4 の後片づけ: 自分が mkdir で作ったフォルダ(空になっていれば)だけ
                except FileNotFoundError:
                    pass
                except OSError:
                    left += 1
        return left


def _remove_own_file(p: Path) -> bool:
    """FR-4 の後片づけ: Owned に自分で足した(O_EXCL で新しく作った)ファイルだけを消す。利用者のファイルはここに来ない。"""
    try:
        os.remove(p)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _excl_create(p: Path) -> None:
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
    os.close(fd)


def _numbered(stem: str, ext: str, n: int) -> str:
    return f"{stem}{ext}" if n == 1 else f"{stem} ({n}){ext}"


def reserve_file_in(folder: Path, stem: str, ext: str, owned: Owned,
                    create: Callable[[Path], None] = _excl_create) -> Path:
    for n in range(1, MAX_NAME_TRIES + 1):
        p = folder / _numbered(stem, ext, n)
        try:
            create(p)
        except FileExistsError:
            continue
        owned.add_file(p)
        return p
    raise OutputError("names_exhausted")


def reserve_dir_in(folder: Path, name: str, owned: Owned) -> Path:
    for n in range(1, MAX_NAME_TRIES + 1):
        p = folder / _numbered(name, "", n)
        try:
            os.mkdir(p)
        except FileExistsError:
            continue
        owned.add_dir(p)
        return p
    raise OutputError("names_exhausted")


def reserve(kind: str, folder: Path, stem: str, ext: str, owned: Owned, fallback: Callable[[], Path]) -> tuple[Path, bool]:
    """M-16: 元と同じフォルダに予約し、書けなければ fallback()(ドキュメント\\MojiFix)へ。戻り値は (パス, 置き場を替えたか)。"""

    def once(base: Path) -> Path:
        if kind == "dir":
            return reserve_dir_in(base, stem, owned)
        return reserve_file_in(base, stem, ext, owned)

    try:
        return once(folder), False
    except OSError as e:
        if is_disk_full(e):
            raise OutputError("disk_full") from None
        if not is_denied(e) and not isinstance(e, FileNotFoundError):
            raise OutputError(None) from None
    fb = fallback()
    try:
        fb.mkdir(parents=True, exist_ok=True)
        return once(fb), True
    except OSError as e:
        raise OutputError("disk_full" if is_disk_full(e) else None) from None
