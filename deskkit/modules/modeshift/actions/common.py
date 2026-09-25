# 計画時の読み取り環境 PlanEnv(現在状態を1回だけ読んでキャッシュ。状態は変えない)と、
# 実行時の環境 ExecEnv(中断フラグ・イベント送信・強制終了の確認・スナップショットへの記録)。
# どちらも OS には system.Backends 経由でしか触らない。
from __future__ import annotations

import logging
import ntpath
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from deskkit.modules.modeshift.system import Backends, MasterState, PowerScheme, ProcInfo, SessionInfo, ThemeState

log = logging.getLogger("deskkit.modeshift")

_UNSET: Any = object()


def same_path(a: str, b: str) -> bool:
    return ntpath.normcase(ntpath.normpath(a)) == ntpath.normcase(ntpath.normpath(b))


@dataclass
class PlanEnv:
    backends: Backends
    game_processes: frozenset[str]
    in_game: bool
    _procs: Any = _UNSET
    _active: Any = _UNSET
    _schemes: Any = _UNSET
    _master: Any = _UNSET
    _sessions: Any = _UNSET
    _capture: Any = _UNSET
    _theme: Any = _UNSET
    notes: list[str] = field(default_factory=list)

    def procs(self) -> list[ProcInfo]:
        if self._procs is _UNSET:
            try:
                self._procs = list(self.backends.processes.list_processes())
            except Exception as e:  # noqa: BLE001
                log.warning("プロセス一覧を取得できません: %s", type(e).__name__)
                self._procs = []
        return list(self._procs)

    def own_pid(self) -> int:
        return self.backends.processes.own_pid()

    def pids_of(self, exe: str) -> list[int]:
        own = self.own_pid()
        return sorted(p.pid for p in self.procs() if p.exe == exe and p.pid != own)

    def running_same_exe(self, path: str) -> list[int]:
        """path と同じ exe の動作中 PID。パスを読めないプロセスは同名なら同じとみなす(起動を控える側)。"""
        name = ntpath.basename(path).lower()
        out = []
        for pid in self.pids_of(name):
            p = self.backends.processes.exe_path(pid)
            if p is None or same_path(p, path):
                out.append(pid)
        return out

    def power_active(self) -> str | None:
        if self._active is _UNSET:
            try:
                self._active = self.backends.power.get_active()
            except Exception as e:  # noqa: BLE001
                log.warning("電源プランを読めません: %s", type(e).__name__)
                self._active = None
        return self._active  # type: ignore[no-any-return]

    def power_schemes(self) -> list[PowerScheme]:
        if self._schemes is _UNSET:
            try:
                self._schemes = list(self.backends.power.list_schemes())
            except Exception as e:  # noqa: BLE001
                log.warning("電源プラン一覧を読めません: %s", type(e).__name__)
                self._schemes = []
        return list(self._schemes)

    def scheme_name(self, guid: str | None) -> str:
        if guid is None:
            return "不明"
        for s in self.power_schemes():
            if s.guid == guid:
                return f"{s.name} ({guid})" if s.name else guid
        return guid

    def master(self) -> MasterState | None:
        if self._master is _UNSET:
            try:
                self._master = self.backends.audio.get_master()
            except Exception as e:  # noqa: BLE001
                log.warning("マスター音量を読めません: %s", type(e).__name__)
                self._master = None
        return self._master  # type: ignore[no-any-return]

    def capture(self) -> MasterState | None:
        if self._capture is _UNSET:
            try:
                self._capture = self.backends.audio.get_capture()
            except Exception as e:  # noqa: BLE001
                log.warning("マイクの音量を読めません: %s", type(e).__name__)
                self._capture = None
        return self._capture  # type: ignore[no-any-return]

    def theme(self) -> ThemeState | None:
        if self._theme is _UNSET:
            try:
                self._theme = self.backends.theme.get()
            except Exception as e:  # noqa: BLE001
                log.warning("テーマを読めません: %s", type(e).__name__)
                self._theme = None
        return self._theme  # type: ignore[no-any-return]

    def sessions(self) -> list[SessionInfo]:
        if self._sessions is _UNSET:
            try:
                self._sessions = list(self.backends.audio.list_sessions())
            except Exception as e:  # noqa: BLE001
                log.warning("オーディオセッションを読めません: %s", type(e).__name__)
                self._sessions = []
        return list(self._sessions)


@dataclass
class ExecEnv:
    backends: Backends
    game_processes: frozenset[str]
    in_game: bool
    abort: threading.Event
    emit: Callable[[str, dict[str, Any]], None]
    run_id: str
    confirm_force: Callable[[str, int], bool] | None = None   # None なら強制終了の経路自体を使わない(FR-14)
    snapshot: dict[str, Any] | None = None                   # 切替時: 書いた値を記録する先
    last_launch_ok: bool = False

    def pids_of(self, exe: str) -> list[int]:
        own = self.backends.processes.own_pid()
        return sorted(p.pid for p in self.backends.processes.list_processes() if p.exe == exe and p.pid != own)
