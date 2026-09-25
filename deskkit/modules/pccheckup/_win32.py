# PcCheckup が読む Win32 API(ctypes)。読むだけで、設定・プロセス・サービスを変える関数は定義しない(INV-2)。
# GetAdaptersAddresses・SHQueryRecycleBinW・GetSystemPowerStatus・PowerGetEffectiveOverlayScheme・既知フォルダ・ドライブの空き。
# 定数値は Windows SDK のヘッダ(iptypes.h / shellapi.h / winbase.h / KnownFolders.h / fileapi.h)に合わせる。argtypes は必ず設定する。
from __future__ import annotations

import ctypes
import ipaddress
import sys
import uuid
from ctypes import wintypes as w
from dataclasses import dataclass

# --- ws2def.h
AF_UNSPEC = 0
AF_INET = 2
AF_INET6 = 23
# --- iptypes.h: GetAdaptersAddresses のフラグ
GAA_FLAG_SKIP_ANYCAST = 0x0002
GAA_FLAG_SKIP_MULTICAST = 0x0004
GAA_FLAG_INCLUDE_GATEWAYS = 0x0080
# --- ifdef.h: IF_OPER_STATUS
IF_OPER_STATUS_UP = 1
# --- ipifcons.h: IFTYPE
IF_TYPE_SOFTWARE_LOOPBACK = 24
IF_TYPE_TUNNEL = 131
# --- winerror.h
ERROR_SUCCESS = 0
ERROR_BUFFER_OVERFLOW = 111
S_OK = 0
# --- fileapi.h: GetDriveTypeW
DRIVE_FIXED = 3
# --- winbase.h: SYSTEM_POWER_STATUS
BATTERY_FLAG_NO_BATTERY = 128
BATTERY_FLAG_UNKNOWN = 255
AC_LINE_OFFLINE = 0
SYSTEM_STATUS_FLAG_SAVER_ON = 1
# --- KnownFolders.h: FOLDERID_Downloads
FOLDERID_DOWNLOADS = "{374DE290-123F-4565-9164-39C4925E467B}"


@dataclass(frozen=True)
class AdapterInfo:
    """使っているアダプタの IP・ゲートウェイ・DNS(N2)。adapter_id は小文字・波かっこ無しの GUID。"""

    adapter_id: str
    if_type: int
    up: bool
    ipv4: tuple[str, ...]
    has_ipv6: bool
    gateways: tuple[str, ...]
    dns: tuple[str, ...]
    metric: int
    link_bps: int


class _SOCKET_ADDRESS(ctypes.Structure):  # noqa: N801 - SDK の構造体名に合わせる
    _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]


class _ADDR_ENTRY(ctypes.Structure):  # noqa: N801 - SDK の構造体名に合わせる
    """IP_ADAPTER_UNICAST_ADDRESS / DNS_SERVER_ADDRESS / GATEWAY_ADDRESS の共通の先頭部分。"""


_ADDR_ENTRY._fields_ = [
    ("Alignment", ctypes.c_ulonglong),
    ("Next", ctypes.POINTER(_ADDR_ENTRY)),
    ("Address", _SOCKET_ADDRESS),
]


class _IP_ADAPTER_ADDRESSES(ctypes.Structure):  # noqa: N801 - SDK の構造体名に合わせる
    """IP_ADAPTER_ADDRESSES_LH の先頭から Ipv6Metric まで(以降は読まないので定義しない)。"""


_IP_ADAPTER_ADDRESSES._fields_ = [
    ("Alignment", ctypes.c_ulonglong),
    ("Next", ctypes.POINTER(_IP_ADAPTER_ADDRESSES)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.POINTER(_ADDR_ENTRY)),
    ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.POINTER(_ADDR_ENTRY)),
    ("DnsSuffix", ctypes.c_wchar_p),
    ("Description", ctypes.c_wchar_p),
    ("FriendlyName", ctypes.c_wchar_p),
    ("PhysicalAddress", ctypes.c_ubyte * 8),
    ("PhysicalAddressLength", w.ULONG),
    ("Flags", w.ULONG),
    ("Mtu", w.ULONG),
    ("IfType", w.ULONG),
    ("OperStatus", ctypes.c_int),
    ("Ipv6IfIndex", w.ULONG),
    ("ZoneIndices", w.ULONG * 16),
    ("FirstPrefix", ctypes.c_void_p),
    ("TransmitLinkSpeed", ctypes.c_ulonglong),
    ("ReceiveLinkSpeed", ctypes.c_ulonglong),
    ("FirstWinsServerAddress", ctypes.c_void_p),
    ("FirstGatewayAddress", ctypes.POINTER(_ADDR_ENTRY)),
    ("Ipv4Metric", w.ULONG),
    ("Ipv6Metric", w.ULONG),
]


