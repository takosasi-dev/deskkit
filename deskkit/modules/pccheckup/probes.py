# OS から値を読む部分。Probes(Protocol)の背後に置き、テストでは偽物に差し替える(§2・AC-1)。
# 本物(RealProbes)は psutil・winrt・ctypes(_win32)・winreg の読み取りだけを使い、通信・設定の変更はしない(INV-1・INV-2)。
# 重い import(psutil・winrt)は使う関数の中で行う(NFR-5)。1回の診断ごとに作り直し、同じ値の読み直しは覚えておく。
from __future__ import annotations

import os
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from deskkit.modules.pccheckup import cleanup, fswalk
from deskkit.modules.pccheckup._win32 import AdapterInfo
from deskkit.modules.pccheckup.checks.base import Cancel

__all__ = ["AdapterInfo", "ConnInfo", "CpuSample", "DiskInfo", "FolderSize", "MemInfo", "PowerInfo", "ProcUsage",
           "Probes", "ProxyInfo", "RealProbes", "SizeInfo", "StartupInfo"]

# 電源モード(オーバーレイ)の GUID。「最適な電力効率」だけを注意にする(P6)
OVERLAY_BEST_EFFICIENCY = "961cc777-2547-4f9d-8174-7d86181b8a7a"
OVERLAY_BALANCED = "00000000-0000-0000-0000-000000000000"
OVERLAY_BEST_PERFORMANCE = "ded574b5-45a0-4f42-8737-46345c09c238"

# NetworkConnectivityLevel(Windows.Networking.Connectivity)
LEVEL_NONE = 0
LEVEL_LOCAL = 1
LEVEL_CONSTRAINED = 2
LEVEL_INTERNET = 3
# NetworkCostType
COST_UNKNOWN = 0
COST_UNRESTRICTED = 1
COST_FIXED = 2
COST_VARIABLE = 3

_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN32 = r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"
_APPROVED = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"
_REBOOT_REQUIRED = r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired"
_INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
_STORAGE_POLICY = r"Software\Microsoft\Windows\CurrentVersion\StorageSense\Parameters\StoragePolicy"


@dataclass(frozen=True)
class ProcUsage:
    name: str       # exe 名(画面とコピーにだけ出す。ログ・履歴には書かない)
    value: float    # CPU は 0〜100%、メモリはバイト


@dataclass(frozen=True)
class CpuSample:
    percent: float                 # 1 秒ごとの全体の使用率の平均
    top: tuple[ProcUsage, ...]     # 上位 5(論理コア数で割って 0〜100%)
    samples: int = 5


@dataclass(frozen=True)
class MemInfo:
    percent: float
    total: int
    top: tuple[ProcUsage, ...]     # 上位 5(プライベート。exe 名ごとの合計)


@dataclass(frozen=True)
class DiskInfo:
    root: str
    total: int
    free: int


@dataclass(frozen=True)
class StartupInfo:
    enabled: int
    disabled: int


@dataclass(frozen=True)
class PowerInfo:
    has_battery: bool
    on_battery: bool
    saver: bool
    overlay: str | None            # 電源モードの GUID(小文字)。読めなければ None
    battery_percent: int | None = None


@dataclass(frozen=True)
class ConnInfo:
    has_profile: bool
    level: int = LEVEL_NONE
    is_wireless: bool = False
    signal_bars: int | None = None
    link_mbps: float | None = None
    adapter_id: str | None = None
    cost_type: int = COST_UNKNOWN
    ssid: str | None = None        # コピー文字列から伏せるためだけに持つ。画面・ログ・履歴には出さない


@dataclass(frozen=True)
class ProxyInfo:
    enabled: bool
    auto_config: bool


@dataclass(frozen=True)
class SizeInfo:
    bytes: int
    files: int = 0
    denied: bool = False
    partial: bool = False


@dataclass(frozen=True)
class FolderSize:
    name: str
    path: str
    size: SizeInfo


