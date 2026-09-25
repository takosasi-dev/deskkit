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
class ThemeState:
    """アプリ・Windows(タスクバー等)のテーマ。"dark" / "light"。値が無い・読めないものは None。"""

    apps: str | None
    system: str | None


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


class ForceTarget(Protocol):
    """FR-14 の強制終了の候補。確認ダイアログを出す前に開いたプロセスハンドル(PID ではなくハンドルで終了させる)。
    開いている間はプロセスオブジェクトが残るので、対象が先に終了しても PID が別のプロセスに再利用されることはない。"""

    @property
    def pid(self) -> int: ...
    def alive(self) -> bool: ...
    def terminate_confirmed(self) -> tuple[bool, str]: ...   # 利用者が確認ダイアログで承認した後にだけ呼ぶ
    def close(self) -> None: ...


class ProcessApi(Protocol):
    def list_processes(self) -> list[ProcInfo]: ...
    def exe_path(self, pid: int) -> str | None: ...
    def own_pid(self) -> int: ...
    def is_alive(self, pid: int) -> bool: ...
    def post_close(self, pids: set[int]) -> CloseRequest: ...
    def wait_exit(self, pids: set[int], timeout_s: float, abort: threading.Event) -> set[int]: ...
    def open_for_force(self, pid: int, exe: str) -> ForceTarget | None: ...   # 開けない/exe が違えば None


class Launcher(Protocol):
    def launch(self, path: str, args: list[str], cwd: str | None) -> LaunchResult: ...


class PowerApi(Protocol):
    def list_schemes(self) -> list[PowerScheme]: ...
    def get_active(self) -> str | None: ...           # 取れない/書式が想定外なら None
    def set_active(self, guid: str) -> tuple[bool, str]: ...
    def ac_online(self) -> bool | None: ...           # AC 電源なら True / バッテリーなら False / 不明なら None


class AudioApi(Protocol):
    def get_master(self) -> MasterState | None: ...
    def set_master(self, level: float | None, mute: bool | None) -> MasterState | None: ...  # 読み戻し値
    # 既定の録音デバイス(マイク)。値の形は MasterState と同じ(device_id は録音デバイスの ID)
    def get_capture(self) -> MasterState | None: ...
    def set_capture(self, level: float | None, mute: bool | None) -> MasterState | None: ...  # 読み戻し値
    def list_sessions(self) -> list[SessionInfo]: ...
    def set_sessions(self, levels: dict[str, float]) -> dict[str, float | None]: ...        # 読み戻し値


class ThemeApi(Protocol):
    def get(self) -> ThemeState: ...
    # None の項目は変えない。書いたあと WM_SETTINGCHANGE("ImmersiveColorSet")を送り、読み戻した値を返す。
    # 戻り値の文字列は結果の注記(通知できなかったウィンドウがある等)
    def set(self, apps: str | None, system: str | None) -> tuple[ThemeState | None, str]: ...


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
    theme: ThemeApi


def real_backends() -> Backends:
    from deskkit.modules.modeshift._win32 import Win32Processes
    from deskkit.modules.modeshift.actions.audio import CoreAudio
    from deskkit.modules.modeshift.actions.launch import PopenLauncher
    from deskkit.modules.modeshift.actions.open import ShellOpener
    from deskkit.modules.modeshift.actions.power import PowercfgPower
    from deskkit.modules.modeshift.actions.theme import RegistryTheme

    return Backends(Win32Processes(), PopenLauncher(), PowercfgPower(), CoreAudio(), ShellOpener(), RegistryTheme())
