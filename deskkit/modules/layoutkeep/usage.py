# 利用状況(MODULE_GUIDE §7)。oplog.jsonl から日ごとの件数だけを数えて UsageSeries にする。
# 中身(タイトル・パス)は扱わない。曖昧スキップには撤退基準 R-4 を hint として付ける。
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

from deskkit.usage import UsageSeries, count_jsonl, day_index

R4_HINT = "R-4: 本番運用2週間で、適用の半分以上が曖昧(M-5)で主要ウィンドウを動かせないなら、同定方式を見直すか撤退"


def _is_apply(row: dict[str, Any]) -> bool:
    return row.get("action") == "apply" and row.get("result") in (None, "applied")


def _sum_ambiguous(path: Path, days: int, today: _dt.date | None = None) -> tuple[list[int], list[int]]:
    """(曖昧でスキップした枚数, 曖昧を含んだ適用の回数) を日ごとに。本番の適用行だけを数える。"""
    idx = day_index(days, today)
    windows = [0] * days
    applies = [0] * days
    if not path.exists():
        return windows, applies
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict) or not _is_apply(row):
                        continue
                    d = _dt.datetime.fromisoformat(str(row.get("ts", ""))).date()
                    sk = row.get("skipped")
                    n = int(sk.get("ambiguous", 0)) if isinstance(sk, dict) else 0
                except (ValueError, TypeError):
                    continue
                i = idx.get(d)
                if i is not None and n > 0:
                    windows[i] += n
                    applies[i] += 1
    except OSError:
        pass
    return windows, applies


def usage_series(oplog: Path, days: int, today: _dt.date | None = None) -> list[UsageSeries]:
    applies = count_jsonl(oplog, days, _is_apply, today=today)
    plans = count_jsonl(oplog, days, lambda r: r.get("action") == "plan", today=today)
    amb_windows, amb_applies = _sum_ambiguous(oplog, days, today)
    return [
        UsageSeries("applies", "配置の復元(本番)", applies, "回", primary=True,
                    extra={"dry_run_plans": plans, "auto": count_jsonl(
                        oplog, days, lambda r: _is_apply(r) and r.get("source") == "auto", today=today)}),
        UsageSeries("plans", "試運転の計画", plans, "回", good_when="neutral"),
        UsageSeries("ambiguous", "曖昧で動かさなかったウィンドウ", amb_windows, "枚", good_when="low", hint=R4_HINT,
                    extra={"applies_with_ambiguous": amb_applies}),
        UsageSeries("saves", "配置の保存", count_jsonl(oplog, days, lambda r: r.get("action") == "save", today=today),
                    "回", good_when="neutral"),
        UsageSeries("suppressed", "ゲーム・全画面などで抑止", count_jsonl(
            oplog, days, lambda r: r.get("action") == "suppressed", today=today), "回", good_when="neutral"),
    ]