class Probes(Protocol):
    # 重い(P)
    def cpu(self, seconds: int, cancel: Cancel) -> CpuSample: ...
    def memory(self) -> MemInfo: ...
    def system_drive(self) -> DiskInfo: ...
    def uptime_seconds(self) -> float: ...
    def startup(self) -> StartupInfo: ...
    def power(self) -> PowerInfo: ...
    def reboot_pending(self) -> bool: ...
    # ネット(N)
    def connectivity(self) -> ConnInfo: ...
    def adapters(self) -> list[AdapterInfo]: ...
    def proxy(self) -> ProxyInfo: ...
    # 容量(S)
    def fixed_drives(self) -> list[DiskInfo]: ...
    def recycle_bin_bytes(self) -> int: ...
    def old_temp(self, limit_s: float, cancel: Cancel) -> SizeInfo: ...
    def downloads(self, limit_s: float, cancel: Cancel) -> tuple[str, SizeInfo]: ...
    def user_folders(self, limit_s: float, cancel: Cancel) -> list[FolderSize]: ...
    def storage_sense(self) -> bool: ...


def _stopper(limit_s: float, cancel: Cancel) -> Callable[[], bool]:
    deadline = time.monotonic() + limit_s
    return lambda: cancel.is_set() or time.monotonic() >= deadline


def _top(values: dict[str, float], n: int = 5) -> tuple[ProcUsage, ...]:
    items = sorted(values.items(), key=lambda kv: kv[1], reverse=True)[:n]
    return tuple(ProcUsage(k, v) for k, v in items if v > 0)


