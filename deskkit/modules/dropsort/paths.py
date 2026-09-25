# 監視先(ダウンロードフォルダ)の解決と、移動先フォルダの検査(ネットワーク・存在・FS・自分自身)。
# 監視先は SHGetKnownFolderPath(FOLDERID_Downloads) で解決し、既定の場所をコードに書かない(FR-1)。
# 移動先は最終パスを解決してから判定する(INV-11)。
from __future__ import annotations

import ntpath
from dataclasses import dataclass

from deskkit.modules.dropsort._win32 import VolumeInfo, Win32Api


def norm(p: str) -> str:
    return ntpath.normcase(ntpath.normpath(p))


def is_under(child: str, parent: str) -> bool:
    c, p = norm(child), norm(parent)
    return c == p or c.startswith(p.rstrip("\\") + "\\")


def resolve_downloads(api: Win32Api, override: str | None) -> str | None:
    """監視先を返す。解決できない・フォルダでないなら None。"""
    p = override or api.known_folder_downloads()
    if not p:
        return None
    p = ntpath.normpath(p)
    if not api.is_dir(p):
        return None
    return p


def is_unc(p: str) -> bool:
    return p.startswith("\\\\") or p.startswith("//")


@dataclass(frozen=True)
class DestCheck:
    ok: bool
    code: str | None = None       # network_dest / dest_missing / dest_is_downloads / dest_in_archive / dest_unavailable
    message: str | None = None
    final: str | None = None
    volume: VolumeInfo | None = None

    @property
    def unavailable(self) -> bool:
        """一時的に使えない(ドライブ未接続)。ルールは無効化せず今回だけ見送る。"""
        return self.code == "dest_unavailable"


def volume_of(api: Win32Api, path: str) -> VolumeInfo | None:
    root = api.volume_root(path)
    return api.volume_info(root) if root else None


def check_dest(api: Win32Api, dest: str, downloads: str | None, archive_dir_name: str) -> DestCheck:
    """ルールの移動先を検査する(§9.2 の読み込み時の検査 + FR-11)。"""
    d = dest.strip()
    if not d:
        return DestCheck(False, "dest_missing", "移動先が空です")
    if is_unc(d):
        return DestCheck(False, "network_dest", "ネットワーク上のフォルダ(UNC パス)には移動できません")
    if not ntpath.isabs(d) or len(d) < 3 or d[1] != ":":
        return DestCheck(False, "dest_missing", "移動先はドライブ文字から始まる絶対パスにしてください")
    root = d[:3]
    vi_root = api.volume_info(root)
    if vi_root is not None and vi_root.is_remote:
        return DestCheck(False, "network_dest", "ネットワークドライブには移動できません")
    if not api.exists(root):
        return DestCheck(False, "dest_unavailable", f"{root[:2]} ドライブが接続されていません")
    if not api.is_dir(d):
        return DestCheck(False, "dest_missing", "移動先フォルダがありません(自動では作りません)")
    final = api.final_path(d) or ntpath.normpath(d)
    if is_unc(final):
        return DestCheck(False, "network_dest", "移動先の実体がネットワーク上にあります")
    vol = volume_of(api, final)
    if vol is None:
        return DestCheck(False, "dest_unavailable", "移動先のボリューム情報を取得できません")
    if vol.is_remote:
        return DestCheck(False, "network_dest", "移動先の実体がネットワークドライブにあります")
    if downloads:
        dl_final = api.final_path(downloads) or downloads
        if norm(final) == norm(dl_final):
            return DestCheck(False, "dest_is_downloads", "移動先がダウンロードフォルダ自身です")
        if is_under(final, ntpath.join(dl_final, archive_dir_name)):
            return DestCheck(False, "dest_in_archive", "移動先がアーカイブフォルダの中です")
    return DestCheck(True, None, None, final, vol)
