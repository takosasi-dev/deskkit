# ダウンロード完了判定(D-3 / FR-3): 一時拡張子でない → サイズと更新時刻が stable_seconds 不変
# → 共有なしで開ける、の3条件。0バイトのファイルは stable_seconds の2倍待つ。状態はメモリ上だけに持つ。
from __future__ import annotations

import ntpath
from dataclasses import dataclass

from deskkit.modules.dropsort._win32 import ERROR_SUCCESS, FileInfo, Win32Api


@dataclass
class _Track:
    size: int
    mtime: float
    since: float
    lock_fails: int = 0


PENDING = "pending"      # まだ完了していない(再スキャン待ち)
TEMP = "temp"            # 一時拡張子
STABLE = "stable"        # 安定した(排他オープン前)
LOCKED = "locked"        # 排他オープン失敗
LOCKED_GIVEUP = "locked_giveup"  # locked_retry_max 回続いた
COMPLETE = "complete"


class CompletionTracker:
    def __init__(self) -> None:
        self._t: dict[str, _Track] = {}

    @staticmethod
    def is_temp(name: str, temp_extensions: frozenset[str]) -> bool:
        return ntpath.splitext(name)[1].lower() in temp_extensions

    def stability(self, info: FileInfo, now: float, stable_seconds: float) -> tuple[str, float]:
        """(PENDING/STABLE, あと何秒で安定するか)。"""
        key = info.name.lower()
        t = self._t.get(key)
        if t is None or t.size != info.size or t.mtime != info.mtime:
            t = _Track(info.size, info.mtime, now)
            self._t[key] = t
        need = stable_seconds * (2 if info.size == 0 else 1)
        remain = need - (now - t.since)
        if remain > 0:
            return PENDING, remain
        return STABLE, 0.0

    def exclusive(self, api: Win32Api, info: FileInfo, locked_retry_max: int) -> str:
        """共有なしで開けるか(開いてすぐ閉じる。FR-4)。"""
        key = info.name.lower()
        t = self._t.setdefault(key, _Track(info.size, info.mtime, 0.0))
        if api.try_exclusive_open(info.path) == ERROR_SUCCESS:
            t.lock_fails = 0
            return COMPLETE
        t.lock_fails += 1
        return LOCKED_GIVEUP if t.lock_fails >= locked_retry_max else LOCKED

    def forget(self, name: str) -> None:
        self._t.pop(name.lower(), None)

    def prune(self, present: set[str]) -> None:
        for k in [k for k in self._t if k not in present]:
            del self._t[k]