# ------------------------------------------------------------------ 本物
class RealProbes:
    """1回の診断ごとに作る。winrt と GetAdaptersAddresses の結果は N1〜N4 で使い回す(例外も覚える)。"""

    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._memo: dict[str, Any] = {}

    def _cached(self, key: str, fn: Callable[[], Any]) -> Any:
        if key not in self._memo:
            try:
                self._memo[key] = ("ok", fn())
            except Exception as e:  # noqa: BLE001 - 読めなかったことも覚えて、同じ例外を出し直す
                self._memo[key] = ("err", e)
        kind, v = self._memo[key]
        if kind == "err":
            raise v
        return v

    # ---- 重い
    def cpu(self, seconds: int, cancel: Cancel) -> CpuSample:
        import psutil

        ncpu = psutil.cpu_count(logical=True) or 1
        procs: list[Any] = []
        for p in psutil.process_iter(["name"]):
            if p.pid == 0:  # System Idle Process は「空き」なので数えない
                continue
            try:
                p.cpu_percent(None)
                procs.append(p)
            except psutil.Error:
                continue
        psutil.cpu_percent(None)
        samples: list[float] = []
        for _ in range(seconds):
            if cancel.wait(1.0):
                break
            samples.append(float(psutil.cpu_percent(None)))
        if not samples:  # 1 秒も測れずに中止された
            samples.append(float(psutil.cpu_percent(None)))
        per: dict[str, float] = defaultdict(float)
        for p in procs:
            try:
                v = float(p.cpu_percent(None)) / ncpu
            except psutil.Error:
                continue  # 終了した・読めないプロセスは除いて続ける(§10)
            name = p.info.get("name") or ""
            if name:
                per[name] += v
        return CpuSample(sum(samples) / len(samples), _top(per), len(samples))

    def memory(self) -> MemInfo:
        import psutil

        vm = psutil.virtual_memory()
        per: dict[str, float] = defaultdict(float)
        for p in psutil.process_iter(["name"]):
            if p.pid in (0, 4):
                continue
            try:
                mi = p.memory_info()
            except psutil.Error:
                continue
            name = p.info.get("name") or ""
            private = getattr(mi, "private", None)
            if name and private is not None:
                per[name] += float(private)
        return MemInfo(float(vm.percent), int(vm.total), _top(per))

    def system_drive(self) -> DiskInfo:
        from deskkit.modules.pccheckup import _win32

        root = (os.environ.get("SYSTEMDRIVE") or "C:").rstrip("\\") + "\\"
        total, free = _win32.disk_space(root)
        return DiskInfo(root, total, free)

    def uptime_seconds(self) -> float:
        import psutil

        return max(0.0, self._now() - float(psutil.boot_time()))

    def startup(self) -> StartupInfo:
        import winreg

        def values(root: int, path: str) -> list[str]:
            out: list[str] = []
            try:
                with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as k:
                    i = 0
                    while True:
                        try:
                            name, data, _t = winreg.EnumValue(k, i)
                        except OSError:
                            break
                        i += 1
                        if name and data:
                            out.append(name)
            except FileNotFoundError:
                pass
            return out

        def approved(root: int, sub: str) -> dict[str, bytes]:
            out: dict[str, bytes] = {}
            try:
                with winreg.OpenKey(root, _APPROVED + "\\" + sub, 0, winreg.KEY_READ) as k:
                    i = 0
                    while True:
                        try:
                            name, data, _t = winreg.EnumValue(k, i)
                        except OSError:
                            break
                        i += 1
                        if isinstance(data, bytes) and data:
                            out[name.lower()] = data
            except FileNotFoundError:
                pass
            return out

        def folder_items(path: str | None) -> list[str]:
            if not path or not os.path.isdir(path):
                return []
            return [e.name for e in os.scandir(path) if e.is_file() and e.name.lower() != "desktop.ini"]

        appdata = os.environ.get("APPDATA")
        progdata = os.environ.get("PROGRAMDATA")
        user_sf = os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs\Startup") if appdata else None
        common_sf = os.path.join(progdata, r"Microsoft\Windows\Start Menu\Programs\StartUp") if progdata else None
        hkcu, hklm = winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE
        sources = [
            (values(hkcu, _RUN), approved(hkcu, "Run")),
            (values(hklm, _RUN), approved(hklm, "Run")),
            (values(hklm, _RUN32), approved(hklm, "Run32")),
            (folder_items(user_sf), approved(hkcu, "StartupFolder")),
            (folder_items(common_sf), approved(hklm, "StartupFolder")),
        ]
        return count_startup(sources)

    def power(self) -> PowerInfo:
        from deskkit.modules.pccheckup import _win32

        s = _win32.power_status()
        has_battery = s.battery_flag not in (_win32.BATTERY_FLAG_NO_BATTERY, _win32.BATTERY_FLAG_UNKNOWN)
        return PowerInfo(
            has_battery=has_battery,
            on_battery=has_battery and s.ac_line == _win32.AC_LINE_OFFLINE,
            saver=s.saver_on,
            overlay=_win32.effective_overlay_scheme(),
            battery_percent=s.battery_percent if has_battery and s.battery_percent <= 100 else None,
        )

    def reboot_pending(self) -> bool:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _REBOOT_REQUIRED, 0, winreg.KEY_READ):
                return True
        except FileNotFoundError:
            return False

    # ---- ネット
    def connectivity(self) -> ConnInfo:
        return self._cached("conn", self._connectivity)  # type: ignore[no-any-return]

    def _connectivity(self) -> ConnInfo:
        # Windows が持っている判定を読むだけ(通信しない。INV-1)
        from winrt.windows.networking.connectivity import NetworkInformation

        p = NetworkInformation.get_internet_connection_profile()
        if p is None:
            return ConnInfo(has_profile=False)
        level = int(p.get_network_connectivity_level())
        wireless = bool(p.is_wlan_connection_profile or p.is_wwan_connection_profile)
        bars: int | None = None
        if wireless:
            try:
                b = p.get_signal_bars()
                bars = None if b is None else int(b)
            except (OSError, ValueError, TypeError):
                bars = None
        adapter_id: str | None = None
        mbps: float | None = None
        na = p.network_adapter
        if na is not None:
            adapter_id = str(na.network_adapter_id).strip("{}").lower()
            bps = int(na.inbound_max_bits_per_second or 0)
            mbps = bps / 1_000_000 if bps > 0 else None
        cost = COST_UNKNOWN
        try:
            cost = int(p.get_connection_cost().network_cost_type)
        except (OSError, ValueError, TypeError):
            cost = COST_UNKNOWN
        ssid: str | None = None
        if p.is_wlan_connection_profile:
            try:
                ssid = str(p.profile_name) or None
            except (OSError, ValueError, TypeError):
                ssid = None
        return ConnInfo(True, level, wireless, bars, mbps, adapter_id, cost, ssid)

    def adapters(self) -> list[AdapterInfo]:
        from deskkit.modules.pccheckup import _win32

        return self._cached("gaa", _win32.adapters)  # type: ignore[no-any-return]

    def proxy(self) -> ProxyInfo:
        import winreg

        enabled = False
        auto = False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS, 0, winreg.KEY_READ) as k:
                try:
                    enabled = int(winreg.QueryValueEx(k, "ProxyEnable")[0]) != 0
                except (FileNotFoundError, ValueError, TypeError):
                    enabled = False
                try:
                    auto = bool(str(winreg.QueryValueEx(k, "AutoConfigURL")[0]).strip())
                except FileNotFoundError:
                    auto = False
        except FileNotFoundError:
            pass
        return ProxyInfo(enabled, auto)

    # ---- 容量
    def fixed_drives(self) -> list[DiskInfo]:
        from deskkit.modules.pccheckup import _win32

        return [DiskInfo(r, t, f) for r, t, f in _win32.fixed_drives()]

    def recycle_bin_bytes(self) -> int:
        from deskkit.modules.pccheckup import _win32

        return _win32.recycle_bin_bytes()

    def old_temp(self, limit_s: float, cancel: Cancel) -> SizeInfo:
        scan = cleanup.scan_old_temp(cleanup.temp_root(), self._now(), _stopper(limit_s, cancel))
        return SizeInfo(scan.bytes, scan.count, scan.denied, scan.partial)

    def downloads(self, limit_s: float, cancel: Cancel) -> tuple[str, SizeInfo]:
        from deskkit.modules.pccheckup import _win32

        path = _win32.known_folder(_win32.FOLDERID_DOWNLOADS)
        if not path or not os.path.isdir(path):
            raise FileNotFoundError("downloads")
        st = fswalk.dir_size(path, _stopper(limit_s, cancel))
        return path, SizeInfo(st.bytes, st.files, st.denied, st.partial)

    def user_folders(self, limit_s: float, cancel: Cancel) -> list[FolderSize]:
        home = os.environ.get("USERPROFILE") or str(Path.home())
        stop = _stopper(limit_s, cancel)
        out: list[FolderSize] = []
        subdirs: list[tuple[str, str]] = []
        with os.scandir(home) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if fswalk.is_reparse(st) or not e.is_dir(follow_symlinks=False):
                    continue  # 「Application Data」などのジャンクションはたどらない
                subdirs.append((e.name, e.path))
        for name, path in sorted(subdirs, key=lambda x: x[0].lower()):
            if stop():
                out.append(FolderSize(name, path, SizeInfo(0, 0, False, True)))
                continue
            ws = fswalk.dir_size(path, stop)
            out.append(FolderSize(name, path, SizeInfo(ws.bytes, ws.files, ws.denied, ws.partial)))
        return out

    def storage_sense(self) -> bool:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STORAGE_POLICY, 0, winreg.KEY_READ) as k:
                return int(winreg.QueryValueEx(k, "01")[0]) == 1
        except FileNotFoundError:
            return False  # 一度もオンにしていない PC には値が無い(既定はオフ)


def count_startup(sources: list[tuple[list[str], dict[str, bytes]]]) -> StartupInfo:
    """(項目名の一覧, StartupApproved の値) の組から、有効/無効を数える。
    StartupApproved の値の先頭バイトが奇数なら無効、偶数または値が無ければ有効(§4 の推測。§8 で実測)。"""
    enabled = disabled = 0
    for names, appr in sources:
        for n in names:
            flag = appr.get(n.lower())
            if flag and flag[0] & 1:
                disabled += 1
            else:
                enabled += 1
    return StartupInfo(enabled, disabled)
