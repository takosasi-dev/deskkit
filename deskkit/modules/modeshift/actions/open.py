# open_path / open_url: フォルダ・ファイル、http/https の URL を既定のハンドラで開く(FR-17)。
# os.startfile の既定動詞だけを使い、昇格を求める動詞は使わない(INV-7)。ModeShift 自身は接続しない(INV-9)。
# 実行ファイル・ショートカットは開かない(起動は launch_app の役目。config で弾き、ここでも再確認する)。
from __future__ import annotations

import os
from typing import Any

from deskkit.modules.modeshift.actions.common import ExecEnv, PlanEnv
from deskkit.modules.modeshift.config import EXEC_EXTENSIONS, safe_url, url_ok
from deskkit.modules.modeshift.model import FAILED, GAME_REASON, OK, SKIPPED, Step


class ShellOpener:
    """Opener の実物。"""

    def open_path(self, path: str) -> tuple[bool, str]:
        if os.path.splitext(path)[1].lower() in EXEC_EXTENSIONS:
            return False, "実行ファイルは開かない"
        if not os.path.exists(path):
            return False, "パスが見つからない"
        try:
            os.startfile(path)  # 既定の動詞(open)のみ
        except OSError as e:
            return False, f"開けない(Win32 エラー {getattr(e, 'winerror', None)})"
        return True, "既定のアプリで開いた"

    def open_url(self, url: str) -> tuple[bool, str]:
        if not url_ok(url):
            return False, "http / https 以外は開かない"
        try:
            os.startfile(url)  # 既定のブラウザに渡すだけ
        except OSError as e:
            return False, f"開けない(Win32 エラー {getattr(e, 'winerror', None)})"
        return True, "既定のブラウザに渡した"


def plan_path(a: dict[str, Any], env: PlanEnv) -> Step:
    path: str = a["path"]
    step = Step(0, "open_path", path, "—", "既定のアプリで開く", True, params={"path": path})
    if env.in_game:
        step.planned, step.reason = False, GAME_REASON
    elif not os.path.exists(path):
        step.planned, step.reason = False, "パスが見つからない"
    return step


def plan_url(a: dict[str, Any], env: PlanEnv) -> Step:
    url: str = a["url"]
    step = Step(0, "open_url", safe_url(url), "—", "既定のブラウザで開く", True, params={"url": url})
    if env.in_game:
        step.planned, step.reason = False, GAME_REASON
    elif not url_ok(url):
        step.planned, step.reason = False, "http / https 以外"
    return step


def run_path(step: Step, env: ExecEnv) -> tuple[str, str]:
    if env.in_game:
        return SKIPPED, GAME_REASON
    ok, why = env.backends.opener.open_path(step.params["path"])
    return (OK, why) if ok else (FAILED, why)


def run_url(step: Step, env: ExecEnv) -> tuple[str, str]:
    if env.in_game:
        return SKIPPED, GAME_REASON
    ok, why = env.backends.opener.open_url(step.params["url"])
    return (OK, why) if ok else (FAILED, why)
