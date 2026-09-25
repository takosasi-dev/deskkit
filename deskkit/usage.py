# 利用状況ダッシュボードの共通の型。モジュールは任意で Module.usage(days) を実装し、UsageSeries の一覧を返す。
# 値は「日ごとの件数」だけで、本文・パス・タイトルなどの中身は持たない(C-12)。
from __future__ import annotations

import datetime as _dt
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class UsageSeries:
    key: str                  # "switches" など(モジュール内で一意)
    label: str                # 画面に出す名前(例: "モード切替")
    per_day: list[int]        # 古い日 → 今日の順。長さは days
    unit: str = "回"
    primary: bool = False     # そのモジュールの代表指標(カードの大きな数字に使う)
    hint: str | None = None   # 撤退基準などの注記(例: "週5回未満なら撤退を検討(R-4)")
    good_when: str = "high"   # "high" / "low" / "neutral"(色分け用)
    extra: dict[str, Any] = field(default_factory=dict)


def day_index(days: int, today: _dt.date | None = None) -> dict[_dt.date, int]:
    """日付 → per_day の添字。"""
    t = today or _dt.date.today()
    return {t - _dt.timedelta(days=days - 1 - i): i for i in range(days)}


def count_jsonl(path: Path, days: int, pick: Callable[[dict[str, Any]], bool],
                ts_key: str = "ts", today: _dt.date | None = None) -> list[int]:
    """jsonl の各行のうち pick が True のものを、ts(ISO 8601)の日付ごとに数える。壊れた行は飛ばす。"""
    idx = day_index(days, today)
    out = [0] * days
    if not path.exists():
        return out
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict) or not pick(row):
                        continue
                    d = _dt.datetime.fromisoformat(str(row.get(ts_key, ""))).date()
                except (ValueError, TypeError):
                    continue
                i = idx.get(d)
                if i is not None:
                    out[i] += 1
    except OSError:
        pass
    return out


def count_dates(dates: Iterable[_dt.date], days: int, today: _dt.date | None = None) -> list[int]:
    idx = day_index(days, today)
    out = [0] * days
    for d in dates:
        i = idx.get(d)
        if i is not None:
            out[i] += 1
    return out
