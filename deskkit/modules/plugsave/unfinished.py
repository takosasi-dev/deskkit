# 途中で止まった回の手当て(v0.4.1。docs/v0.4.1/plugsave.md)。
# 1MiB 未満のファイルは fsync しない(copier.FSYNC_MIN_BYTES)ので、回の途中で抜かれる・落ちると、最終の名前のまま中身の欠けた
# ファイルが残りうる。そこで「終わらずに止まった回」をドライブ側の印で分かるようにし、次の回でその回に書いた小さいファイルを確かめる。
#
# 印: 回の一時フォルダ `<PC>\_作業中\<回の ID>\` そのもの(ID は乱数の 32 桁の 16 進。ファイル名・パスを持たない)。
#     回が終わったら消し、止まったら(抜いた・落ちた・PC が止まった・利用者が止めた)残す。フォルダの作成日時がその回の始まり。
#     中の部品(staging.py): `alive` = v0.4.1 の回の目印(空のファイル)。更新日時を回の途中で進め、前の回の終わりに近い時刻にする。
#                           `end`   = 次の回が引き継いだときに作る空のフォルダ。作成日時がその回の終わりの上限(次の回が書いた物を含めない)。
#                                     時計が戻ったと分かったときは、作成日時を始まりより前にして「すべて確かめる」を覚えておく。
#                           `from`  = 確かめ終えたところ(空のフォルダ)。作成日時より前のファイルは確かめ済み(上限で区切った回の続き)。
# 見分け方: バックアップ先のファイルの作成日時が、印の [始まり - 2 秒, 終わり) に入る小さいファイル、または今より先の作成日時の
#     小さいファイルを「確かめる」。作成日時は PlugSave が `.part` を作った時刻(os.utime は作成日時を変えない)。名前の変更で保たれる。
#     ただしトンネリング(同じフォルダで消した・移した名前を 15 秒以内に作り直すと、前の作成日時が戻る)で、変わったファイルは
#     古い版の作成日時になるため、copier が名前の変更のあとで `.part` の作成日時に戻す(set_birth_ns)。
# 作成日時で見分けられないとき(読めない・ファイルシステムが分からない・時計が戻った・v0.4.0 が残した印で目印が無い)は、
#     小さいファイルをすべて確かめる(安全な側)。
from __future__ import annotations

import ctypes
import dataclasses
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from deskkit.modules.plugsave import staging

MARGIN_NS = 2_000_000_000          # 幅。FAT の仲間の刻み(2 秒)と、時計の粗さを吸収する
DAY_NS = 86_400 * 10**9
NO_END = 1 << 62                   # 終わりが分からない(計画だけの回。まだ誰も書いていない)
BIRTH_FS = frozenset({"NTFS", "EXFAT", "FAT32", "FAT", "FAT16", "FAT12", "REFS"})   # 作成日時を持つファイルシステム
_EPOCH_AS_FILETIME = 116_444_736_000_000_000   # 1601-01-01 から 1970-01-01 までの 100ns の数

# 「すべて確かめる」の理由(画面の 1 行を合わせる)
R_BIRTH = "birth"      # 作成日時が読めない・ファイルシステムが分からない
R_LEGACY = "legacy"    # v0.4.0 が残した印(目印が無い。トンネリングの手当てが無かった)
R_CLOCK = "clock"      # 時計が戻った
_RANK = {"": 0, R_BIRTH: 1, R_LEGACY: 2, R_CLOCK: 3}


@dataclass(frozen=True)
class Marker:
    path: str
    birth_ns: int           # 0 = 読めない
    end_ns: int = 0         # 0 = まだ閉じていない(`end` が無い)。-1 = `end` はあるが作成日時が読めない
    from_ns: int = 0        # 0 = 確かめ終えたところが無い。-1 = `from` はあるが作成日時が読めない
    last_ns: int = 0        # 閉じていない印の、終わりに近い時刻(`alive` の更新日時・残った .part)。0 = 分からない
    versioned: bool = True  # v0.4.1 の目印(`alive`)がある