class _SYSTEM_POWER_STATUS(ctypes.Structure):  # noqa: N801 - SDK の構造体名に合わせる
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", w.DWORD),
        ("BatteryFullLifeTime", w.DWORD),
    ]


class _SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]


class _GUID(ctypes.Structure):
    _fields_ = [("raw", ctypes.c_ubyte * 16)]


def _guid(s: str) -> _GUID:
    g = _GUID()
    ctypes.memmove(g.raw, uuid.UUID(s).bytes_le, 16)
    return g


_bound = False
_iphlp: ctypes.WinDLL
_k32: ctypes.WinDLL
_sh: ctypes.WinDLL
_ole: ctypes.WinDLL
_powr: ctypes.WinDLL


def _bind() -> None:
    global _bound, _iphlp, _k32, _sh, _ole, _powr
    if _bound:
        return
    if sys.platform != "win32":  # pragma: no cover - Windows 専用
        raise OSError("PcCheckup は Windows 専用です")
    _iphlp = ctypes.WinDLL("iphlpapi", use_last_error=True)
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _sh = ctypes.WinDLL("shell32", use_last_error=True)
    _ole = ctypes.WinDLL("ole32", use_last_error=True)
    _powr = ctypes.WinDLL("powrprof", use_last_error=True)
    _iphlp.GetAdaptersAddresses.argtypes = [w.ULONG, w.ULONG, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(w.ULONG)]
    _iphlp.GetAdaptersAddresses.restype = w.ULONG
    _k32.GetSystemPowerStatus.argtypes = [ctypes.POINTER(_SYSTEM_POWER_STATUS)]
    _k32.GetSystemPowerStatus.restype = w.BOOL
    _k32.GetLogicalDriveStringsW.argtypes = [w.DWORD, w.LPWSTR]
    _k32.GetLogicalDriveStringsW.restype = w.DWORD
    _k32.GetDriveTypeW.argtypes = [w.LPCWSTR]
    _k32.GetDriveTypeW.restype = w.UINT
    _k32.GetDiskFreeSpaceExW.argtypes = [w.LPCWSTR, ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong),
                                         ctypes.POINTER(ctypes.c_ulonglong)]
    _k32.GetDiskFreeSpaceExW.restype = w.BOOL
    _sh.SHQueryRecycleBinW.argtypes = [w.LPCWSTR, ctypes.POINTER(_SHQUERYRBINFO)]
    _sh.SHQueryRecycleBinW.restype = ctypes.c_long
    _sh.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(_GUID), w.DWORD, w.HANDLE, ctypes.POINTER(w.LPWSTR)]
    _sh.SHGetKnownFolderPath.restype = ctypes.c_long
    _ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    _ole.CoTaskMemFree.restype = None
    _powr.PowerGetEffectiveOverlayScheme.argtypes = [ctypes.POINTER(_GUID)]
    _powr.PowerGetEffectiveOverlayScheme.restype = w.DWORD
    _bound = True


# ------------------------------------------------------------------ ネットワーク(N2)
def _sockaddr_text(sa: _SOCKET_ADDRESS) -> tuple[int, str] | None:
    """SOCKET_ADDRESS → (family, 表記)。名前解決はしない(ipaddress で数字を文字にするだけ)。"""
    if not sa.lpSockaddr or sa.iSockaddrLength < 8:
        return None
    raw = ctypes.string_at(sa.lpSockaddr, sa.iSockaddrLength)
    family = int.from_bytes(raw[0:2], "little")
    if family == AF_INET and len(raw) >= 8:
        return AF_INET, str(ipaddress.IPv4Address(raw[4:8]))
    if family == AF_INET6 and len(raw) >= 24:
        return AF_INET6, str(ipaddress.IPv6Address(raw[8:24]))
    return None


def _walk(first: ctypes._Pointer[_ADDR_ENTRY]) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    p = first
    guard = 0
    while p and guard < 256:
        t = _sockaddr_text(p.contents.Address)
        if t is not None:
            out.append(t)
        p = p.contents.Next
        guard += 1
    return out


