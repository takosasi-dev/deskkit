# ruff: noqa  (仕様書 §8.1 の測定スクリプトを原文のまま置くため、書式の検査を外す)
# clip_formats.py — クリップボードの形式一覧と所有者 exe を表示する測定用スクリプト(本文は表示しない)
import ctypes, time
from ctypes import wintypes as w
u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
u32.OpenClipboard.argtypes = [w.HWND]; u32.OpenClipboard.restype = w.BOOL
u32.EnumClipboardFormats.argtypes = [w.UINT]; u32.EnumClipboardFormats.restype = w.UINT
u32.GetClipboardFormatNameW.argtypes = [w.UINT, w.LPWSTR, ctypes.c_int]
u32.GetClipboardData.argtypes = [w.UINT]; u32.GetClipboardData.restype = w.HANDLE
u32.GetClipboardOwner.restype = w.HWND; u32.GetClipboardSequenceNumber.restype = w.DWORD
u32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
u32.RegisterClipboardFormatW.argtypes = [w.LPCWSTR]; u32.RegisterClipboardFormatW.restype = w.UINT
k32.GlobalLock.argtypes = [w.HGLOBAL]; k32.GlobalLock.restype = ctypes.c_void_p
k32.GlobalUnlock.argtypes = [w.HGLOBAL]; k32.GlobalSize.argtypes = [w.HGLOBAL]; k32.GlobalSize.restype = ctypes.c_size_t
k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]; k32.OpenProcess.restype = w.HANDLE
k32.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
k32.CloseHandle.argtypes = [w.HANDLE]
FLAG_NAMES = ["ExcludeClipboardContentFromMonitorProcessing",
              "CanIncludeInClipboardHistory", "CanUploadToCloudClipboard"]
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000  # 値は SDK で確認すること

def owner_exe() -> str:
    hwnd = u32.GetClipboardOwner()
    if not hwnd: return "(所有者なし)"
    pid = w.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h: return f"(pid={pid.value} 開けない err={ctypes.get_last_error()})"
    try:
        buf = ctypes.create_unicode_buffer(32768); n = w.DWORD(len(buf))
        ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
        return buf.value if ok else f"(pid={pid.value} 名前取得失敗)"
    finally:
        k32.CloseHandle(h)

def dump() -> None:
    flags = {u32.RegisterClipboardFormatW(n): n for n in FLAG_NAMES}
    for _ in range(10):
        if u32.OpenClipboard(None): break
        time.sleep(0.05)
    else:
        print("  OpenClipboard 失敗"); return
    try:
        print("  owner:", owner_exe())
        fmt = 0
        while (fmt := u32.EnumClipboardFormats(fmt)) != 0:
            name = ctypes.create_unicode_buffer(256)
            label = name.value if u32.GetClipboardFormatNameW(fmt, name, 256) else "(標準形式)"
            extra = ""
            if fmt in flags:  # 除外形式だけ中身の先頭を見る。本文形式は読まない
                h = u32.GetClipboardData(fmt)
                p = k32.GlobalLock(h) if h else None
                if p:
                    size = k32.GlobalSize(h)
                    extra = f" size={size} head={ctypes.string_at(p, min(size, 8)).hex()}"
                    k32.GlobalUnlock(h)
                else:
                    extra = " (データ取得不可)"
            print(f"  {fmt:>6} {label}{extra}")
    finally:
        u32.CloseClipboard()

if __name__ == "__main__":
    print("コピーするたびに形式一覧を表示します。Ctrl+C で終了。本文は表示しません。")
    last = None
    while True:
        seq = u32.GetClipboardSequenceNumber()
        if seq != last:
            last = seq; print(f"--- {time.strftime('%H:%M:%S')} seq={seq}"); dump()
        time.sleep(0.5)
