# 操作記録 ops.jsonl(結果コードと所要時間だけ。本文・パス・ファイル名は書かない。J-15・INV-3)。
# 1行1件: {"ts", "event", "reason", "retries", "ms"}。起動時に 90 日より古い行を消す(FR-20。一時ファイル + os.replace)。
from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from deskkit.modules.jotdrop.target import REASONS

EVENTS = ("sent", "pending", "retry_ok", "undo", "undo_refused", "blocked", "missing")
KEEP_DAYS = 90


def _now() -> datetime:
    return datetime.now().astimezone()


class OpLog:
    def __init__(self, path: Path, now: Callable[[], datetime] = _now) -> None:
        self.path = path
        self._now = now
        self._mu = threading.Lock()

    def write(self, event: str, reason: str | None = None, retries: int = 0, ms: int = 0) -> dict[str, Any]:
        if event not in EVENTS:
            raise ValueError("event")
        r = reason if reason in REASONS or reason in ("restore_refused", "busy", "changed", "failed") else None
        row = {"ts": self._now().isoformat(timespec="seconds"), "event": event, "reason": r,
               "retries": int(retries), "ms": int(ms)}
        with self._mu:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def prune(self) -> int:
        """90 日より古い行と、読めない行を消す。消した行の数。"""
        cutoff = self._now() - timedelta(days=KEEP_DAYS)
        with self._mu:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return 0
            keep: list[str] = []
            for ln in lines:
                try:
                    v = json.loads(ln)
                    ts = datetime.fromisoformat(str(v["ts"]))
                except (ValueError, KeyError, TypeError):
                    continue
                if ts.tzinfo is None:
                    ts = ts.astimezone()
                if ts >= cutoff:
                    keep.append(ln)
            removed = len(lines) - len(keep)
            if removed:
                tmp = self.path.with_name(self.path.name + ".tmp")
                with tmp.open("w", encoding="utf-8", newline="\n") as f:
                    f.write("".join(k + "\n" for k in keep))
                os.replace(tmp, self.path)
            return removed

    def rows(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        try:
            for ln in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    v = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(v, dict):
                    out.append(v)
        except OSError:
            pass
        return out
