# パレットを開く前の foreground への復帰と、任意の自動貼り付け(FR-12 / FR-20 / FR-21)。
# SendInput は foreground が記録した hwnd と一致し、ゲーム・全画面・昇格(不明を含む)・拒否 exe のどれでもないときだけ(INV-6)。
# 判定結果は ops.jsonl の auto_paste に残す(dry_run は「送るはずだった/送らなかった理由」)。
from __future__ import annotations

import logging
from collections.abc import Callable

from deskkit.foreground import ForegroundInfo
from deskkit.modules.clipshelf._win32 import Win32Api
from deskkit.modules.clipshelf.ops import OpsLog

SENT = "sent"
DRY_RUN = "dry_run"
SKIPPED_GAME = "skipped_game"
SKIPPED_FULLSCREEN = "skipped_fullscreen"
SKIPPED_ELEVATED = "skipped_elevated"
SKIPPED_FOCUS = "skipped_focus_changed"
SKIPPED_DENIED = "skipped_denied"  # auto_paste_deny_exes(§9.5 の例に無いので追加。報告済み)


def check_target(target_hwnd: int, fg: ForegroundInfo, deny_exes: frozenset[str]) -> str | None:
    """送ってよければ None、だめなら skipped_* を返す。判定できない項目は送らない側に倒す。"""
    if fg.is_game:
        return SKIPPED_GAME
    if fg.is_fullscreen is not False:
        return SKIPPED_FULLSCREEN
    if fg.is_elevated is not False:
        return SKIPPED_ELEVATED
    if not target_hwnd or fg.hwnd != target_hwnd:
        return SKIPPED_FOCUS
    if fg.exe and fg.exe.lower() in deny_exes:
        return SKIPPED_DENIED
    return None


class Paster:
    def __init__(self, api: Win32Api, foreground: Callable[[], ForegroundInfo], ops: OpsLog, log: logging.Logger) -> None:
        self._api = api
        self._foreground = foreground
        self._ops = ops
        self._log = log

    def restore(self, target_hwnd: int) -> bool:
        """記録した foreground ウィンドウへ戻す。既に無い・戻せないなら False(呼び出し側が通知する)。"""
        if not target_hwnd or not self._api.is_window(target_hwnd):
            return False
        return self._api.set_foreground_window(target_hwnd)

    def auto_paste(self, mode: str, target_hwnd: int, deny_exes: frozenset[str]) -> str | None:
        """mode: off / dry_run / on。結果コードを返す(off は None)。"""
        if mode not in ("dry_run", "on"):
            return None
        fg = self._foreground()  # 送る直前に取り直す
        skip = check_target(target_hwnd, fg, deny_exes)
        if mode == "dry_run":
            would = skip or SENT
            self._ops.write("auto_paste", result=DRY_RUN, would=would)
            self._log.info("auto_paste dry_run would=%s exe=%s", would, fg.exe or "(不明)")
            return DRY_RUN
        if skip is not None:
            self._ops.write("auto_paste", result=skip)
            self._log.info("auto_paste %s exe=%s", skip, fg.exe or "(不明)")
            return skip
        n = self._api.send_ctrl_v()
        self._ops.write("auto_paste", result=SENT)
        self._log.info("auto_paste sent inputs=%d exe=%s", n, fg.exe or "(不明)")
        return SENT
