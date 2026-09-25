# 診断の履歴 history.jsonl(時刻・カテゴリ・チェックごとの status・所要時間だけ。最新 200 行。P-5)と、
# 操作記録 ops.jsonl(ごみ箱へ送った件数・送れなかった件数・バイト数だけ。§9)。
# 書く前に中身を型と値の一覧で絞る(プロセス名・パス・SSID が紛れ込まないように。INV-3)。
from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from deskkit.modules.pccheckup.checks.base import SEVERITY, STATUS_TEXT

MAX_LINES = 200
CATEGORIES = ("perf", "net", "storage")


def _now() -> datetime:
    return datetime.now().astimezone()


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    v = json.loads(line)
                except ValueError:
                    continue
                if isinstance(v, dict):
                    out.append(v)
    except OSError:
        return out
    return out


class History:
    def __init__(self, path: Path, valid_ids: tuple[str, ...], now: Callable[[], datetime] = _now) -> None:
        self.path = path
        self._ids = frozenset(valid_ids)
        self._now = now
        self._mu = threading.Lock()

    def append(self, category: str, results: dict[str, str], ms: int) -> dict[str, Any]:
        if category not in CATEGORIES:
            raise ValueError("category")
        clean = {k: v for k, v in results.items() if k in self._ids and v in STATUS_TEXT}
        row = {"ts": self._now().isoformat(timespec="seconds"), "category": category, "results": clean, "ms": int(ms)}
        with self._mu:
            rows = _read_rows(self.path)
            rows.append(row)
            rows = rows[-MAX_LINES:]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)  # 自分の履歴ファイルの差し替え(利用者のファイルは触らない)
        return row

    def entries(self) -> list[dict[str, Any]]:
        """古い順。形の合わない行は飛ばす。"""
        out = []
        for r in _read_rows(self.path):
            if r.get("category") in CATEGORIES and isinstance(r.get("results"), dict) and isinstance(r.get("ts"), str):
                out.append(r)
        return out

    def last(self, category: str) -> dict[str, str] | None:
        for r in reversed(self.entries()):
            if r.get("category") == category:
                res = r.get("results")
                if isinstance(res, dict):
                    return {str(k): str(v) for k, v in res.items()}
        return None


def worsened(prev: dict[str, str] | None, check_id: str, status: str) -> bool:
    """FR-7: 前回より悪くなったか。今回が warn / bad で、前回の重さより大きいときだけ(unknown は比べない)。"""
    if not prev or status not in ("warn", "bad"):
        return False
    before = prev.get(check_id)
    if before not in SEVERITY:
        return False
    return SEVERITY[status] > SEVERITY[str(before)]


class OpsLog:
    """ops.jsonl: {"ts", "op": "recycle_temp", "sent", "skipped", "bytes"}。数だけを書く。"""

    def __init__(self, path: Path, now: Callable[[], datetime] = _now) -> None:
        self.path = path
        self._now = now
        self._mu = threading.Lock()

    def recycle_temp(self, sent: int, skipped: int, nbytes: int) -> dict[str, Any]:
        row = {"ts": self._now().isoformat(timespec="seconds"), "op": "recycle_temp",
               "sent": int(sent), "skipped": int(skipped), "bytes": int(nbytes)}
        with self._mu:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row
