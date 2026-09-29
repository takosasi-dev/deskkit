# 「N 日バックアップしていません」の判定(FR-25・B-13)。純粋な関数だけ(時計は呼ぶ側が渡す)。
# 最後の成功は全ドライブのうち最も新しい日。1度も成功していなければ、最初の登録から数える。
from __future__ import annotations

import datetime as _dt
from collections.abc import Mapping, Sequence
from typing import Any

FIRST_CHECK_S = 60                 # 起動の 60 秒後
CHECK_INTERVAL_S = 6 * 60 * 60     # 以後 6 時間ごと
MIN_GAP = _dt.timedelta(hours=24)  # 24 時間に1回まで


def parse_ts(v: Any) -> _dt.datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        d = _dt.datetime.fromisoformat(v)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.astimezone()
    return d


def last_success(drives: Sequence[Mapping[str, Any]]) -> _dt.datetime | None:
    got = [d for d in (parse_ts(x.get("last_success_at")) for x in drives) if d is not None]
    return max(got) if got else None


def base_time(drives: Sequence[Mapping[str, Any]]) -> _dt.datetime | None:
    """数え始め: 最後の成功、無ければ最初の登録。"""
    ls = last_success(drives)
    if ls is not None:
        return ls
    reg = [d for d in (parse_ts(x.get("registered_at")) for x in drives) if d is not None]
    return min(reg) if reg else None


def days_since(t: _dt.datetime | None, now: _dt.datetime) -> int | None:
    if t is None:
        return None
    return max(0, int((now - t).total_seconds() // 86400))


def due(drives: Sequence[Mapping[str, Any]], remind_days: int, last_reminded_at: Any, now: _dt.datetime,
        snoozed: bool) -> int | None:
    """知らせるなら経った日数を、知らせないなら None を返す。"""
    if remind_days < 1 or snoozed or not drives:
        return None
    base = base_time(drives)
    if base is None:
        return None
    if now - base <= _dt.timedelta(days=remind_days):
        return None
    last = parse_ts(last_reminded_at)
    if last is not None and now - last < MIN_GAP:
        return None
    return days_since(base, now)
