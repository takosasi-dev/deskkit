# 記録 known.json(ハッシュと日時と印だけ。W-6)と操作記録 events.jsonl(場所の記号と件数だけ。INV-4)の読み書き。
# 書くのは一時ファイル + os.replace。events.jsonl は最新 2,000 行を残す(自分が作ったデータなので C-8 の例外)。
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from deskkit.modules.startupwatch.diff import FLAGS

VERSION = 1
EVENTS_KEEP = 2000
EVENT_NAMES = ("baseline", "added", "changed", "removed", "ack", "rebaseline", "open")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class KnownStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> tuple[dict[str, Any] | None, bool]:
        """(記録, 壊れていたか)。無ければ (None, False)、読めなければ (None, True)。"""
        if not self.path.exists():
            return None, False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, True
        if not isinstance(data, dict) or data.get("version") != VERSION or not isinstance(data.get("items"), dict):
            return None, True
        items: dict[str, dict[str, Any]] = {}
        for k, v in data["items"].items():
            if not isinstance(k, str) or not _HEX64.match(k) or not isinstance(v, dict):
                return None, True
            if not isinstance(v.get("loc"), str) or not isinstance(v.get("cmd"), str) or v.get("flag") not in FLAGS:
                return None, True
            items[k] = {"loc": v["loc"], "cmd": v["cmd"], "first_seen": str(v.get("first_seen") or ""), "flag": v["flag"]}
        return {"version": VERSION, "baseline_at": str(data.get("baseline_at") or ""), "items": items}, False

    def save(self, record: dict[str, Any]) -> None:
        _atomic_write(self.path, json.dumps(record, ensure_ascii=False, indent=1, sort_keys=True))


class EventLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._lines: int | None = None  # 行数(最初に書くときに数える)

    def append(self, event: str, loc: str | None, n: int, notified: bool = False) -> None:
        if event not in EVENT_NAMES:
            raise ValueError("unknown event")
        row = {"ts": now_iso(), "event": event, "loc": loc, "n": int(n), "notified": bool(notified)}
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self._lines is None:
                self._lines = self._count()
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)
            self._lines += 1
            if self._lines > EVENTS_KEEP:
                self._trim()

    def _count(self) -> int:
        try:
            with self.path.open(encoding="utf-8") as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    def _trim(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError:
            return
        if len(lines) > EVENTS_KEEP:
            _atomic_write(self.path, "".join(lines[-EVENTS_KEEP:]))
        self._lines = min(len(lines), EVENTS_KEEP)
