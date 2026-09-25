# Plan の Step を上から順に1つずつ実行する(D-1: 失敗しても続行)。Plan に無い操作はしない(INV-3)。
# 切替では実行前に snapshot.json を保存し、保存できなければ何もしない(FR-9)。各 Step の結果は ops.jsonl に1行ずつ追記(FR-10)。
# 中断(host 終了)が要求されたら、残りの Step を aborted として記録して抜ける。
from __future__ import annotations

import logging
from collections.abc import Callable

from deskkit.modules.modeshift import undo
from deskkit.modules.modeshift.actions import audio, close, launch, layout, power, theme
from deskkit.modules.modeshift.actions import open as opn
from deskkit.modules.modeshift.actions.common import ExecEnv
from deskkit.modules.modeshift.model import ABORTED, FAILED, OK, SKIPPED, UNDOABLE_TYPES, Plan, Step
from deskkit.modules.modeshift.oplog import OpLog, make_row

log = logging.getLogger("deskkit.modeshift")
Runner = Callable[[Step, ExecEnv], tuple[str, str]]

RUNNERS: dict[str, Runner] = {
    "launch_app": launch.run,
    "close_app": close.run,
    "power_plan": power.run,
    "master_volume": audio.run_master,
    "app_volume": audio.run_app,
    "open_path": opn.run_path,
    "open_url": opn.run_url,
    "layout_apply": layout.run,
    "mic_volume": audio.run_mic,
    "theme": theme.run,
}
UNDO_RUNNERS: dict[str, Runner] = {
    "power_plan": undo.run_power,
    "master_volume": undo.run_master,
    "app_volume": undo.run_app,
    "mic_volume": undo.run_mic,
    "theme": undo.run_theme,
}


def execute(plan: Plan, env: ExecEnv, oplog: OpLog, snapshots: undo.SnapshotStore | None = None, *,
            mode_from: str | None = None, on_step: Callable[[Step], None] | None = None) -> Plan:
    plan.executed = True
    plan.dry_run = False
    runners = UNDO_RUNNERS if plan.kind == "undo" else RUNNERS

    def record(step: Step, result: str, reason: str) -> None:
        step.result, step.result_reason = result, reason
        try:
            oplog.append([make_row(plan, step, result, reason)])
        except OSError as e:
            log.error("ops.jsonl に書けません: %s", e)
        if on_step is not None:
            try:
                on_step(step)
            except Exception:  # noqa: BLE001
                log.exception("on_step で例外")

    if plan.kind == "switch" and snapshots is not None:
        snap = undo.take_snapshot(plan, env.backends, mode_from)
        try:
            snapshots.save(snap)
        except OSError as e:
            log.error("snapshot.json を保存できないため実行しません: %s", e)
            for s in plan.steps:
                record(s, FAILED, "スナップショットを保存できないため実行しない")
            plan.finished = True
            return plan
        env.snapshot = snap

    prev_launched = False
    for step in plan.steps:
        if env.abort.is_set():
            record(step, ABORTED, "中断(DeskKit の終了/停止)")
            continue
        if not step.planned:
            record(step, SKIPPED, step.reason)
            prev_launched = False
            continue
        env.last_launch_ok = prev_launched
        fn = runners.get(step.type)
        if fn is None:
            result, reason = FAILED, "実行できない種別"
        else:
            try:
                result, reason = fn(step, env)
            except Exception as e:  # noqa: BLE001 - 1 Step の例外で残りを止めない(D-1)
                log.exception("Step %d (%s) で例外", step.index, step.type)
                result, reason = FAILED, f"例外 {type(e).__name__}"
        prev_launched = step.type == "launch_app" and result == OK
        if env.snapshot is not None and step.type in UNDOABLE_TYPES and snapshots is not None:
            try:
                snapshots.save(env.snapshot)   # 書いた値を記録(D-6 の比較用)
            except OSError as e:
                log.error("snapshot.json を更新できません: %s", e)
        record(step, result, reason)
    plan.finished = True
    return plan