def birth_of(st: Any) -> int:
    """stat の作成日時(ns)。無い・0 以下なら 0。"""
    v = getattr(st, "st_birthtime_ns", None)
    return int(v) if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def dir_birth(path: str) -> int:
    try:
        return birth_of(os.stat(path))
    except OSError:
        return 0


def find_markers(pc_dir: str) -> list[Marker]:
    """`<PC>\\_作業中\\` の中の、止まった回の印を読む(読むだけ)。印とみなすのは、32 桁の 16 進の名前のフォルダで、
    中身が `<数字>.part`・空の `alive`・`end`・`from` のフォルダだけのもの(ほかの物が入っていれば、PlugSave が片づけられないので印にしない)。"""
    work = os.path.join(pc_dir, staging.WORK_DIR)
    try:
        runs = list(os.scandir(work))
    except OSError:
        return []
    out: list[Marker] = []
    for run in runs:
        if not staging.RUN_ID_RE.match(run.name):
            continue
        try:
            if not run.is_dir(follow_symlinks=False) or run.is_junction():
                continue
            birth = birth_of(run.stat(follow_symlinks=False))
            inner = list(os.scandir(run.path))
        except OSError:
            continue
        end = frm = last = 0
        versioned = foreign = False
        for p in inner:
            try:
                if p.name in staging.MARK_DIRS and p.is_dir(follow_symlinks=False) and not p.is_junction():
                    b = birth_of(p.stat(follow_symlinks=False)) or -1
                    if p.name == staging.END_NAME:
                        end = b
                    else:
                        frm = b
                elif p.name == staging.ALIVE_NAME and p.is_file(follow_symlinks=False):
                    st = p.stat(follow_symlinks=False)
                    if st.st_size != 0:
                        foreign = True
                    versioned = True
                    last = max(last, int(st.st_mtime_ns))
                elif staging.PART_RE.match(p.name) and p.is_file(follow_symlinks=False):
                    st = p.stat(follow_symlinks=False)
                    last = max(last, birth_of(st), int(st.st_mtime_ns))
                else:
                    foreign = True
            except OSError:
                foreign = True
        if foreign:
            continue
        out.append(Marker(run.path, birth, end, frm, last if end == 0 else 0, versioned))
    out.sort(key=lambda m: m.birth_ns)
    return out


def close_markers(markers: Sequence[Marker]) -> list[Marker]:
    """引き継いだ印に `end` のフォルダを作り、その作成日時を終わりにする(この回が書く物を前の回の分に含めない)。
    作れなければ終わりは 0 のまま(この回の始まりで閉じたものとして扱う)。"""
    out: list[Marker] = []
    for m in markers:
        if m.end_ns != 0:
            out.append(m)
            continue
        p = os.path.join(m.path, staging.END_NAME)
        try:
            os.mkdir(p)
        except OSError:
            out.append(m)
            continue
        out.append(dataclasses.replace(m, end_ns=dir_birth(p) or -1))
    return out


@dataclass
class Suspect:
    """計画で「確かめる」ファイルを選ぶ。max_size 未満(fsync していない)で、作成日時が窓に入るもの・今より先のもの。"""

    windows: tuple[tuple[int, int], ...]
    all_small: bool
    max_size: int
    now_ns: int = 0                 # この回の始まり(計画だけの回は 0 = 今より先の判定をしない)
    all_from: int = 0               # すべて確かめるときの、確かめ終えたところ
    reason: str = ""
    clock_marks: tuple[str, ...] = ()   # 時計が戻ったと分かった印
    future_hits: int = field(default=0, compare=False)

    def __call__(self, size: int, birth_ns: int) -> bool:
        if size >= self.max_size:
            return False
        if birth_ns <= 0:
            return True
        if self.now_ns > 0 and birth_ns >= self.now_ns:
            self.future_hits += 1   # 今より先の作成日時: 時計が戻った(前の回の後半かもしれない)
            return True
        if self.all_small:
            return birth_ns >= self.all_from
        return any(lo <= birth_ns < hi for lo, hi in self.windows)


