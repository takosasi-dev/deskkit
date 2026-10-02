# 操作記録 `ops.jsonl`(§9)。1行1回で、件数・バイト数・時間・理由コード・結果コードだけを書く。
# 値は型と決まった語で確かめ、ファイル名・パス・ラベル・ドライブ文字・PC の名前が入る余地を作らない(INV-4)。usage() の日ごとの集計も持つ。
from __future__ import annotations

import datetime as _dt
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from deskkit.modules.plugsave.planner import REASONS

TRIGGERS = ("auto", "manual", "first")
RESULTS = ("ok", "cancelled", "removed", "no_space", "error")
FS_KINDS = ("NTFS", "exFAT", "FAT32", "other")
_MAX_BYTES = 2 * 1024 * 1024
_KEEP_LINES = 5_000


def _int(v: Any, name: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        raise TypeError(f"ops の値は 0 以上の整数だけ: {name}")
    return v


class OpsLog:
    def __init__(self, path: Path, now: Callable[[], _dt.datetime] | None = None) -> None:
        self.path = path
        self._now = now or (lambda: _dt.datetime.now().astimezone())

    def write(self, *, trigger: str, result: str, drive_slot: int, fs: str, new: int, changed: int, unchanged: int,
              skipped: Mapping[str, int], failed: int, bytes: int, ms: int, prev_unfinished: bool = False,
              verified: int = 0, recopied: int = 0, verify_left: int = 0) -> dict[str, Any]:
        """prev_unfinished 以下は v0.4.1(前の回が途中で止まっていたときの手当て。件数と真偽だけ)。"""
        if trigger not in TRIGGERS or result not in RESULTS or fs not in FS_KINDS or drive_slot not in (1, 2, 3):
            raise ValueError("ops の語が決まりと違う")
        if not isinstance(prev_unfinished, bool):
            raise TypeError("ops の prev_unfinished は真偽だけ")
        sk: dict[str, int] = {}
        for k, v in skipped.items():
            if k not in REASONS:
                raise ValueError("ops の理由コードが決まりと違う")
            if v:
                sk[k] = _int(v, k)
        rec: dict[str, Any] = {
            "ts": self._now().astimezone().isoformat(timespec="seconds"), "trigger": trigger, "result": result,
            "drive_slot": drive_slot, "fs": fs, "new": _int(new, "new"), "changed": _int(changed, "changed"),
            "unchanged": _int(unchanged, "unchanged"), "skipped": sk, "failed": _int(failed, "failed"),
            "bytes": _int(bytes, "bytes"), "ms": _int(ms, "ms"), "prev_unfinished": prev_unfinished,
            "verified": _int(verified, "verified"), "recopied": _int(recopied, "recopied"),
            "verify_left": _int(verify_left, "verify_left"),
        }
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._trim()
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass  # 記録が書けなくても本処理は止めない
        return rec

    def _trim(self) -> None:
        """大きくなりすぎたら、新しい方の行だけを残して書き直す(DeskKit 自身の記録。消さずにその場で縮める)。"""
        try:
            if not self.path.exists() or self.path.stat().st_size <= _MAX_BYTES:
                return
            lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)[-_KEEP_LINES:]
            with open(self.path, "w", encoding="utf-8") as f:
                f.writelines(lines)
        except OSError:
            pass

    def rows(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return out
        for line in text.splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out

    def per_day(self, days: int, pick: Callable[[dict[str, Any]], int], today: _dt.date | None = None) -> list[int]:
        """日ごとの合計(pick が行ごとの数を返す)。古い日 → 今日。"""
        from deskkit.usage import day_index

        idx = day_index(days, today)
        out = [0] * days
        for r in self.rows():
            try:
                d = _dt.datetime.fromisoformat(str(r.get("ts", ""))).date()
            except ValueError:
                continue
            i = idx.get(d)
            if i is None:
                continue
            try:
                out[i] += int(pick(r))
            except (TypeError, ValueError):
                continue
        return out
