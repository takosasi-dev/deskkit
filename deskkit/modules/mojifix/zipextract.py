# zip の展開(M-9〜M-12・FR-21〜FR-24)。ZipFile.open() で1項目ずつ読み、~mojifix-<16 進 8 桁>.part に書いてから
# 決めた名前へ os.rename する。extract / extractall は使わない。上限・空き容量・Zone.Identifier の引き継ぎもここで行う。
# 中止・空き不足・上限の超過では、その回に作ったものを全部消す(FR-4)。書くのは展開先の中だけ(INV-3)。
from __future__ import annotations

import os
import secrets
import shutil
import time
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from deskkit.modules.mojifix import zipnames as Z
from deskkit.modules.mojifix.owned import OutputError, Owned, is_disk_full, reserve

CHUNK = 1024 * 1024
ZONE_MAX = 4096
SPACE_MARGIN = 200 * 1024 * 1024

MSG_CHANGED = "読み込んだあとに zip が変わりました"
MSG_FAILED = "展開できませんでした"
MSG_DISK_FULL = "空き容量が足りません"
MSG_NAMES = "同じ名前のフォルダが多すぎます"
MSG_ZONE = "この場所では『インターネットから来た』印を付けられませんでした"


class CancelledError(Exception):
    pass


class ExtractError(Exception):
    def __init__(self, reason: str | None, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass
class ExtractResult:
    dest: Path
    fallback: bool
    extracted: int = 0
    renamed: int = 0
    composed: int = 0
    skipped: dict[str, int] = field(default_factory=dict)       # 画面の理由 → 件数
    zone_failed: bool = False

    @property
    def skipped_total(self) -> int:
        return sum(self.skipped.values())

    def summary(self) -> str:
        s = f"展開 {self.extracted:,} 件・名前を変えた {self.renamed:,} 件・飛ばした {self.skipped_total:,} 件"
        if self.skipped:
            s += "(" + "、".join(f"{k} {v:,} 件" for k, v in self.skipped.items()) + ")"
        return s


def read_zone(zip_path: Path) -> bytes | None:
    """M-11: zip の Zone.Identifier(4KB 以下のときだけ)。無ければ None。中身はログに書かない。"""
    try:
        with open(str(zip_path) + ":Zone.Identifier", "rb") as f:
            data = f.read(ZONE_MAX + 1)
    except OSError:
        return None
    if not data or len(data) > ZONE_MAX:
        return None
    return data


def _free(folder: Path) -> int:
    try:
        return int(shutil.disk_usage(folder).free)
    except OSError:
        return 1 << 62


def space_needed(items: list[Z.PlanItem]) -> int:
    return sum(it.size for it in items if it.status != "skip" and not it.is_dir) + SPACE_MARGIN


def gb_text(n: int) -> str:
    return f"{max(n, 0) / Z.GB:.1f}"


def _mkdirs(dest: Path, rel_parts: list[str], owned: Owned) -> Path:
    cur = dest
    for p in rel_parts:
        cur = cur / p
        try:
            os.mkdir(cur)
            owned.add_dir(cur)
        except FileExistsError:
            if not cur.is_dir():
                raise
    return cur


def _new_part(folder: Path, owned: Owned) -> tuple[Path, BinaryIO]:
    for _ in range(100):
        p = folder / f"~mojifix-{secrets.token_hex(4)}.part"
        try:
            f = open(p, "xb")  # noqa: SIM115 - O_EXCL で新しく作った名前にだけ書く
        except FileExistsError:
            continue
        owned.add_file(p)
        return p, f
    raise ExtractError(None, MSG_FAILED)


def _mtime(dt: tuple[int, int, int, int, int, int]) -> float | None:
    try:
        return time.mktime((*dt, 0, 0, -1))
    except (OverflowError, ValueError):
        return None


def unpack(zip_path: Path, scan: Z.ZipScan, key: str, owned: Owned, fallback: Callable[[], Path], *,
            compose_names: bool = True, skip_mac: bool = True, max_total_gb: int = 20,
            cancel: Callable[[], bool] = lambda: False, progress: Callable[[str, int, int], None] = lambda _s, _d, _t: None,
            free_bytes: Callable[[Path], int] | None = None) -> ExtractResult:
    try:
        st = os.stat(zip_path)
    except OSError:
        raise ExtractError("broken_zip", Z.MSG_BROKEN) from None
    if st.st_size != scan.size or st.st_mtime_ns != scan.mtime_ns:
        raise ExtractError("changed", MSG_CHANGED)
    if Z.bomb_reason(scan, max_total_gb):                          # FR-21
        raise ExtractError("bomb", Z.MSG_BOMB)
    zone = read_zone(zip_path)
    stem = zip_path.stem or "zip"
    try:
        dest, used_fb = reserve("dir", zip_path.parent, stem, "", owned, fallback)   # M-12・M-16
    except OutputError as e:
        raise ExtractError(e.code, MSG_DISK_FULL if e.code == "disk_full" else MSG_NAMES if e.code == "names_exhausted"
                           else MSG_FAILED) from None
    try:
        return _extract_into(zip_path, scan, key, dest, used_fb, zone, owned, compose_names, skip_mac, cancel, progress,
                             free_bytes or _free)
    except BaseException:
        owned.discard()  # FR-4: 中止・空き不足・上限の超過・失敗では、この回に作ったものを後ろから全部消す
        raise


def _extract_into(zip_path: Path, scan: Z.ZipScan, key: str, dest: Path, used_fb: bool, zone: bytes | None, owned: Owned,
                  compose_names: bool, skip_mac: bool, cancel: Callable[[], bool],
                  progress: Callable[[str, int, int], None], free_bytes: Callable[[Path], int]) -> ExtractResult:
    items = Z.plan(scan, key, dest, compose_names=compose_names, skip_mac=skip_mac)
    need = space_needed(items)
    free = free_bytes(dest)
    if free < need:
        raise ExtractError("disk_full", f"空き容量が足りません(あと {gb_text(need - free)} GB 要ります)")
    res = ExtractResult(dest, used_fb)
    for it in items:
        if it.status == "skip":
            res.skipped[it.reason] = res.skipped.get(it.reason, 0) + 1
    declared = sum(it.size for it in items if it.status != "skip" and not it.is_dir)
    written_total = 0
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError, ValueError, UnicodeDecodeError, NotImplementedError):
        raise ExtractError("broken_zip", Z.MSG_BROKEN) from None
    with zf:
        infos = zf.infolist()
        if len(infos) != len(scan.entries):
            raise ExtractError("changed", MSG_CHANGED)
        progress("extract", 0, declared)
        for it in items:
            if cancel():
                raise CancelledError()
            if it.status == "skip" or it.rel is None:
                continue
            parts = it.rel.split("\\")
            try:
                if it.is_dir:
                    _mkdirs(dest, parts, owned)
                    continue
                folder = _mkdirs(dest, parts[:-1], owned)
            except OSError as e:
                if is_disk_full(e):
                    raise ExtractError("disk_full", MSG_DISK_FULL) from None
                res.skipped[Z.SKIP_CLASH[1]] = res.skipped.get(Z.SKIP_CLASH[1], 0) + 1
                continue
            info = infos[it.index]
            try:
                part, out = _new_part(folder, owned)
            except OSError as e:
                raise ExtractError("disk_full" if is_disk_full(e) else None,
                                   MSG_DISK_FULL if is_disk_full(e) else MSG_FAILED) from None
            written = 0
            broken = False
            try:
                with out:
                    try:
                        with zf.open(info) as src:
                            while True:
                                if cancel():
                                    raise CancelledError()
                                data = src.read(CHUNK)
                                if not data:
                                    break
                                written += len(data)
                                written_total += len(data)
                                if written > info.file_size or written_total > declared:   # M-10: 宣言より多く出たら止める
                                    raise ExtractError("bomb", Z.MSG_BOMB)
                                out.write(data)
                                progress("extract", written_total, declared)
                    except (zipfile.BadZipFile, zlib.error, EOFError):
                        broken = True
            except OSError as e:
                if is_disk_full(e):
                    raise ExtractError("disk_full", MSG_DISK_FULL) from None
                raise ExtractError(None, MSG_FAILED) from None
            if broken:                                                                     # FR-23
                owned.discard_one(part)
                res.skipped[Z.SKIP_BROKEN[1]] = res.skipped.get(Z.SKIP_BROKEN[1], 0) + 1
                continue
            if zone is not None and not res.zone_failed:
                try:
                    with open(str(part) + ":Zone.Identifier", "wb") as zf_ads:   # 自分が作った .part の印(M-11)
                        zf_ads.write(zone)
                except OSError:
                    res.zone_failed = True  # FAT32・exFAT など(FR-22)
            target = folder / parts[-1]
            try:
                os.rename(part, target)
            except FileExistsError:
                owned.discard_one(part)
                res.skipped[Z.SKIP_EXISTS[1]] = res.skipped.get(Z.SKIP_EXISTS[1], 0) + 1
                continue
            except OSError as e:
                raise ExtractError("disk_full" if is_disk_full(e) else None,
                                   MSG_DISK_FULL if is_disk_full(e) else MSG_FAILED) from None
            owned.renamed(part, target)
            mt = _mtime(scan.entries[it.index].date_time)
            if mt is not None:
                try:
                    os.utime(target, (mt, mt))
                except (OSError, ValueError, OverflowError):
                    pass
            res.extracted += 1
            if it.status == "rename":
                res.renamed += 1
            if it.composed:
                res.composed += 1
    return res
