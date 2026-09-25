# ルールごとの実績(D3): dryrun.jsonl と oplog.jsonl から、直近 N 日の「予定・移動・取り消し・拒否」をルール名で数え、
# 試運転のルールが本番へ切り替えてよさそうか(件数・日数・問題なし)を判定する。切り替えは利用者の操作だけで行う。
# ログが大きくなりうるので、読むのは末尾の一定量だけ。作業スレッドで呼び、結果はモジュール側でキャッシュする。
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from deskkit.modules.dropsort.oplog import tail_jsonl

STATS_DAYS = 14             # 集計する日数
PROMOTE_MIN_PLANNED = 5     # 本番への切り替えを勧める「移動予定」の件数
PROMOTE_MIN_DAYS = 7        # 試運転の記録が始まってからの日数
MAX_READ_BYTES = 16 * 1024 * 1024  # 各ログの末尾から読む上限


@dataclass
class RuleStats:
    planned: int = 0          # 試運転の「移動予定」(would_move)
    refuse_planned: int = 0   # 試運転の「拒否予定」(would_refuse)
    moved: int = 0            # 本番の移動(自動・既存整理)
    undone: int = 0           # そのルールで動いたものを元に戻した件数
    problems: int = 0         # 本番の拒否・失敗(undo の失敗は除く)
    first_dry: float | None = None  # 読んだ範囲で最も古い試運転の記録(日数の判定に使う)


def _ts(r: dict[str, Any]) -> float | None:
    try:
        return datetime.fromisoformat(str(r.get("ts", ""))).timestamp()
    except ValueError:
        return None


def compute(oplog: Path, dryrun: Path, now: float, days: int = STATS_DAYS,
            max_bytes: int = MAX_READ_BYTES) -> dict[str, RuleStats]:
    since = now - days * 86400
    out: dict[str, RuleStats] = {}

    def get(name: Any) -> RuleStats | None:
        if not isinstance(name, str) or not name:
            return None
        return out.setdefault(name, RuleStats())

    for r in tail_jsonl(dryrun, 0, max_bytes=max_bytes):
        s = get(r.get("rule"))
        t = _ts(r)
        if s is None or t is None:
            continue
        op = r.get("op")
        if op in ("would_move", "would_refuse"):
            s.first_dry = t if s.first_dry is None else min(s.first_dry, t)
        if t < since:
            continue
        if op == "would_move":
            s.planned += 1
        elif op == "would_refuse":
            s.refuse_planned += 1
    for r in tail_jsonl(oplog, 0, max_bytes=max_bytes):
        t = _ts(r)
        s = get(r.get("rule"))
        if s is None or t is None or t < since:
            continue
        op = r.get("op")
        if op == "move":
            s.moved += 1
        elif op == "undo":
            s.undone += 1
        elif op in ("refused", "failed") and r.get("source") != "undo":
            s.problems += 1
    return out


def promotion_ready(s: RuleStats | None, now: float) -> bool:
    """試運転のルールを本番へ切り替えてよさそうか: 予定が一定件数以上・一定日数以上・問題なし。"""
    if s is None or s.first_dry is None:
        return False
    return (s.planned >= PROMOTE_MIN_PLANNED and now - s.first_dry >= PROMOTE_MIN_DAYS * 86400
            and s.refuse_planned == 0 and s.problems == 0 and s.undone == 0)


def summary(s: RuleStats | None, apply: bool, days: int = STATS_DAYS) -> str:
    s = s or RuleStats()
    if not apply:
        text = f"試運転 {days}日で {s.planned} 件の予定・拒否予定 {s.refuse_planned} 件"
        if s.undone:
            text += f"・取り消し {s.undone} 件"
        return text
    text = f"本番 {days}日で {s.moved} 件を移動・取り消し {s.undone} 件"
    if s.problems:
        text += f"・拒否/失敗 {s.problems} 件"
    return text


__all__ = ["RuleStats", "compute", "promotion_ready", "summary", "STATS_DAYS", "PROMOTE_MIN_PLANNED",
           "PROMOTE_MIN_DAYS"]
