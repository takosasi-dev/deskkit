# 操作記録 ops.jsonl(§9)。1行1件で {"ts", "op": "scan"|"recycle", "files", "groups", "sent", "skipped", "bytes", "ms"} を追記する。
# 書けるのは件数・バイト数・時間(整数)だけ。ファイル名・パスは型の上で書けない(INV-4)。usage() 用の日ごとの集計も持つ。
from __future__ import annotations

import datetime as _dt
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

_MAX_BYTES = 2 * 1024 * 1024
FIELDS = ("files", "groups", "sent", "skipped", "bytes", "ms")


class OpsLog:
    def __init__(self, path: Path, now: Callable[[], _dt.datetime] | None = None) -> None:
        self.path = path
        self._now = now or (lambda: _dt.datetime.now().astimezone())

    def write(self, op: str, *, files: int = 0, groups: int = 0, sent: int = 0, skipped: int = 0, bytes: int = 0,
              ms: int = 0) -> None:
        if op not in ("scan", "recycle"):
            raise ValueError("op は scan / recycle だけ")
        vals = {"files": files, "groups": groups, "sent": sent, "skipped": skipped, "bytes": bytes, "ms": ms}
        for k, v in vals.items():
            if not isinstance(v, int) or isinstance(v, bool):
                raise TypeError(f"ops の値は整数だけ: {k}")
        rec: dict[str, Any] = {"ts": self._now().astimezone().isoformat(timespec="seconds"), "op": op, **vals}
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > _MAX_BYTES:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass  # 記録が書けなくても本処理は止めない

    def rows(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in (self.path.with_name(self.path.name + ".1"), self.path):
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
        return out

    def per_day(self, days: int, op: str, value: str | None, today: _dt.date | None = None) -> list[int]:
        """日ごとの合計(value が None なら行数)。古い日 → 今日。"""
        from deskkit.usage import day_index

        idx = day_index(days, today)
        out = [0] * days
        for r in self.rows():
            if r.get("op") != op:
                continue
            try:
                d = _dt.datetime.fromisoformat(str(r.get("ts", ""))).date()
            except ValueError:
                continue
            i = idx.get(d)
            if i is None:
                continue
            if value is None:
                out[i] += 1
            else:
                v = r.get(value)
                if isinstance(v, int) and not isinstance(v, bool):
                    out[i] += v
        return out
