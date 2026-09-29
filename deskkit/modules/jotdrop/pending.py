# 書けなかったメモの預かり pending.jsonl(本文とパスを持つ唯一のファイル。J-15)。書けたら消す。
# 書き直しは一時ファイル + os.replace(INV-7)。1行1件: {"id", "created", "path", "line", "reason"}。
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from deskkit.modules.jotdrop.target import REASONS


@dataclass(frozen=True)
class PendingItem:
    id: str
    created: str      # ISO 8601(Enter を押した瞬間)
    path: str
    line: str
    reason: str

    def created_dt(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.created)
        except ValueError:
            return None


def _parse(v: object) -> PendingItem | None:
    if not isinstance(v, dict):
        return None
    keys = ("id", "created", "path", "line", "reason")
    if not all(isinstance(v.get(k), str) for k in keys):
        return None
    if v["reason"] not in REASONS:
        return None
    return PendingItem(*(str(v[k]) for k in keys))


class PendingStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._mu = threading.Lock()
        self._items: list[PendingItem] = self._load()

    def _load(self) -> list[PendingItem]:
        out: list[PendingItem] = []
        try:
            with self.path.open(encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        it = _parse(json.loads(raw))
                    except ValueError:
                        continue
                    if it is not None:
                        out.append(it)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        return out

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            for it in self._items:
                f.write(json.dumps(asdict(it), ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)  # 自分の預かりファイルの差し替え(利用者のファイルは触らない)

    def items(self) -> list[PendingItem]:
        with self._mu:
            return list(self._items)

    def count(self) -> int:
        with self._mu:
            return len(self._items)

    def add(self, item: PendingItem) -> None:
        with self._mu:
            self._items = [x for x in self._items if x.id != item.id] + [item]
            self._save()

    def remove(self, item_id: str) -> PendingItem | None:
        with self._mu:
            hit = next((x for x in self._items if x.id == item_id), None)
            if hit is None:
                return None
            self._items = [x for x in self._items if x.id != item_id]
            self._save()
            return hit

    def set_reason(self, item_id: str, reason: str) -> None:
        with self._mu:
            self._items = [PendingItem(x.id, x.created, x.path, x.line, reason) if x.id == item_id else x
                           for x in self._items]
            self._save()

    def get(self, item_id: str) -> PendingItem | None:
        with self._mu:
            return next((x for x in self._items if x.id == item_id), None)
