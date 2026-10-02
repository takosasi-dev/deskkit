# テストと自己検査で使う偽の Probes。値は属性で差し替え、raise_on に名前を入れるとその読み取りで例外を出す。
# OS には触れない(psutil・winrt・レジストリ・ファイルを読まない)。
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime

from deskkit.modules.pccheckup._win32 import AdapterInfo
from deskkit.modules.pccheckup.checks.base import Cancel, GiB
from deskkit.modules.pccheckup.probes import (
    COST_UNRESTRICTED,
    LEVEL_INTERNET,
    OVERLAY_BALANCED,
    ConnInfo,
    CpuSample,
    DiskInfo,
    Firmware,
    FolderSize,
    MemInfo,
    PowerInfo,
    ProcUsage,
    ProxyInfo,
    RegRead,
    SecureBootRaw,
    SizeInfo,
    StartupInfo,
)

WIFI_ID = "11111111-2222-3333-4444-555555555555"
FAKE_TODAY = date(2026, 9, 26)   # 偽の Probes の既定の今日(証明書の期限 2026-10-19 より前)


def sb_raw(firmware: str = "uefi", sb: RegRead | None = None, status: RegRead | None = None,
           error: RegRead | None = None, capable: RegRead | None = None) -> SecureBootRaw:
    """起動の安全の読み取り結果を作る。既定は「UEFI・有効・更新済み・Error なし・Capable 2」(この PC の実測と同じ形)。"""
    fw: Firmware = "uefi" if firmware == "uefi" else "bios" if firmware == "bios" else "unknown"
    return SecureBootRaw(fw, sb or RegRead("ok", 1), status or RegRead("ok", "Updated"), error or RegRead("missing"),
                         capable or RegRead("ok", 2))


def _adapter() -> AdapterInfo:
    return AdapterInfo(WIFI_ID, 71, True, ("192.168.1.20",), True, ("192.168.1.1",), ("192.168.1.1",), 35, 300_000_000)


@dataclass
class FakeProbes:
    cpu_value: CpuSample = field(default_factory=lambda: CpuSample(20.0, (ProcUsage("editor.exe", 8.0),)))
    mem: MemInfo = field(default_factory=lambda: MemInfo(40.0, 16 * GiB, (ProcUsage("browser.exe", 2.0 * GiB),)))
    sysdrive: DiskInfo = field(default_factory=lambda: DiskInfo("C:\\", 500 * GiB, 200 * GiB))
    uptime: float = 3600.0
    startup_info: StartupInfo = field(default_factory=lambda: StartupInfo(5, 2))
    power_info: PowerInfo = field(default_factory=lambda: PowerInfo(True, False, False, OVERLAY_BALANCED, 80))
    reboot: bool = False
    conn: ConnInfo = field(default_factory=lambda: ConnInfo(True, LEVEL_INTERNET, True, 4, 300.0, WIFI_ID,
                                                              COST_UNRESTRICTED, None))
    adapter_list: list[AdapterInfo] = field(default_factory=lambda: [_adapter()])
    proxy_info: ProxyInfo = field(default_factory=lambda: ProxyInfo(False, False))
    drives: list[DiskInfo] = field(default_factory=lambda: [DiskInfo("C:\\", 500 * GiB, 200 * GiB)])
    recycle: int = 10 * 1024 * 1024
    temp: SizeInfo = field(default_factory=lambda: SizeInfo(5 * 1024 * 1024, 3))
    downloads_path: str = "C:\\Users\\someone\\Downloads"
    downloads_size: SizeInfo = field(default_factory=lambda: SizeInfo(1 * GiB, 30))
    folders: list[FolderSize] = field(default_factory=lambda: [
        FolderSize("AppData", "C:\\Users\\someone\\AppData", SizeInfo(20 * GiB, 1000)),
        FolderSize("Pictures", "C:\\Users\\someone\\Pictures", SizeInfo(5 * GiB, 300)),
    ])
    sense: bool = True
    sb: SecureBootRaw = field(default_factory=sb_raw)
    raise_on: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)
    # 今日(B2 の期限の判定)。now を渡せばその時計の日付、無ければ FAKE_TODAY(本物の日付に頼らない)
    now: Callable[[], float] | None = None

    def _hit(self, name: str) -> None:
        self.calls.append(name)
        if name in self.raise_on:
            raise OSError(f"偽の失敗: {name}")

    def cpu(self, seconds: int, cancel: Cancel) -> CpuSample:
        self._hit("cpu")
        return self.cpu_value

    def memory(self) -> MemInfo:
        self._hit("memory")
        return self.mem

    def system_drive(self) -> DiskInfo:
        self._hit("system_drive")
        return self.sysdrive

    def uptime_seconds(self) -> float:
        self._hit("uptime")
        return self.uptime

    def startup(self) -> StartupInfo:
        self._hit("startup")
        return self.startup_info

    def power(self) -> PowerInfo:
        self._hit("power")
        return self.power_info

    def reboot_pending(self) -> bool:
        self._hit("reboot")
        return self.reboot

    def connectivity(self) -> ConnInfo:
        self._hit("connectivity")
        return self.conn

    def adapters(self) -> list[AdapterInfo]:
        self._hit("adapters")
        return list(self.adapter_list)

    def proxy(self) -> ProxyInfo:
        self._hit("proxy")
        return self.proxy_info

    def fixed_drives(self) -> list[DiskInfo]:
        self._hit("fixed_drives")
        return list(self.drives)

    def recycle_bin_bytes(self) -> int:
        self._hit("recycle")
        return self.recycle

    def old_temp(self, limit_s: float, cancel: Cancel) -> SizeInfo:
        self._hit("old_temp")
        return self.temp

    def downloads(self, limit_s: float, cancel: Cancel) -> tuple[str, SizeInfo]:
        self._hit("downloads")
        return self.downloads_path, self.downloads_size

    def user_folders(self, limit_s: float, cancel: Cancel) -> list[FolderSize]:
        self._hit("user_folders")
        return list(self.folders)

    def storage_sense(self) -> bool:
        self._hit("storage_sense")
        return self.sense

    def secure_boot(self) -> SecureBootRaw:
        self._hit("secure_boot")
        return self.sb

    def today(self) -> date:
        return datetime.fromtimestamp(self.now()).date() if self.now is not None else FAKE_TODAY
