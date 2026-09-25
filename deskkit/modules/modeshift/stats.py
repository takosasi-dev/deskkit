# 利用状況(MODULE_GUIDE §7): ops.jsonl から日ごとの件数だけを数える(対象・理由などの中身は返さない)。
# 1回の切替は複数行(1 Step 1行)なので run_id ごとに1件として数える。dry-run は数えない。
# Qt に依存しない。
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from deskkit.usage import UsageSeries, count_jsonl

R2_HINT = "本番運用2週間の切替回数が基準(R-2、回数は未決定)未満なら撤退を検討"


def _once_per_run(cond: Callable[[dict[str, Any]], bool]) -> Callable[[dict[str, Any]], bool]:
    seen: set[str] = set()

    def pick(row: dict[str, Any]) -> bool:
        if row.get("dry_run") is not False or not cond(row):
            return False
        rid = str(row.get("run_id") or "")
        if not rid or rid in seen:
            return False
        seen.add(rid)
        return True

    return pick


def usage_series(ops_path: Path, days: int) -> list[UsageSeries]:
    switches = count_jsonl(ops_path, days, _once_per_run(lambda r: r.get("source") != "undo"))
    undos = count_jsonl(ops_path, days, _once_per_run(lambda r: r.get("source") == "undo"))
    problems = count_jsonl(ops_path, days, _once_per_run(lambda r: r.get("result") in ("failed", "still_running")))
    return [
        UsageSeries("switches", "モード切替", switches, primary=True, hint=R2_HINT),
        UsageSeries("undos", "元に戻す", undos, good_when="neutral"),
        UsageSeries("problems", "失敗・終了しなかった切替", problems, good_when="low"),
    ]
