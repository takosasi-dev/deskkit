# 古い一時ファイルの一覧化(S3)と、deskkit.fileops.recycle でのごみ箱送り(FR-9)。自分では1件も消さない(VINV-2)。
# 対象は %TEMP% の下の、最終更新から 7 日を超えたファイルだけ。リパースポイントはたどらない(P-6・INV-4)。
# 送る直前にもう一度「%TEMP% の下・リンクでない・7 日を超えた」を確かめる。パスはログ・ops.jsonl に書かない。
from __future__ import annotations

import os
import stat as _stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from deskkit.modules.pccheckup.fswalk import WalkStats, is_reparse, iter_files

if TYPE_CHECKING:
    from deskkit.fileops import RecycleResult

OLD_DAYS = 7
OLD_SECONDS = OLD_DAYS * 86400
CHUNK = 20            # recycle を何件ずつ呼ぶか(終了時に待つ時間を短くするため)
SCAN_LIMIT_S = 60.0   # 「ごみ箱へ」を押したときの一覧化の上限

RecycleFn = Callable[[Sequence[Path], "int | None"], "RecycleResult"]


@dataclass
class TempScan:
    root: str
    files: list[tuple[str, int]] = field(default_factory=list)  # (パス, サイズ)
    bytes: int = 0
    denied: bool = False
    partial: bool = False

    @property
    def count(self) -> int:
        return len(self.files)


@dataclass
class CleanupResult:
    sent: int = 0
    skipped: int = 0          # 使用中・アクセス拒否・中断・対象外になったもの(数だけ)
    bytes: int = 0            # 送ったファイルの合計(ごみ箱を空にすると空く量)
    aborted: bool = False     # 恒久削除の確認で「いいえ」、または終了の合図で止めた
    reasons: dict[str, int] = field(default_factory=dict)


def temp_root() -> str:
    """%TEMP%(無ければ %TMP%)。どちらも無い・フォルダでなければ OSError。"""
    for key in ("TEMP", "TMP"):
        v = os.environ.get(key)
        if v and os.path.isdir(v):
            return os.path.abspath(v)
    raise OSError("TEMP フォルダが見つかりません")


def is_old(mtime: float, now: float) -> bool:
    """最終更新から 7 日を「超えた」か(ちょうど 7 日は含めない)。"""
    return now - mtime > OLD_SECONDS


def scan_old_temp(root: str, now: float, stop: Callable[[], bool]) -> TempScan:
    stats = WalkStats()
    out = TempScan(root=root)
    for f in iter_files(root, stats, stop):
        if is_old(f.mtime, now):
            out.files.append((f.path, f.size))
            out.bytes += f.size
    out.denied, out.partial = stats.denied, stats.partial
    return out


def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p))


def is_under(root: str, path: str) -> bool:
    r, p = _norm(root), _norm(path)
    try:
        return os.path.commonpath([r, p]) == r and p != r
    except ValueError:  # 別ドライブ
        return False


def still_eligible(root: str, path: str, now: float) -> bool:
    """送る直前の確認: %TEMP% の下・途中にリンクが無い・ファイル本体がリンクでない・7 日を超えた。"""
    if not is_under(root, path):
        return False
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    if is_reparse(st) or not _stat.S_ISREG(st.st_mode) or not is_old(st.st_mtime, now):
        return False
    r = _norm(root)
    d = os.path.dirname(os.path.abspath(path))
    while _norm(d) != r:
        try:
            if is_reparse(os.stat(d, follow_symlinks=False)):
                return False
        except OSError:
            return False
        parent = os.path.dirname(d)
        if parent == d:
            return False
        d = parent
    return True


def recycle_old_temp(scan: TempScan, now: float, recycle: RecycleFn, parent_hwnd: int | None,
                     stop: Callable[[], bool]) -> CleanupResult:
    """scan の一覧を CHUNK 件ずつ recycle へ渡す。stop() は chunk の合間にだけ見る(呼び出し中は待つ。§10)。"""
    res = CleanupResult()
    sizes = {p: s for p, s in scan.files}
    pending = [p for p, _ in scan.files]
    i = 0
    while i < len(pending):
        if stop():
            res.aborted = True
            res.skipped += len(pending) - i
            res.reasons["stopped"] = res.reasons.get("stopped", 0) + len(pending) - i
            break
        chunk = pending[i:i + CHUNK]
        i += len(chunk)
        ok = [p for p in chunk if still_eligible(scan.root, p, now)]
        n_bad = len(chunk) - len(ok)
        if n_bad:
            res.skipped += n_bad
            res.reasons["not_eligible"] = res.reasons.get("not_eligible", 0) + n_bad
        if not ok:
            continue
        r = recycle([Path(p) for p in ok], parent_hwnd)
        res.sent += r.sent_count
        res.bytes += sum(sizes.get(str(p), 0) for p in r.sent)
        for _p, reason in r.skipped:
            res.skipped += 1
            res.reasons[reason] = res.reasons.get(reason, 0) + 1
        if r.count("aborted"):
            res.aborted = True
            rest = len(pending) - i
            if rest:
                res.skipped += rest
                res.reasons["aborted"] = res.reasons.get("aborted", 0) + rest
            break
    return res
