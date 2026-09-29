# MojiFix が使う Win32(ctypes): 「ドキュメント」フォルダの場所(M-16 の書けないときの置き場)。
# 空き容量は shutil.disk_usage、属性は os.stat の st_file_attributes(標準ライブラリ)で読む。argtypes / restype を必ず設定する。
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


FOLDERID_DOCUMENTS = "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}"


def known_folder(fid: str) -> Path | None:
    try:
        shell32 = ctypes.WinDLL("shell32")
        ole32 = ctypes.WinDLL("ole32")
        ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(GUID)]
        ole32.CLSIDFromString.restype = ctypes.HRESULT
        fn = shell32.SHGetKnownFolderPath
        fn.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
        fn.restype = ctypes.HRESULT
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole32.CoTaskMemFree.restype = None
        g = GUID()
        ole32.CLSIDFromString(fid, ctypes.byref(g))
        out = ctypes.c_wchar_p()
        fn(ctypes.byref(g), 0, None, ctypes.byref(out))
        try:
            return Path(out.value) if out.value else None
        finally:
            ole32.CoTaskMemFree(out)
    except OSError:
        return None


def documents_mojifix() -> Path:
    """書けないときの置き場「ドキュメント\\MojiFix」。"""
    base = known_folder(FOLDERID_DOCUMENTS) or (Path.home() / "Documents")
    return base / "MojiFix"
