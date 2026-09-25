# 操作記録 ops.jsonl(仕様書 §9。1行1ジョブ)。書くのは種類・プリセット ID・結果コード・バイト数・伏せ字の数・所要時間だけ。
# ファイル名・パス・文字認識で読んだ文字列は受け取る口が無い(INV-5・VINV-4)。ワーカーと GUI の両方から書くので排他する。
from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

KINDS = ("image", "video", "clipboard")
RESULTS = ("ok", "too_large", "verify_failed", "cancelled", "error")
_MAX_BYTES = 2 * 1024 * 1024


class OpsLog:
    def __init__(self, path: Path, now: Callable[[], datetime] | None = None) -> None:
        self.path = path
        self._now = now or (lambda: datetime.now().astimezone())
        self._mu = threading.Lock()

    def write(self, *, kind: str, preset: str, result: str, in_bytes: int, out_bytes: int | None,
              redactions: int, ms: int) -> None:
        if kind not in KINDS or result not in RESULTS:
            raise ValueError("ops の kind / result が範囲外")
        if not (preset.isascii() and preset.replace("-", "").isalnum() and len(preset) <= 20):
            raise ValueError("ops の preset は ID だけ")  # 利用者が付けたプリセット名は書かない
        for v in (in_bytes, redactions, ms):
            if not isinstance(v, int) or isinstance(v, bool):
                raise TypeError("ops の数値は int のみ")
        if out_bytes is not None and (not isinstance(out_bytes, int) or isinstance(out_bytes, bool)):
            raise TypeError("ops の out_bytes は int か None")
        rec: dict[str, Any] = {
            "ts": self._now().astimezone().isoformat(timespec="seconds"),
            "kind": kind, "preset": preset, "result": result, "in_bytes": in_bytes,
            "out_bytes": out_bytes, "redactions": redactions, "ms": ms,
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
        """テスト・画面用。古い順。壊れた行は飛ばす。"""
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


def is_prepared(row: dict[str, Any]) -> bool:
    """「整えた件数」に数える行: 成功したファイルのジョブとクリップボード(伏せ字の焼き込み直しの行は数えない)。"""
    if row.get("result") != "ok":
        return False
    if row.get("kind") == "clipboard":
        return True
    return not (row.get("kind") == "image" and int(row.get("redactions") or 0) > 0)


def is_redacted(row: dict[str, Any]) -> bool:
    """「伏せ字を焼き込んだ件数」に数える行。"""
    return row.get("result") == "ok" and int(row.get("redactions") or 0) > 0
