# 操作ログ ops.jsonl(1 Step 1行、追記のみ。C-8 / INV-11)。書き換え・削除はしない。
# 対象は Step.target を書く。ただし open_url はホスト名だけ(利用者の判断 v0.2: ドメインのみ)。ウィンドウタイトルは扱わない。
# 画面の履歴表示用に末尾の数行を読む関数も持つ。
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from deskkit.modules.modeshift.config import url_host
from deskkit.modules.modeshift.model import Plan, Step, now_iso

log = logging.getLogger("deskkit.modeshift")


def log_target(step: Step) -> str:
    """ops.jsonl に書く対象。open_url は URL のホスト名だけ(パス・クエリを残さない)。"""
    if step.type == "open_url":
        return url_host(str(step.params.get("url") or ""))
    return step.target


def make_row(plan: Plan, step: Step, result: str, reason: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ts": now_iso(),
        "run_id": plan.run_id,
        "source": "undo" if plan.kind == "undo" else plan.source,
        "mode": plan.mode,
        "dry_run": plan.dry_run,
        "step": step.index,
        "type": step.type,
        "target": log_target(step),
        "result": result,
        "reason": reason,
    }
    if plan.kind == "undo":
        row["via"] = plan.source
    return row


class OpLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def append(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        text = "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as f:   # 追記モードのみ
                f.write(text)

    def append_dry_run(self, plan: Plan) -> None:
        self.append([make_row(plan, s, "planned" if s.planned else "skipped", s.reason) for s in plan.steps])

    def tail(self, n: int = 200) -> list[dict[str, Any]]:
        """末尾 n 行(新しい順)。壊れた行は飛ばす。"""
        try:
            with open(self.path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 400 * n))
                data = f.read()
        except OSError:
            return []
        lines = data.decode("utf-8", errors="replace").splitlines()
        if size > 400 * n and lines:
            lines = lines[1:]   # 途中から読んだ先頭行は捨てる
        out: list[dict[str, Any]] = []
        for ln in reversed(lines[-n:]):
            try:
                v = json.loads(ln)
            except ValueError:
                continue
            if isinstance(v, dict):
                out.append(v)
        return out
