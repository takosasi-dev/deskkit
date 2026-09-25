# layout_apply: ctx.emit("layout.apply", payload) を呼ぶだけ。結果は待たず「送信のみ」と記録する(FR-18)。
# ペイロードは 00_共通 §9.4 の形(+ 契約 v0.2 の任意の preset)。直前の Step でアプリを起動できたときだけ wait_s に定義の待ち秒数を入れる。
# LayoutKeep が有効かどうかは調べない(他モジュールを見ない。INV-5)。
from __future__ import annotations

from typing import Any

from deskkit.modules.modeshift.actions.common import ExecEnv, PlanEnv
from deskkit.modules.modeshift.model import OK, Step


def plan(a: dict[str, Any], env: PlanEnv) -> Step:
    layout = a.get("layout")
    preset = a.get("preset")
    wait = float(a.get("wait_s", 0))
    new = "layout.apply を送信" + (f"(直前に起動したら {wait:g} 秒待ってもらう)" if wait else "")
    target = layout or "(既定のレイアウト)"
    if preset:
        target += f" / プリセット「{preset}」"
    params: dict[str, Any] = {"layout": layout, "wait_s": wait}
    if preset:
        params["preset"] = preset
    return Step(0, "layout_apply", target, "—", new, True, params=params)


def payload(step: Step, run_id: str, prev_launched: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "layout": step.params.get("layout"),
        "source": "modeshift",
        "request_id": f"{run_id}-{step.index}",
        "wait_s": float(step.params.get("wait_s", 0)) if prev_launched else 0,
    }
    if step.params.get("preset"):
        out["preset"] = step.params["preset"]   # 契約 §2: 任意。受けた LayoutKeep は構成シグネチャのプリセット名で探す
    return out


def run(step: Step, env: ExecEnv) -> tuple[str, str]:
    env.emit("layout.apply", payload(step, env.run_id, env.last_launch_ok))
    return OK, "送信のみ"
