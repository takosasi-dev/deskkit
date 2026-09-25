# OS に作用する部分(プロセス・ウィンドウ・起動・電源・音量・既定のハンドラ)の Protocol と値の型。
# 実物(_win32 / actions/*)と偽物(fakes.py)をここで差し替える。planner / executor はこの型だけを見る。
# real_backends() は実物を組み立てる(重い import はここで遅延する)。
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    exe: str  # 小文字のファイル名


@dataclass(frozen=True)
class PowerScheme:
    guid: str        # 小文字
    name: str        # 表示専用(照合には使わない。D-9)
    active: bool


@dataclass(frozen=True)
class MasterState:
    level: float     # スカラー 0.0〜1.0
    mute: bool
    device_id: str | None


@dataclass(frozen=True)
class SessionInfo:
    session_id: str  # IAudioSessionControl2::GetSessionInstanceIdentifier
    pid: int
    level: float
    mute: bool


@dataclass(frozen=True)
class LaunchResult:
    ok: bool
    reason: str
    pid: int | None = None


@dataclass(frozen=True)
class CloseRequest:
    posted: int        # WM_CLOSE を投げたウィンドウ数
    denied: int        # 投げられなかった数(UIPI 等)
    last_error: int = 0


class ProcessApi(Protocol):
    def list_processes(self) -> list[ProcInfo]: ...
    def exe_path(self, pid: int) -> str | None: ...
    def own_pid(self) -> int: ...
    def is_alive(self, pid: int) -> bool: ...
    def post_close(self, pids: set[int]) -> CloseRequest: ...
    def wait_exit(self, pids: set[int], timeout_s: float, abort: threading.Event) -> set[int]: ...
    def force_terminate_confirmed(self, pid: int) -> tuple[bool, str]: ...


class Launcher(Protocol):
    def launch(self, path: str, args: list[str], cwd: str | None) -> LaunchResult: ...


class PowerApi(Protocol):
    def list_schemes(self) -> list[PowerScheme]: ...
    def get_active(self) -> str | None: ...           # 取れない/書式が想定外なら None
    def set_active(self, guid: str) -> tuple[bool, str]: ...


class AudioApi(Protocol):
    def get_master(self) -> MasterState | None: ...
    def set_master(self, level: float | None, mute: bool | None) -> MasterState | None: ...  # 読み戻し値
    def list_sessions(self) -> list[SessionInfo]: ...
    def set_sessions(self, levels: dict[str, float]) -> dict[str, float | None]: ...        # 読み戻し値


class Opener(Protocol):
    def open_path(self, path: str) -> tuple[bool, str]: ...
    def open_url(self, url: str) -> tuple[bool, str]: ...


@dataclass
class Backends:
    processes: ProcessApi
    launcher: Launcher
    power: PowerApi
    audio: AudioApi
    opener: Opener


def real_backends() -> Backends:
    from deskkit.modules.modeshift._win32 import Win32Processes
    from deskkit.modules.modeshift.actions.audio import CoreAudio
    from deskkit.modules.modeshift.actions.launch import PopenLauncher
    from deskkit.modules.modeshift.actions.open import ShellOpener
    from deskkit.modules.modeshift.actions.power import PowercfgPower

    return Backends(Win32Processes(), PopenLauncher(), PowercfgPower(), CoreAudio(), ShellOpener())
