# アーカイブ判定(FR-19 / D-10 / D-11): 「最後に触られた時刻」= max(更新, 作成, 初回観測, 最終アクセス※) が
# idle_days より前のファイルを <ダウンロード>\<dir_name>\YYYY-MM\ へ。YYYY-MM は最後に触られた月。
# ※ 最終アクセス時刻は設定 archive.use_atime が true のときだけ使う(NTFS で更新が止まっていることがあるため)。
from __future__ import annotations

import ntpath
from datetime import datetime

from deskkit.modules.dropsort._win32 import ERROR_ALREADY_EXISTS, ERROR_SUCCESS, FileInfo, Win32Api
from deskkit.modules.dropsort.config import ArchiveCfg


def last_touched(info: FileInfo, first_seen: float | None, use_atime: bool) -> float:
    vals = [info.mtime, info.ctime]
    if first_seen is not None:
        vals.append(first_seen)
    if use_atime:
        vals.append(info.atime)
    return max(vals)


def is_idle(info: FileInfo, first_seen: float | None, cfg: ArchiveCfg, now: float) -> bool:
    return now - last_touched(info, first_seen, cfg.use_atime) >= cfg.idle_days * 86400


def month_dir(downloads: str, cfg: ArchiveCfg, touched: float) -> str:
    return ntpath.join(downloads, cfg.dir_name, datetime.fromtimestamp(touched).strftime("%Y-%m"))


def ensure_dir(api: Win32Api, path: str) -> bool:
    """アーカイブ先(ダウンロードフォルダ配下)だけは自動で作る。"""
    parent = ntpath.dirname(path)
    if not api.is_dir(parent):
        e = api.create_directory(parent)
        if e not in (ERROR_SUCCESS, ERROR_ALREADY_EXISTS):
            return False
    if api.is_dir(path):
        return True
    return api.create_directory(path) in (ERROR_SUCCESS, ERROR_ALREADY_EXISTS)
