# フォルダの中のファイルを、リパースポイント(シンボリックリンク・ジャンクション)をたどらずに列挙する(P-6・INV-4)。
# 読めないフォルダは denied、時間切れ・中止は partial の印を付けて、読めた分だけ返す(§10)。
# ファイルの中身は読まない。読むのは属性・サイズ・更新時刻だけ。パスはログに書かない。
from __future__ import annotations

import os
import stat as _stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass

FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
# クラウドにだけある(ディスクを使っていない)ファイルの印。大きさの合計には入れない
CLOUD_ONLY = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
_CHECK_EVERY = 256


@dataclass
class WalkStats:
    bytes: int = 0
    files: int = 0
    denied: bool = False     # 一部のフォルダ・ファイルを読めなかった
    partial: bool = False    # 時間切れ・中止で途中まで


@dataclass(frozen=True)
class FileEntry:
    path: str
    size: int
    mtime: float
    attrs: int


def is_reparse(st: os.stat_result) -> bool:
    attrs = int(getattr(st, "st_file_attributes", 0))
    return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT) or _stat.S_ISLNK(st.st_mode)


def iter_files(root: str, stats: WalkStats, stop: Callable[[], bool]) -> Iterator[FileEntry]:
    """root の下のファイル(リパースポイントは除く)。stop() が True になったら partial にして終える。"""
    stack = [root]
    n = 0
    while stack:
        if stop():
            stats.partial = True
            return
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            stats.denied = True
            continue
        with it:
            while True:
                try:
                    e = next(it)
                except StopIteration:
                    break
                except OSError:
                    stats.denied = True
                    break
                n += 1
                if n % _CHECK_EVERY == 0 and stop():
                    stats.partial = True
                    return
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    stats.denied = True
                    continue
                if is_reparse(st):
                    continue  # リンク・ジャンクションの先はたどらない。リンクそのものも数えない
                if _stat.S_ISDIR(st.st_mode):
                    stack.append(e.path)
                    continue
                yield FileEntry(e.path, int(st.st_size), float(st.st_mtime), int(getattr(st, "st_file_attributes", 0)))


def dir_size(root: str, stop: Callable[[], bool]) -> WalkStats:
    """root の合計(クラウドにだけあるファイルは入れない)。"""
    stats = WalkStats()
    for f in iter_files(root, stats, stop):
        if f.attrs & CLOUD_ONLY:
            continue
        stats.bytes += f.size
        stats.files += 1
    return stats
