# ファイルの移動。同一ボリュームは MoveFileExW(上書き・別ボリューム複製のフラグなし。FR-12)、
# 別ボリュームは §9.5 の「コピー → サイズと Zone.Identifier の検証 → 元を削除」(FR-13)。名前衝突は ' (n)' 連番(D-9)。
# MOTW 付きを ADS 非対応の移動先へ移さない(INV-2)。元ファイルを消すのは検証済みコピーがあるときの1か所だけ(INV-4)。
from __future__ import annotations

import logging
import ntpath
from dataclasses import dataclass

from deskkit.modules.dropsort import _win32 as W
from deskkit.modules.dropsort.motw import Motw
from deskkit.modules.dropsort.paths import is_unc, volume_of

FAILED_DIR = "_dropsort_failed"


def suffixed(name: str, n: int) -> str:
    """'a.tar.gz', 1 → 'a.tar (1).gz'(拡張子は最後の '.' 以降のみ)。"""
    if n <= 0:
        return name
    stem, ext = ntpath.splitext(name)
    return f"{stem} ({n}){ext}"


@dataclass(frozen=True)
class Plan:
    ok: bool
    dst: str | None = None
    reason: str | None = None
    dst_fs: str | None = None
    same_volume: bool = True
    final_dir: str | None = None
    first_n: int = 0


@dataclass(frozen=True)
class Outcome:
    kind: str  # moved / refused / failed / skipped
    dst: str | None = None
    reason: str | None = None
    dst_fs: str | None = None
    win_error: int | None = None
    cross_volume: bool = False


def _win_reason(err: int) -> str:
    if err in (W.ERROR_SHARING_VIOLATION, W.ERROR_LOCK_VIOLATION):
        return "locked"
    if err == W.ERROR_FILENAME_EXCED_RANGE:
        return "path_too_long"
    if err == W.ERROR_ACCESS_DENIED:
        return "access_denied"
    return "move_error"


