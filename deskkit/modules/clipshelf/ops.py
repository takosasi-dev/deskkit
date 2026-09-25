# 操作ログ ops.jsonl(§9.5)。自動処理と明示操作を1行1件の JSON で追記する。
# 書けるのは操作名・理由コード・項目 ID・件数・真偽値だけ。本文・文字数・ハッシュは書かない(INV-1)。
from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

_MAX_BYTES = 2 * 1024 * 1024
OpValue = str | int | bool | None


class OpsLog:
    def __init__(self, path: Path, now: Callable[[], datetime] | None = None) -> None:
        self.path = path
        self._now = now or (lambda: datetime.now().astimezone())

    def write(self, op: str, **fields: OpValue) -> None:
        for k, v in fields.items():
            if not isinstance(v, (str, int, bool)) and v is not None:
                raise TypeError(f"ops の値は str/int/bool のみ: {k}")
        rec: dict[str, OpValue] = {"ts": self._now().astimezone().isoformat(timespec="seconds"), "op": op}
        rec.update(fields)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > _MAX_BYTES:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass  # 操作ログが書けなくても本処理は止めない

    def read(self) -> list[dict[str, OpValue]]:
        """テスト・画面用。壊れた行は飛ばす。"""
        if not self.path.exists():
            return []
        out: list[dict[str, OpValue]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out
