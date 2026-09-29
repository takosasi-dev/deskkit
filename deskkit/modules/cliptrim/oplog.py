# 操作記録 ops.jsonl(仕様書 §9。1行1ジョブ)。書くのは切り方・書き出し方・本数・秒・バイト数・結果コード・所要時間だけで、
# ファイル名・パス・ffmpeg の stderr を受け取る口が無い(INV-3)。日ごとの本数を数える関数もここ(FR-18)。
from __future__ import annotations

import datetime as _dt
import json
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from deskkit.usage import day_index

RESULTS = ("ok", "partial", "cancelled", "verify_failed", "no_space", "timeout", "error")
_MAX_BYTES = 2 * 1024 * 1024


class OpsLog:
    def __init__(self, path: Path, now: Callable[[], _dt.datetime] | None = None) -> None:
        self.path = path
        self._now = now or (lambda: _dt.datetime.now().astimezone())
        self._mu = threading.Lock()

    def write(self, *, mode: str, output: str, segments: int, files_ok: int, files_failed: int, src_seconds: float,
              out_seconds: float, out_bytes: int, shift_max_s: float | None, result: str, ms: int) -> None:
        if mode not in ("copy", "precise") or output not in ("separate", "join") or result not in RESULTS:
            raise ValueError("ops の mode / output / result が範囲外")
        for v in (segments, files_ok, files_failed, out_bytes, ms):
            if not isinstance(v, int) or isinstance(v, bool):
                raise TypeError("ops の数値は int のみ")
        rec: dict[str, Any] = {
            "ts": self._now().astimezone().isoformat(timespec="seconds"),
            "mode": mode, "output": output, "segments": segments, "files_ok": files_ok, "files_failed": files_failed,
            "src_seconds": round(float(src_seconds), 3), "out_seconds": round(float(out_seconds), 3),
            "out_bytes": out_bytes, "shift_max_s": None if shift_max_s is None else round(float(shift_max_s), 3),
            "result": result, "ms": ms,
        }
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with self._mu:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > _MAX_BYTES:
                    os.replace(self.path, self.path.with_name(self.path.name + ".1"))
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass  # 操作記録が書けなくても本処理は止めない

    def paths(self) -> list[Path]:
        return [self.path.with_name(self.path.name + ".1"), self.path]

    def read(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in self.paths():
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


def sum_per_day(rows: list[dict[str, Any]], days: int, value: Callable[[dict[str, Any]], int],
                today: _dt.date | None = None) -> list[int]:
    """行ごとの値(本数など)を日ごとに足す(古い日 → 今日)。"""
    idx = day_index(days, today)
    out = [0] * days
    for row in rows:
        try:
            d = _dt.datetime.fromisoformat(str(row.get("ts", ""))).date()
            v = int(value(row))
        except (ValueError, TypeError):
            continue
        i = idx.get(d)
        if i is not None:
            out[i] += v
    return out
