# モード定義+現在状態の読み取り → Plan(Step の列)。状態は一切変えない(FR-6)。
# 3つの入口(トレイ・ホットキー・CLI)とプレビュー・dry-run はすべてこの build_plan の出力を使う(D-2 / FR-5)。
# ゲーム中はフォーカスに作用する Step を「スキップ: ゲーム中」にする(D-10)。
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from deskkit.modules.modeshift.actions import audio, close, launch, layout, power, theme
from deskkit.modules.modeshift.actions import open as opn
from deskkit.modules.modeshift.actions.common import PlanEnv
from deskkit.modules.modeshift.config import ModeDef
from deskkit.modules.modeshift.model import Plan, Step, new_run_id
from deskkit.modules.modeshift.system import Backends

PLANNERS: dict[str, Callable[[dict[str, Any], PlanEnv], Step]] = {
    "launch_app": launch.plan,
    "close_app": close.plan,
    "power_plan": power.plan,
    "master_volume": audio.plan_master,
    "app_volume": audio.plan_app,
    "open_path": opn.plan_path,
    "open_url": opn.plan_url,
    "layout_apply": layout.plan,
    "mic_volume": audio.plan_mic,
    "theme": theme.plan,
}


def build_plan(mode: ModeDef, backends: Backends, *, game_processes: Iterable[str], in_game: bool,
               source: str, dry_run: bool, run_id: str | None = None) -> Plan:
    env = PlanEnv(backends, frozenset(g.lower() for g in game_processes), in_game)
    steps: list[Step] = []
    for i, a in enumerate(mode.actions, 1):
        t = str(a.get("type"))
        try:
            st = PLANNERS[t](a, env)
        except Exception as e:  # noqa: BLE001 - 1件の読み取り失敗で計画全体を止めない
            st = Step(0, t, "?", "読めない", "?", False, f"計画できない({type(e).__name__})")
        st.index = i
        steps.append(st)
    return Plan(
        run_id=run_id or new_run_id(), kind="switch", mode=mode.name, label=mode.label, source=source,
        dry_run=dry_run, steps=steps, def_hash=mode.def_hash, needs_confirmation=not mode.confirmed, in_game=in_game,
    )
