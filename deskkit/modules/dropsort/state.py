# state.json: 監視先ごとの基準線(名前・サイズ・更新時刻)、初回観測時刻、判定済みの記録、undo 済みの操作 ID。
# 書き込みは一時ファイル + os.replace(同じフォルダ内の自前データのみ)。読み書きは op.lock を持った状態で行う。
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deskkit.modules.dropsort.paths import norm

VERSION = 1


@dataclass
class DirState:
    baseline: dict[str, dict[str, Any]] = field(default_factory=dict)   # 名前(小文字) → {name,size,mtime}
    first_seen: dict[str, float] = field(default_factory=dict)          # 名前(小文字) → 初回観測(epoch)
    handled: dict[str, dict[str, Any]] = field(default_factory=dict)    # 名前(小文字) → {size,mtime,status,...}
    created: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {"baseline": self.baseline, "first_seen": self.first_seen, "handled": self.handled, "created": self.created}

    @staticmethod
    def from_json(d: Any) -> DirState:
        if not isinstance(d, dict):
            return DirState()
        return DirState(dict(d.get("baseline") or {}), {k: float(v) for k, v in (d.get("first_seen") or {}).items()},
                        dict(d.get("handled") or {}), float(d.get("created") or 0.0))

    # ---- 同一性(サイズと更新時刻)
    @staticmethod
    def same(entry: dict[str, Any], size: int, mtime: float) -> bool:
        return int(entry.get("size", -1)) == size and abs(float(entry.get("mtime", -1.0)) - mtime) < 1e-3

    def in_baseline(self, name: str, size: int, mtime: float) -> bool:
        e = self.baseline.get(name.lower())
        return e is not None and self.same(e, size, mtime)

    def handled_for(self, name: str, size: int, mtime: float) -> dict[str, Any] | None:
        key = name.lower()
        e = self.handled.get(key)
        if e is None:
            return None
        if not self.same(e, size, mtime):
            del self.handled[key]
            return None
        return e

    def set_handled(self, name: str, size: int, mtime: float, status: str, **extra: Any) -> None:
        d = {"name": name, "size": size, "mtime": mtime, "status": status}
        d.update(extra)
        self.handled[name.lower()] = d

    def prune(self, present: set[str]) -> None:
        for m in (self.baseline, self.first_seen, self.handled):
            for k in [k for k in m if k not in present]:
                del m[k]


@dataclass
class State:
    dirs: dict[str, DirState] = field(default_factory=dict)
    undone: list[str] = field(default_factory=list)
    undo_skipped: list[str] = field(default_factory=list)

    def dir(self, path: str) -> DirState | None:
        return self.dirs.get(norm(path))

    def new_dir(self, path: str) -> DirState:
        ds = DirState()
        self.dirs[norm(path)] = ds
        return ds


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> State:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return State()
        if not isinstance(raw, dict):
            return State()
        return State(
            {str(k): DirState.from_json(v) for k, v in (raw.get("dirs") or {}).items()},
            [str(x) for x in raw.get("undone") or []],
            [str(x) for x in raw.get("undo_skipped") or []],
        )

    def save(self, st: State) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": VERSION, "dirs": {k: v.to_json() for k, v in st.dirs.items()},
                "undone": st.undone, "undo_skipped": st.undo_skipped}
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
