# LayoutKeep の Phase 0 診断スクリプト(§9.6)。標準ライブラリと ctypes だけで単体で動き、読むだけで何も動かさない。
# サブコマンド: monitors [--no-dpi-aware] / windows / watch [--interval-ms N] / awareness。出力は JSON Lines。
# タイトルは画面に出すだけでファイルに書かない(貼るときは伏せること)。ウィンドウの配置を変える API は一切呼ばない。
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import sys
import time
from ctypes import wintypes as w
from typing import Any

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    shcore: Any = ctypes.WinDLL("shcore", use_last_error=True)
except OSError:
    shcore = None
try:
    dwmapi: Any = ctypes.WinDLL("dwmapi", use_last_error=True)
except OSError:
    dwmapi = None

# ---- 定数(Windows SDK のヘッダ値)
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
DPI_AWARENESS_NAMES = {-1: "invalid", 0: "unaware", 1: "system_aware", 2: "per_monitor_aware"}
MONITORINFOF_PRIMARY = 1
MDT_EFFECTIVE_DPI = 0
EDD_GET_DEVICE_INTERFACE_NAME = 1
DISPLAY_DEVICE_ACTIVE = 1
QDC_ONLY_ACTIVE_PATHS = 2
GET_SOURCE_NAME, GET_TARGET_NAME = 1, 2
ERROR_INSUFFICIENT_BUFFER = 122
GWL_EXSTYLE = -20
GW_OWNER = 4
WS_EX_TOOLWINDOW = 0x80
DWMWA_CLOAKED = 14
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", w.DWORD),
                ("szDevice", w.WCHAR * 32)]


class DISPLAY_DEVICEW(ctypes.Structure):  # noqa: N801
    _fields_ = [("cb", w.DWORD), ("DeviceName", w.WCHAR * 32), ("DeviceString", w.WCHAR * 128),
                ("StateFlags", w.DWORD), ("DeviceID", w.WCHAR * 128), ("DeviceKey", w.WCHAR * 128)]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", w.UINT), ("flags", w.UINT), ("showCmd", w.UINT), ("ptMinPosition", w.POINT),
                ("ptMaxPosition", w.POINT), ("rcNormalPosition", w.RECT)]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", w.DWORD), ("HighPart", w.LONG)]


class PATH_SOURCE(ctypes.Structure):  # noqa: N801
    _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32), ("statusFlags", ctypes.c_uint32)]


class PATH_TARGET(ctypes.Structure):  # noqa: N801
    _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                ("outputTechnology", ctypes.c_uint32), ("rotation", ctypes.c_uint32), ("scaling", ctypes.c_uint32),
                ("refreshNum", ctypes.c_uint32), ("refreshDen", ctypes.c_uint32), ("scanLineOrdering", ctypes.c_uint32),
                ("targetAvailable", w.BOOL), ("statusFlags", ctypes.c_uint32)]


class PATH_INFO(ctypes.Structure):  # noqa: N801
    _fields_ = [("sourceInfo", PATH_SOURCE), ("targetInfo", PATH_TARGET), ("flags", ctypes.c_uint32)]


class MODE_INFO(ctypes.Structure):  # noqa: N801
    _fields_ = [("infoType", ctypes.c_uint32), ("id", ctypes.c_uint32), ("adapterId", LUID), ("union", ctypes.c_uint64 * 6)]


class INFO_HEADER(ctypes.Structure):  # noqa: N801
    _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32), ("adapterId", LUID), ("id", ctypes.c_uint32)]


class SOURCE_NAME(ctypes.Structure):  # noqa: N801
    _fields_ = [("header", INFO_HEADER), ("viewGdiDeviceName", w.WCHAR * 32)]


class TARGET_NAME(ctypes.Structure):  # noqa: N801
    _fields_ = [("header", INFO_HEADER), ("flags", ctypes.c_uint32), ("outputTechnology", ctypes.c_uint32),
                ("edidManufactureId", ctypes.c_uint16), ("edidProductCodeId", ctypes.c_uint16),
                ("connectorInstance", ctypes.c_uint32), ("monitorFriendlyDeviceName", w.WCHAR * 64),
                ("monitorDevicePath", w.WCHAR * 128)]


WNDENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
MONITORENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HMONITOR, w.HDC, ctypes.POINTER(w.RECT), w.LPARAM)

user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetProcessDpiAwarenessContext.restype = w.BOOL
user32.GetThreadDpiAwarenessContext.argtypes = []
user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
user32.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.AreDpiAwarenessContextsEqual.restype = w.BOOL
user32.EnumDisplayMonitors.argtypes = [w.HDC, ctypes.POINTER(w.RECT), MONITORENUMPROC, w.LPARAM]
user32.EnumDisplayMonitors.restype = w.BOOL
user32.GetMonitorInfoW.argtypes = [w.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
user32.GetMonitorInfoW.restype = w.BOOL
user32.EnumDisplayDevicesW.argtypes = [w.LPCWSTR, w.DWORD, ctypes.POINTER(DISPLAY_DEVICEW), w.DWORD]
user32.EnumDisplayDevicesW.restype = w.BOOL
user32.GetDisplayConfigBufferSizes.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
user32.GetDisplayConfigBufferSizes.restype = w.LONG
user32.QueryDisplayConfig.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(PATH_INFO),
                                      ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(MODE_INFO), ctypes.c_void_p]
user32.QueryDisplayConfig.restype = w.LONG
user32.DisplayConfigGetDeviceInfo.argtypes = [ctypes.POINTER(INFO_HEADER)]
user32.DisplayConfigGetDeviceInfo.restype = w.LONG
user32.EnumWindows.argtypes = [WNDENUMPROC, w.LPARAM]
user32.EnumWindows.restype = w.BOOL
user32.IsWindowVisible.argtypes = [w.HWND]
user32.IsWindowVisible.restype = w.BOOL
user32.GetWindow.argtypes = [w.HWND, w.UINT]
user32.GetWindow.restype = w.HWND
_get_long = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
_get_long.argtypes = [w.HWND, ctypes.c_int]
_get_long.restype = ctypes.c_ssize_t
user32.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowTextLengthW.argtypes = [w.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
user32.GetWindowThreadProcessId.restype = w.DWORD
user32.GetWindowPlacement.argtypes = [w.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
user32.GetWindowPlacement.restype = w.BOOL
user32.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
user32.GetWindowRect.restype = w.BOOL
kernel32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
kernel32.OpenProcess.restype = w.HANDLE
kernel32.CloseHandle.argtypes = [w.HANDLE]
kernel32.CloseHandle.restype = w.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = w.BOOL
if shcore is not None:
    shcore.GetDpiForMonitor.argtypes = [w.HMONITOR, ctypes.c_int, ctypes.POINTER(w.UINT), ctypes.POINTER(w.UINT)]
    shcore.GetDpiForMonitor.restype = ctypes.c_long
if dwmapi is not None:
    dwmapi.DwmGetWindowAttribute.argtypes = [w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD]
    dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long


def out(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def rect(r: w.RECT) -> list[int]:
    return [int(r.left), int(r.top), int(r.right), int(r.bottom)]


def awareness() -> str:
    ctx = user32.GetThreadDpiAwarenessContext()
    if user32.AreDpiAwarenessContextsEqual(ctx, ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
        return "per_monitor_aware_v2"
    return DPI_AWARENESS_NAMES.get(user32.GetAwarenessFromDpiAwarenessContext(ctx), "unknown")


def edid_mfg(raw: int) -> str:
    v = ((raw & 0xFF) << 8) | ((raw >> 8) & 0xFF)
    cs = [(v >> 10) & 0x1F, (v >> 5) & 0x1F, v & 0x1F]
    return "".join(chr(c + 64) for c in cs) if all(1 <= c <= 26 for c in cs) else f"{raw:04X}"


def displayconfig() -> dict[str, tuple[str | None, str | None]]:
    res: dict[str, tuple[str | None, str | None]] = {}
    for _ in range(3):
        np_, nm = ctypes.c_uint32(), ctypes.c_uint32()
        if user32.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(np_), ctypes.byref(nm)) != 0:
            return res
        paths = (PATH_INFO * max(1, np_.value))()
        modes = (MODE_INFO * max(1, nm.value))()
        rc = user32.QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(np_), paths, ctypes.byref(nm), modes, None)
        if rc == ERROR_INSUFFICIENT_BUFFER:
            continue
        if rc != 0:
            return res
        for i in range(np_.value):
            p = paths[i]
            src = SOURCE_NAME()
            src.header.type, src.header.size = GET_SOURCE_NAME, ctypes.sizeof(src)
            src.header.adapterId, src.header.id = p.sourceInfo.adapterId, p.sourceInfo.id
            tgt = TARGET_NAME()
            tgt.header.type, tgt.header.size = GET_TARGET_NAME, ctypes.sizeof(tgt)
            tgt.header.adapterId, tgt.header.id = p.targetInfo.adapterId, p.targetInfo.id
            if user32.DisplayConfigGetDeviceInfo(ctypes.byref(src.header)) or user32.DisplayConfigGetDeviceInfo(ctypes.byref(tgt.header)):
                continue
            edid = f"{edid_mfg(tgt.edidManufactureId)}-{tgt.edidProductCodeId:04X}" if tgt.flags & 0x4 else None
            res.setdefault(src.viewGdiDeviceName.upper(), (tgt.monitorDevicePath or None, edid))
        return res
    return res


def device_interface(device: str) -> str | None:
    dd = DISPLAY_DEVICEW()
    first = None
    for i in range(16):
        dd.cb = ctypes.sizeof(dd)
        if not user32.EnumDisplayDevicesW(device, i, ctypes.byref(dd), EDD_GET_DEVICE_INTERFACE_NAME):
            break
        if dd.DeviceID and dd.StateFlags & DISPLAY_DEVICE_ACTIVE:
            return str(dd.DeviceID)
        first = first or (str(dd.DeviceID) or None)
    return first


def monitors() -> list[dict[str, Any]]:
    hs: list[int] = []
    proc = MONITORENUMPROC(lambda h, _d, _r, _l: (hs.append(int(h)) if h else None) or True)
    user32.EnumDisplayMonitors(None, None, proc, 0)
    dc = displayconfig()
    rows = []
    for h in hs:
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(mi)
        if not user32.GetMonitorInfoW(h, ctypes.byref(mi)):
            continue
        dpi = None
        if shcore is not None:
            x, y = w.UINT(), w.UINT()
            if shcore.GetDpiForMonitor(h, MDT_EFFECTIVE_DPI, ctypes.byref(x), ctypes.byref(y)) == 0:
                dpi = int(x.value)
        path, edid = dc.get(mi.szDevice.upper(), (None, None))
        rows.append({"device": mi.szDevice, "rcMonitor": rect(mi.rcMonitor), "rcWork": rect(mi.rcWork),
                     "primary": bool(mi.dwFlags & MONITORINFOF_PRIMARY), "dpi": dpi,
                     "ids": {"device_interface": device_interface(mi.szDevice), "displayconfig_path": path, "edid": edid}})
    return rows


def signatures(rows: list[dict[str, Any]]) -> dict[str, str | None]:
    """候補 ID ごとの構成シグネチャ(LayoutKeep §9.3 と同じ規則)。"""
    prim = next((r for r in rows if r["primary"]), None)
    out_: dict[str, str | None] = {}
    for src in ("device_interface", "displayconfig_path", "edid"):
        if prim is None or any(not r["ids"][src] or r["dpi"] is None for r in rows):
            out_[src] = None
            continue
        ox, oy = prim["rcMonitor"][0], prim["rcMonitor"][1]
        items = [{"id": r["ids"][src], "w": r["rcMonitor"][2] - r["rcMonitor"][0], "h": r["rcMonitor"][3] - r["rcMonitor"][1],
                  "x": r["rcMonitor"][0] - ox, "y": r["rcMonitor"][1] - oy, "dpi": r["dpi"], "primary": r["primary"]}
                 for r in rows]
        items.sort(key=lambda d: (str(d["id"]), json.dumps(d, sort_keys=True, separators=(",", ":"))))
        text = json.dumps(items, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        out_[src] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return out_


def exe_of(pid: int) -> tuple[str | None, int]:
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None, ctypes.get_last_error()
    try:
        buf = ctypes.create_unicode_buffer(32768)
        n = w.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return buf.value, 0
        return None, ctypes.get_last_error()
    finally:
        kernel32.CloseHandle(h)


def windows(*, with_title: bool) -> list[dict[str, Any]]:
    hs: list[int] = []
    proc = WNDENUMPROC(lambda h, _l: (hs.append(int(h)) if h else None) or True)
    user32.EnumWindows(proc, 0)
    rows = []
    for h in hs:
        if not user32.IsWindowVisible(h):
            continue
        pid = w.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        exe, err = exe_of(int(pid.value))
        cb = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, cb, 256)
        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(wp)
        user32.GetWindowPlacement(h, ctypes.byref(wp))
        rc = w.RECT()
        user32.GetWindowRect(h, ctypes.byref(rc))
        cloaked = w.DWORD()
        if dwmapi is not None:
            dwmapi.DwmGetWindowAttribute(h, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        row: dict[str, Any] = {"hwnd": h, "pid": int(pid.value), "exe": exe, "class": cb.value}
        if with_title:
            n = user32.GetWindowTextLengthW(h)
            tb = ctypes.create_unicode_buffer(max(1, n + 1))
            user32.GetWindowTextW(h, tb, len(tb))
            row["title"] = tb.value
        row.update({"showCmd": int(wp.showCmd), "rcNormalPosition": rect(wp.rcNormalPosition), "windowRect": rect(rc),
                    "cloaked": int(cloaked.value), "toolwindow": bool(_get_long(h, GWL_EXSTYLE) & WS_EX_TOOLWINDOW),
                    "owned": bool(user32.GetWindow(h, GW_OWNER))})
        if exe is None:
            row["exe_error"] = err
        rows.append(row)
    return rows


def cmd_watch(interval_ms: int) -> None:
    t0 = time.monotonic()
    last_sig: dict[str, str | None] | None = None
    last_rects: dict[int, list[int]] = {}
    out({"t": 0.0, "event": "start", "awareness": awareness(), "hint": "Ctrl+C で終了"})
    try:
        while True:
            t = round(time.monotonic() - t0, 2)
            sig = signatures(monitors())
            if sig != last_sig:
                out({"t": t, "event": "signature", "signatures": sig})
                last_sig = sig
            cur = {r["hwnd"]: r for r in windows(with_title=False)
                   if not r["cloaked"] and not r["toolwindow"] and not r["owned"] and r["exe"]}
            for h, r in cur.items():
                if last_rects.get(h) != r["windowRect"]:
                    if h in last_rects:
                        out({"t": t, "event": "moved", "hwnd": h, "exe": os.path.basename(r["exe"]), "class": r["class"],
                             "windowRect": r["windowRect"], "showCmd": r["showCmd"]})
                    last_rects[h] = r["windowRect"]
            for h in [h for h in last_rects if h not in cur]:
                del last_rects[h]
            time.sleep(max(50, interval_ms) / 1000)
    except KeyboardInterrupt:
        out({"t": round(time.monotonic() - t0, 2), "event": "stop"})


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="LayoutKeep 診断(読み取り専用)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("monitors", help="モニタと ID 候補")
    m.add_argument("--no-dpi-aware", action="store_true", help="SetProcessDpiAwarenessContext を呼ばない")
    sub.add_parser("windows", help="可視トップレベルウィンドウ(タイトルは画面だけに出す)")
    wa = sub.add_parser("watch", help="構成シグネチャとウィンドウ矩形の変化を表示")
    wa.add_argument("--interval-ms", type=int, default=500)
    sub.add_parser("awareness", help="現在スレッドの DPI awareness")
    a = ap.parse_args(argv)
    before = awareness()
    set_ok = None
    if not getattr(a, "no_dpi_aware", False):
        set_ok = bool(user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)))
    if a.cmd == "awareness":
        out({"before": before, "set_pmv2": set_ok, "awareness": awareness()})
    elif a.cmd == "monitors":
        out({"awareness": awareness(), "set_pmv2": set_ok})
        rows = monitors()
        for r in rows:
            out(r)
        out({"signatures": signatures(rows)})
    elif a.cmd == "windows":
        out({"awareness": awareness(), "note": "title は画面表示のみ。貼るときは伏せること"})
        for r in windows(with_title=True):
            out(r)
    elif a.cmd == "watch":
        cmd_watch(a.interval_ms)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
