# 操作記録 ops.jsonl(仕様書 §9。1行1ジョブ)。書くのは操作の種類・結果・件数・バイト数・段階・所要時間・理由コードだけ。
# ファイル名・パス・PDF の文字・しおりの題・入力欄は受け取る口が無い(INV-5)。ジョブと GUI の両方から書くので排他する。
from __future__ import annotations

import datetime as _dt
import json
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from deskkit.usage import day_index

OPS = ("merge", "split", "organize", "compress")
RESULTS = ("ok", "not_smaller", "cancelled", "verify_failed", "error")
CODES = ("encrypted", "no_pages", "unreadable", "disk_full", "too_many_pages", "too_large", "names_exhausted", "denied",
         "range_out", "cancelled")
LEVELS = ("light", "normal", "strong")
_MAX_BYTES = 2 * 1024 * 1024


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


class OpsLog:
    def __init__(self, path: Path, now: Callable[[], _dt.datetime] | None = None) -> None:
        self.path = path
        self._now = now or (lambda: _dt.datetime.now().astimezone())
        self._mu = threading.Lock()

    def write(self, *, op: str, result: str, inputs: int, images: int, pages: int, outputs: int, in_bytes: int,
              out_bytes: int | None, level: str | None, ms: int, code: str | None) -> None:
        if op not in OPS or result not in RESULTS:
            raise ValueError("ops の op / result が範囲外")
        if code is not None and code not in CODES:
            raise ValueError("ops の code が範囲外")
        if level is not None and level not in LEVELS:
            raise ValueError("ops の level が範囲外")
        for v in (inputs, images, pages, outputs, in_bytes, ms):
            if not _is_int(v):
                raise TypeError("ops の数値は int のみ")
        if out_bytes is not None and not _is_int(out_bytes):
            raise TypeError("ops の out_bytes は int か None")
        rec: dict[str, Any] = {
            "ts": self._now().astimezone().isoformat(timespec="seconds"), "op": op, "result": result,
            "inputs": inputs, "images": images, "pages": pages, "outputs": outputs, "in_bytes": in_bytes,
            "out_bytes": out_bytes, "level": level, "ms": ms, "code": code,
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

    def per_day(self, days: int, value: Callable[[dict[str, Any]], int], today: _dt.date | None = None) -> list[int]:
        """日ごとの value(row) の合計(古い日→今日)。壊れた行は飛ばす。"""
        idx = day_index(days, today or self._now().date())
        out = [0] * days
        for row in self.read():
            try:
                d = _dt.datetime.fromisoformat(str(row.get("ts", ""))).date()
                v = int(value(row))
            except (ValueError, TypeError):
                continue
            i = idx.get(d)
            if i is not None and v:
                out[i] += v
        return out


def made_pdfs(row: dict[str, Any]) -> int:
    """「作った PDF の数」(R-2): 完了したジョブの出力の数。"""
    if row.get("result") != "ok":
        return 0
    v = row.get("outputs")
    return int(v) if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def compressed(row: dict[str, Any]) -> int:
    """「軽くした件数」: 軽くするで出力が残った件数。"""
    return made_pdfs(row) if row.get("op") == "compress" else 0