class Mover:
    def __init__(self, api: W.Win32Api, max_suffix: int, log: logging.Logger | None = None) -> None:
        self._api = api
        self.max_suffix = max_suffix
        self._log = log or logging.getLogger("deskkit.dropsort")

    # ---- 計画(dry-run でも使う。ファイルは動かさない)
    def plan(self, src: str, dest_dir: str, motw: Motw, name: str | None = None) -> Plan:
        api = self._api
        if is_unc(dest_dir):
            return Plan(False, reason="network_dest")
        final = api.final_path(dest_dir) if api.is_dir(dest_dir) else None
        if final is None:
            return Plan(False, reason="dest_unavailable")
        if is_unc(final):
            return Plan(False, reason="network_dest")
        vol = volume_of(api, final)
        if vol is None:
            return Plan(False, reason="dest_unavailable")
        if vol.is_remote:
            return Plan(False, reason="network_dest", dst_fs=vol.fs_name)
        if motw.present and not vol.named_streams:
            return Plan(False, reason="motw_unsupported_fs", dst_fs=vol.fs_name)
        svol = volume_of(api, src)
        same = svol is not None and svol.serial == vol.serial and svol.root.lower() == vol.root.lower()
        base = name or ntpath.basename(src)
        for n in range(0, self.max_suffix + 1):
            cand = ntpath.join(final, suffixed(base, n))
            if not api.exists(cand):
                return Plan(True, cand, None, vol.fs_name, same, final, n)
        return Plan(False, reason="suffix_exhausted", dst_fs=vol.fs_name)

    # ---- 実行
    def move(self, src: str, dest_dir: str, motw: Motw, expect_size: int, expect_mtime: float,
             name: str | None = None) -> Outcome:
        p = self.plan(src, dest_dir, motw, name)
        if not p.ok or p.final_dir is None:
            kind = "failed" if p.reason == "dest_unavailable" else "refused"
            return Outcome(kind, None, p.reason, p.dst_fs)
        st = self._api.stat(src)
        if st is None or st.size != expect_size or abs(st.mtime - expect_mtime) > 1e-3:
            return Outcome("skipped", None, "source_changed", p.dst_fs)
        base = name or ntpath.basename(src)
        if p.same_volume:
            out = self._same_volume(src, p, base, motw)
            if out.win_error != W.ERROR_NOT_SAME_DEVICE:
                return out
        return self._cross_volume(src, p, base, motw, expect_size, expect_mtime)

    def _same_volume(self, src: str, p: Plan, base: str, motw: Motw) -> Outcome:
        assert p.final_dir is not None
        for n in range(p.first_n, self.max_suffix + 1):
            cand = ntpath.join(p.final_dir, suffixed(base, n))
            if self._api.exists(cand):
                continue
            err = self._api.MoveFileExW(src, cand)
            if err == W.ERROR_SUCCESS:
                if motw.present and motw.raw is not None:
                    after = self._api.read_zone_identifier(cand)
                    if after.data != motw.raw:
                        self._log.warning("移動後の Zone.Identifier が一致しません: %s", cand)
                return Outcome("moved", cand, None, p.dst_fs)
            if err in W.EXISTS_ERRORS:
                continue  # 探索中に別プロセスが同名を作った → 次の番号(上書きしない)
            if err == W.ERROR_NOT_SAME_DEVICE:
                return Outcome("failed", None, "move_error", p.dst_fs, err)
            return Outcome("failed", None, _win_reason(err), p.dst_fs, err)
        return Outcome("refused", None, "suffix_exhausted", p.dst_fs)

    def _cross_volume(self, src: str, p: Plan, base: str, motw: Motw, size: int, mtime: float) -> Outcome:
        """§9.5: コピー → 検証 → 元を削除。検証に失敗したら元を残し、コピーは _dropsort_failed へ。"""
        assert p.final_dir is not None
        api = self._api
        if motw.present and motw.raw is None:
            # MOTW の中身が読めないと移動先で一致を確かめられない → 動かさない(INV-2 を優先)
            return Outcome("refused", None, "verify_mismatch", p.dst_fs, cross_volume=True)
        copied: str | None = None
        for n in range(p.first_n, self.max_suffix + 1):
            cand = ntpath.join(p.final_dir, suffixed(base, n))
            if api.exists(cand):
                continue
            err = api.CopyFileExW(src, cand)
            if err == W.ERROR_SUCCESS:
                copied = cand
                break
            if err in W.EXISTS_ERRORS:
                continue
            return Outcome("failed", None, _win_reason(err), p.dst_fs, err, True)
        if copied is None:
            return Outcome("refused", None, "suffix_exhausted", p.dst_fs, cross_volume=True)
        cst = api.stat(copied)
        ok = cst is not None and cst.size == size
        if ok and motw.present:
            z = api.read_zone_identifier(copied)
            ok = z.present and z.data is not None and z.data == motw.raw
        sst = api.stat(src)
        if ok and (sst is None or sst.size != size or abs(sst.mtime - mtime) > 1e-3):
            ok = False
        if not ok:
            kept = self._park_failed_copy(copied, p.final_dir)
            return Outcome("failed", kept, "verify_mismatch", p.dst_fs, cross_volume=True)
        # INV-4 の唯一の例外: 検証済みコピーが移動先にあるときだけ元ファイルを消す
        err = api.DeleteFileW(src)
        if err != W.ERROR_SUCCESS:
            return Outcome("failed", copied, "source_delete_failed", p.dst_fs, err, True)
        return Outcome("moved", copied, None, p.dst_fs, cross_volume=True)

    def _park_failed_copy(self, copied: str, final_dir: str) -> str:
        """検証に失敗したコピーを移動先の _dropsort_failed\\ へ改名して残す(削除しない)。"""
        api = self._api
        fdir = ntpath.join(final_dir, FAILED_DIR)
        if not api.is_dir(fdir):
            api.create_directory(fdir)
        name = ntpath.basename(copied)
        for n in range(0, self.max_suffix + 1):
            cand = ntpath.join(fdir, suffixed(name, n))
            if api.exists(cand):
                continue
            err = api.MoveFileExW(copied, cand)
            if err == W.ERROR_SUCCESS:
                return cand
            if err not in W.EXISTS_ERRORS:
                break
        self._log.warning("検証に失敗したコピーを %s へ移せませんでした。移動先に残しています: %s", FAILED_DIR, copied)
        return copied
