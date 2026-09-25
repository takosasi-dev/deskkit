# カテゴリごとにチェックを順に実行する。1つのチェックで例外が出ても unknown にして続ける(FR-5)。
# 「中止」は次のチェックから先を行わない(FR-2)。進み具合と結果はコールバックで返す(呼ぶ側がメインスレッドへ渡す)。
# ログにはチェック ID・例外の型名・所要時間だけを書く(例外の文はパスを含みうるので書かない。INV-3)。
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from deskkit.modules.pccheckup.checks import net, perf, storage
from deskkit.modules.pccheckup.checks.base import Cancel, Check, Finding, unknown
from deskkit.modules.pccheckup.probes import Probes

BY_CATEGORY: dict[str, tuple[Check, ...]] = {"perf": perf.CHECKS, "net": net.CHECKS, "storage": storage.CHECKS}
ALL_IDS: tuple[str, ...] = tuple(c.check_id for cs in BY_CATEGORY.values() for c in cs)
TITLES: dict[str, str] = {**perf.TITLES, **net.TITLES, **storage.TITLES}


def category_of(check_id: str) -> str:
    return {"P": "perf", "N": "net", "S": "storage"}.get(check_id[:1], "")


@dataclass
class CategoryResult:
    category: str
    findings: list[Finding] = field(default_factory=list)
    ms: int = 0
    cancelled: bool = False
    failed: list[str] = field(default_factory=list)   # 例外で unknown にしたチェック ID


ProgressFn = Callable[[str, int, int, str], None]      # (カテゴリ, 何番目, 全体, 文)
FindingFn = Callable[[str, Finding], None]


def run_check(chk: Check, probes: Probes, cancel: Cancel, log: logging.Logger) -> tuple[Finding, bool]:
    """(結果, 例外だったか)。"""
    try:
        return chk.run(probes, cancel), False
    except Exception as e:  # noqa: BLE001 - 1つの失敗で全体を止めない(FR-5)
        log.warning("check %s failed: %s", chk.check_id, type(e).__name__)
        return unknown(chk.check_id, TITLES.get(chk.check_id, chk.check_id)), True


def run_category(category: str, probes: Probes, cancel: Cancel, log: logging.Logger, *,
                 on_progress: ProgressFn | None = None, on_finding: FindingFn | None = None,
                 clock: Callable[[], float] = time.monotonic) -> CategoryResult:
    checks = BY_CATEGORY[category]
    res = CategoryResult(category)
    t0 = clock()
    for i, chk in enumerate(checks):
        if cancel.is_set():
            res.cancelled = True
            break
        if on_progress is not None:
            on_progress(category, i, len(checks), chk.progress)
        f, failed = run_check(chk, probes, cancel, log)
        if failed:
            res.failed.append(chk.check_id)
        res.findings.append(f)
        if on_finding is not None:
            on_finding(category, f)
    res.ms = int((clock() - t0) * 1000)
    return res
