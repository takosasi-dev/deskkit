# close_app: 対象 exe のプロセスが持つ可視トップレベルウィンドウに WM_CLOSE を投げ、timeout_s まで待つだけ(D-4)。
# 終わらなければ still_running。game_processes の exe には何も送らない(INV-2)。保存確認ダイアログには触らない。
# 強制終了は FR-14 の条件(許可設定+Step 指定+対話的な入口+確認ダイアログ承認)がそろったときだけ。
from __future__ import annotations

from typing import Any

from deskkit.modules.modeshift.actions.common import ExecEnv, PlanEnv
from deskkit.modules.modeshift.model import GAME_REASON, OK, SKIPPED, STILL_RUNNING, Step


def plan(a: dict[str, Any], env: PlanEnv) -> Step:
    exe: str = a["exe"]
    timeout = float(a.get("timeout_s", 10))
    pids = env.pids_of(exe)
    current = ("実行中 PID " + ", ".join(map(str, pids))) if pids else "停止中"
    new = f"終了を要求(WM_CLOSE → {timeout:g} 秒待つ)"
    if a.get("force_on_timeout"):
        new += "・応答なしなら確認して強制終了"
    params = {"exe": exe, "pids": pids, "timeout_s": timeout, "force_on_timeout": bool(a.get("force_on_timeout", False))}
    step = Step(0, "close_app", exe, current, new, True, params=params)
    if exe in env.game_processes:
        step.planned, step.reason = False, "ゲームの exe は対象外"
    elif env.in_game:
        step.planned, step.reason = False, GAME_REASON
    elif not pids:
        step.planned, step.reason = False, "動作していない"
    return step


def run(step: Step, env: ExecEnv) -> tuple[str, str]:
    p = step.params
    exe: str = p["exe"]
    if exe in env.game_processes:
        return SKIPPED, "ゲームの exe は対象外"
    if env.in_game:
        return SKIPPED, GAME_REASON
    # Plan に列挙した PID のうち、今も同じ exe で動いているものだけ(新しく増えたプロセスには触らない。INV-3)
    now = set(env.pids_of(exe))
    targets = {pid for pid in p.get("pids", []) if pid in now}
    if not targets:
        return SKIPPED, "既に終了していた"
    req = env.backends.processes.post_close(targets)
    remaining = env.backends.processes.wait_exit(targets, float(p.get("timeout_s", 10)), env.abort)
    if not remaining:
        return OK, f"{len(targets)} プロセスが終了"
    if env.abort.is_set():
        return STILL_RUNNING, "中断のため待機をやめた"
    # FR-14: 条件がそろったときだけ、PID ごとに確認して承認されたものだけ強制終了する
    if p.get("force_on_timeout") and env.confirm_force is not None:
        killed: list[int] = []
        for pid in sorted(remaining):
            if env.abort.is_set():
                break
            if pid not in set(env.pids_of(exe)):
                killed.append(pid)
                continue
            if env.confirm_force(exe, pid):
                ok, _why = env.backends.processes.force_terminate_confirmed(pid)
                if ok:
                    killed.append(pid)
        remaining -= set(killed)
        if not remaining:
            return OK, "応答がなかったプロセスを確認のうえ強制終了"
    pid_txt = ", ".join(map(str, sorted(remaining)))
    if req.posted == 0 and req.denied == 0:
        why = "閉じられるウィンドウが無い(トレイに残っている可能性)"
    elif req.denied:
        why = "WM_CLOSE が届かない(昇格プロセスの可能性)"
    else:
        why = "終了しなかった(トレイに残った/保存確認が出ている可能性)"
    return STILL_RUNNING, f"{why}(PID {pid_txt})"
