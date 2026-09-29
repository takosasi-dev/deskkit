# EyeBreak のデータ: daily.json(日ごとの回数の整数だけ。120 日より古い日は消す。E-13・FR-22)と
# state.json(「今日は止めている」の期限だけ。FR-7)。書くのは一時ファイル + os.replace。壊れていたら .bad を付けて残す。
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

FIELDS = ("eye", "body", "rested", "later", "mute_today", "dry")
KEEP_DAYS = 120


def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _set_aside(path: Path) -> None:
    bad = path.with_name(path.name + ".bad")
    i = 1
    while bad.exists():
        i += 1
        bad = path.with_name(f"{path.name}.bad{i}")
    try:
        os.replace(path, bad)
    except OSError:
        pass


class Daily:
    def __init__(self, path: Path, log: logging.Logger) -> None:
        self.path = path
        self.log = log
        self.days: dict[str, dict[str, int]] = {}

    def load(self, today: date) -> None:
        self.days = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("not a dict")
                for k, v in raw.items():
                    date.fromisoformat(k)
                    if not isinstance(v, dict):
                        raise ValueError("not a dict")
                    self.days[k] = {f: int(v.get(f, 0)) for f in FIELDS if isinstance(v.get(f, 0), int)}
            except (OSError, ValueError, TypeError):
                self.days = {}
                _set_aside(self.path)
                self.log.warning("daily broken")  # §10: 名前に .bad を付けて残し、新しく作る
        if self.prune(today):
            self.save()

    def prune(self, today: date) -> bool:
        cut = (today - timedelta(days=KEEP_DAYS)).isoformat()
        old = [k for k in self.days if k < cut]
        for k in old:
            del self.days[k]
        return bool(old)

    def add(self, day: date, field: str, n: int = 1) -> None:
        d = self.days.setdefault(day.isoformat(), {})
        d[field] = d.get(field, 0) + n
        self.save()

    def get(self, day: date, field: str) -> int:
        return int(self.days.get(day.isoformat(), {}).get(field, 0))

    def series(self, days: int, today: date, fields: tuple[str, ...]) -> list[int]:
        out: list[int] = []
        for i in range(days - 1, -1, -1):
            d = self.days.get((today - timedelta(days=i)).isoformat(), {})
            out.append(sum(int(d.get(f, 0)) for f in fields))
        return out

    def save(self) -> None:
        try:
            _atomic_write(self.path, self.days)
        except OSError as e:
            self.log.warning("daily write failed: %s", type(e).__name__)


class State:
    def __init__(self, path: Path, log: logging.Logger) -> None:
        self.path = path
        self.log = log

    def load(self) -> datetime | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            v = raw.get("muted_until") if isinstance(raw, dict) else None
            return datetime.fromisoformat(v) if isinstance(v, str) else None
        except (OSError, ValueError):
            _set_aside(self.path)
            self.log.warning("state broken")
            return None

    def save(self, muted_until: datetime | None) -> None:
        try:
            _atomic_write(self.path, {"muted_until": muted_until.isoformat(timespec="seconds") if muted_until else None})
        except OSError as e:
            self.log.warning("state write failed: %s", type(e).__name__)
