# launch_app: exe の絶対パスを引数リストで起動する(シェルを通さない。INV-8)。昇格はしない(INV-7)。
# 起動したプロセスは host と切り離す(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP。FR-11)。
# skip_if_running なら同じ exe が動いているとき起動しない。
from __future__ import annotations

import ntpath
import os
import subprocess
from typing import Any

from deskkit.modules.modeshift.actions.common import ExecEnv, PlanEnv
from deskkit.modules.modeshift.model import FAILED, GAME_REASON, OK, SKIPPED, Step
from deskkit.modules.modeshift.system import LaunchResult

ERROR_ELEVATION_REQUIRED = 740   # winerror.h
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


class PopenLauncher:
    """Launcher の実物。subprocess.Popen に引数リストで渡す。"""

    def launch(self, path: str, args: list[str], cwd: str | None) -> LaunchResult:
        try:
            p = subprocess.Popen(  # noqa: S603 - 引数リストで起動。シェルは通さない
                [path, *args],
                cwd=cwd or ntpath.dirname(path) or None,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError as e:
            code = getattr(e, "winerror", None)
            if code == ERROR_ELEVATION_REQUIRED:
                return LaunchResult(False, f"管理者権限が必要なため起動しません(Win32 エラー {code})")
            return LaunchResult(False, f"起動に失敗(Win32 エラー {code if code is not None else type(e).__name__})")
        return LaunchResult(True, f"PID {p.pid}", p.pid)


def plan(a: dict[str, Any], env: PlanEnv) -> Step:
    path: str = a["path"]
    args: list[str] = a.get("args", [])
    running = env.running_same_exe(path)
    current = ("実行中 PID " + ", ".join(map(str, running))) if running else "停止中"
    new = "起動" + (f"(引数 {len(args)} 個: {' '.join(args)})" if args else "")
    params = {"path": path, "args": list(args), "cwd": a.get("cwd"), "skip_if_running": a.get("skip_if_running", True)}
    step = Step(0, "launch_app", path, current, new, True, params=params)
    if env.in_game:
        step.planned, step.reason = False, GAME_REASON
    elif params["skip_if_running"] and running:
        step.planned, step.reason = False, "既に起動済み"
    elif not os.path.isfile(path):
        step.planned, step.reason = False, "exe が見つからない"
    return step


def run(step: Step, env: ExecEnv) -> tuple[str, str]:
    p = step.params
    if env.in_game:
        return SKIPPED, GAME_REASON
    if p.get("skip_if_running", True):
        name = ntpath.basename(p["path"]).lower()
        for pid in env.pids_of(name):
            ep = env.backends.processes.exe_path(pid)
            if ep is None or ntpath.normcase(ntpath.normpath(ep)) == ntpath.normcase(ntpath.normpath(p["path"])):
                return SKIPPED, "既に起動済み"
    r = env.backends.launcher.launch(p["path"], list(p.get("args", [])), p.get("cwd"))
    if r.ok:
        env.last_launch_ok = True
        return OK, r.reason
    return FAILED, r.reason