def build_suspect(markers: Sequence[Marker], fs: str, max_size: int, now_birth: int) -> Suspect | None:
    """印から窓を作る。印が無ければ None。now_birth はこの回の一時フォルダの作成日時(計画だけの回は 0)。"""
    if not markers:
        return None
    reason = "" if fs.upper() in BIRTH_FS else R_BIRTH
    clock: list[str] = []

    def worse(r: str) -> None:
        nonlocal reason
        if _RANK[r] > _RANK[reason]:
            reason = r

    windows: list[tuple[int, int]] = []
    for m in markers:
        if not m.versioned:
            worse(R_LEGACY)
        if m.birth_ns <= 0 or m.end_ns < 0 or m.from_ns < 0:
            worse(R_BIRTH)
            continue
        hi = m.end_ns if m.end_ns > 0 else (now_birth if now_birth > 0 else NO_END)
        bad_clock = ((now_birth > 0 and m.birth_ns > now_birth + MARGIN_NS)       # 印がこの回より後
                     or hi < m.birth_ns - MARGIN_NS                              # 終わりが始まりより前(覚えておいた印を含む)
                     or (m.last_ns > 0 and (m.last_ns > hi + MARGIN_NS or m.last_ns < m.birth_ns - MARGIN_NS)))
        if bad_clock:
            worse(R_CLOCK)
            clock.append(m.path)
            continue
        windows.append((max(m.birth_ns - MARGIN_NS, m.from_ns), hi))
    all_from = min(max(m.from_ns, 0) for m in markers)
    return Suspect(tuple(windows), bool(reason), max_size, now_birth, all_from, reason, tuple(clock))


def remember_clock(markers: Sequence[Marker]) -> int:
    """時計が戻った印を覚えておく: `end` の作成日時を始まりの 1 日前にする(次の回も「すべて確かめる」になる)。覚えた数。"""
    n = 0
    for m in markers:
        if m.birth_ns <= 0:
            continue    # 作成日時が読めない印は、何もしなくても次の回に「すべて確かめる」になる
        p = os.path.join(m.path, staging.END_NAME)
        try:
            os.mkdir(p)
        except FileExistsError:
            pass
        except OSError:
            continue
        if set_birth_ns(p, m.birth_ns - DAY_NS):
            n += 1
    return n


def record_progress(markers: Sequence[Marker], upto_ns: int) -> list[Marker]:
    """作成日時が upto_ns より前のファイルは確かめ終えた、と印に書く(`from` のフォルダの作成日時)。
    窓がすべてそれより前の印(もう確かめる物が無い)を返す(呼ぶ側が片づける)。"""
    done: list[Marker] = []
    for m in markers:
        if upto_ns <= max(m.from_ns, 0):
            continue
        p = os.path.join(m.path, staging.FROM_NAME)
        try:
            os.mkdir(p)
        except FileExistsError:
            pass
        except OSError:
            continue
        if set_birth_ns(p, upto_ns) and m.end_ns > 0 and m.end_ns <= upto_ns:
            done.append(m)
    return done


# ------------------------------------------------------------------ 作成日時を変える(トンネリングの手当て・印の部品)
def _filetime(ns: int) -> int:
    return ns // 100 + _EPOCH_AS_FILETIME


def set_birth_ns(path: str, ns: int) -> bool:
    """ファイル・フォルダの作成日時だけを変える(更新日時・アクセス日時は変えない)。Windows 以外・失敗は False。"""
    if sys.platform != "win32" or ns <= 0:
        return False
    from ctypes import wintypes as w

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
    k32.CreateFileW.restype = w.HANDLE
    k32.SetFileTime.argtypes = [w.HANDLE, ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME)]
    k32.SetFileTime.restype = w.BOOL
    k32.CloseHandle.argtypes = [w.HANDLE]
    k32.CloseHandle.restype = w.BOOL
    file_write_attributes = 0x100
    share_all = 0x7
    open_existing = 3
    flags = 0x02000000 | 0x00200000     # FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT
    h = k32.CreateFileW(path, file_write_attributes, share_all, None, open_existing, flags, None)
    if h is None or h == w.HANDLE(-1).value:
        return False
    try:
        v = _filetime(ns)
        ft = w.FILETIME(v & 0xFFFFFFFF, v >> 32)
        return bool(k32.SetFileTime(h, ctypes.byref(ft), None, None))
    finally:
        k32.CloseHandle(h)