def adapters() -> list[AdapterInfo]:
    """GetAdaptersAddresses(AF_UNSPEC・ゲートウェイ込み)。読めなければ OSError。"""
    _bind()
    flags = GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST | GAA_FLAG_INCLUDE_GATEWAYS
    size = w.ULONG(16 * 1024)
    for _ in range(4):
        buf = ctypes.create_string_buffer(size.value)
        rc = _iphlp.GetAdaptersAddresses(AF_UNSPEC, flags, None, buf, ctypes.byref(size))
        if rc == ERROR_BUFFER_OVERFLOW:
            continue
        if rc != ERROR_SUCCESS:
            raise OSError(rc, "GetAdaptersAddresses")
        break
    else:
        raise OSError(ERROR_BUFFER_OVERFLOW, "GetAdaptersAddresses")
    out: list[AdapterInfo] = []
    p = ctypes.cast(buf, ctypes.POINTER(_IP_ADAPTER_ADDRESSES))
    guard = 0
    while p and guard < 256:
        a = p.contents
        name = (a.AdapterName or b"").decode("ascii", "replace").strip("{}").lower()
        uni = _walk(a.FirstUnicastAddress)
        gws = [t for f, t in _walk(a.FirstGatewayAddress) if f in (AF_INET, AF_INET6)]
        dns = [t for f, t in _walk(a.FirstDnsServerAddress) if f in (AF_INET, AF_INET6)]
        out.append(AdapterInfo(
            adapter_id=name, if_type=int(a.IfType), up=int(a.OperStatus) == IF_OPER_STATUS_UP,
            ipv4=tuple(t for f, t in uni if f == AF_INET), has_ipv6=any(f == AF_INET6 for f, _ in uni),
            gateways=tuple(gws), dns=tuple(dns), metric=int(a.Ipv4Metric),
            link_bps=int(a.ReceiveLinkSpeed) if a.ReceiveLinkSpeed != 0xFFFFFFFFFFFFFFFF else 0,
        ))
        p = a.Next
        guard += 1
    return out


# ------------------------------------------------------------------ 電源(P6)
@dataclass(frozen=True)
class PowerStatus:
    ac_line: int
    battery_flag: int
    battery_percent: int
    saver_on: bool


def power_status() -> PowerStatus:
    _bind()
    s = _SYSTEM_POWER_STATUS()
    if not _k32.GetSystemPowerStatus(ctypes.byref(s)):
        raise ctypes.WinError(ctypes.get_last_error())
    return PowerStatus(int(s.ACLineStatus), int(s.BatteryFlag), int(s.BatteryLifePercent),
                       bool(s.SystemStatusFlag & SYSTEM_STATUS_FLAG_SAVER_ON))


def effective_overlay_scheme() -> str | None:
    """電源モード(オーバーレイ)の GUID(小文字)。この API が無い・失敗なら None。"""
    _bind()
    g = _GUID()
    try:
        rc = _powr.PowerGetEffectiveOverlayScheme(ctypes.byref(g))
    except AttributeError:  # pragma: no cover - 古い Windows
        return None
    if rc != ERROR_SUCCESS:
        return None
    return str(uuid.UUID(bytes_le=bytes(g.raw)))


# ------------------------------------------------------------------ 容量(S1・S2・S4)
def recycle_bin_bytes() -> int:
    """全ドライブのごみ箱の合計サイズ(SHQueryRecycleBinW に NULL を渡す)。"""
    _bind()
    q = _SHQUERYRBINFO()
    q.cbSize = ctypes.sizeof(q)
    hr = int(_sh.SHQueryRecycleBinW(None, ctypes.byref(q)))
    if hr != S_OK:
        raise OSError(hr, "SHQueryRecycleBinW")
    return max(0, int(q.i64Size))


def fixed_drives() -> list[tuple[str, int, int]]:
    """固定ドライブの (ルート, 全体, 空き)。読めないドライブ(ロック中など)は飛ばす。"""
    _bind()
    buf = ctypes.create_unicode_buffer(1024)
    n = int(_k32.GetLogicalDriveStringsW(len(buf), buf))
    roots = [r for r in ctypes.wstring_at(buf, n).split("\0") if r]
    out: list[tuple[str, int, int]] = []
    for r in roots:
        if int(_k32.GetDriveTypeW(r)) != DRIVE_FIXED:
            continue
        avail, total, free = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
        if not _k32.GetDiskFreeSpaceExW(r, ctypes.byref(avail), ctypes.byref(total), ctypes.byref(free)):
            continue
        out.append((r, int(total.value), int(free.value)))
    return out


def disk_space(root: str) -> tuple[int, int]:
    """(全体, 空き)。読めなければ OSError。"""
    _bind()
    avail, total, free = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
    if not _k32.GetDiskFreeSpaceExW(root, ctypes.byref(avail), ctypes.byref(total), ctypes.byref(free)):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(total.value), int(free.value)


def known_folder(folder_id: str) -> str | None:
    _bind()
    p = w.LPWSTR()
    g = _guid(folder_id)
    hr = int(_sh.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(p)))
    try:
        if hr != S_OK or not p.value:
            return None
        return str(p.value)
    finally:
        if p:
            _ole.CoTaskMemFree(ctypes.cast(p, ctypes.c_void_p))
