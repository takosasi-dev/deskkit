# 操作記録(ops.jsonl)。利用状況(FR-19)のために、日時と種類と件数・所要時間だけを1行ずつ残す。
# 組み合わせの名前・キーの名前・エラー番号は書かない(INV-4)。結果そのものは保存しない(K-8)。
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

KEEP_DAYS = 120   # 利用状況は最長 90 日なので、それより少し長く残す


class OpLog:
    def __init__(self, path: Path, now: Callable[[], datetime], log: logging.Logger) -> None:
        self.path = path
        self._now = now
        self._log = log

    def write(self, event: str, **counts: int) -> None:
        row: dict[str, Any] = {"ts": self._now().isoformat(timespec="seconds"), "event": event}
        row.update({k: int(v) for k, v in counts.items()})
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as e:
            self._log.warning("ops write failed: %s", type(e).__name__)

    def prune(self) -> None:
        """KEEP_DAYS より古い行と壊れた行を落とす(自分のファイルだけ。書き換えは一時ファイルからの置き換え)。"""
        if not self.path.exists():
            return
        limit = self._now() - timedelta(days=KEEP_DAYS)
        keep: list[str] = []
        dropped = 0
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                    ts = datetime.fromisoformat(str(row.get("ts", "")))
                    if ts.tzinfo is not None:
                        ts = ts.replace(tzinfo=None)
                except (ValueError, TypeError, AttributeError):
                    dropped += 1
                    continue
                if ts < limit:
                    dropped += 1
                    continue
                keep.append(line)
            if dropped:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text("".join(s + "\n" for s in keep), encoding="utf-8")
                tmp.replace(self.path)
        except (OSError, UnicodeDecodeError) as e:
            self._log.warning("ops prune failed: %s", type(e).__name__)
