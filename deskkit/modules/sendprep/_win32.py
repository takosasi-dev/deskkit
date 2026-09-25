# SendPrep が使う Win32(ctypes): 既知のフォルダ(ピクチャ・送る)と、ショートカット(.lnk)の作成・読み取り(IShellLinkW)。
# ShellLinkApi の Protocol の背後に置き、テストでは偽物に差し替える。argtypes / restype を必ず設定する。
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path
from typing import Any, Protocol


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


def _guid(s: str) -> GUID:
    g = GUID()
    ole32 = ctypes.WinDLL("ole32")
    ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(GUID)]
    ole32.CLSIDFromString.restype = ctypes.HRESULT
    ole32.CLSIDFromString(s, ctypes.byref(g))
    return g


FOLDERID_PICTURES = "{33E28130-4E1E-4676-835A-98395C3BC3BB}"
FOLDERID_SENDTO = "{8983036C-27C0-404B-8F08-102D10DCFD74}"


def known_folder(fid: str) -> Path | None:
    try:
        shell32 = ctypes.WinDLL("shell32")
        ole32 = ctypes.WinDLL("ole32")
        fn = shell32.SHGetKnownFolderPath
        fn.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
        fn.restype = ctypes.HRESULT
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole32.CoTaskMemFree.restype = None
        out = ctypes.c_wchar_p()
        g = _guid(fid)
        fn(ctypes.byref(g), 0, None, ctypes.byref(out))
        try:
            return Path(out.value) if out.value else None
        finally:
            ole32.CoTaskMemFree(out)
    except OSError:
        return None


def pictures_dir() -> Path:
    return known_folder(FOLDERID_PICTURES) or (Path.home() / "Pictures")


def sendto_dir() -> Path:
    p = known_folder(FOLDERID_SENDTO)
    if p is not None:
        return p
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "SendTo"


# ------------------------------------------------------------------ ショートカット
class ShellLinkApi(Protocol):
    def create(self, path: Path, target: str, args: str, workdir: str, icon: str, description: str) -> None: ...
    def read(self, path: Path) -> tuple[str, str] | None: ...   # (リンク先, 引数)。読めなければ None


CLSID_SHELLLINK = "{00021401-0000-0000-C000-000000000046}"
IID_ISHELLLINKW = "{000214F9-0000-0000-C000-000000000046}"
IID_IPERSISTFILE = "{0000010B-0000-0000-C000-000000000046}"
_RELEASE, _QI = 2, 0
_SL_GETPATH, _SL_SETDESC, _SL_SETWD, _SL_GETARGS, _SL_SETARGS, _SL_SETICON, _SL_SETPATH = 3, 7, 9, 10, 11, 17, 20
_PF_LOAD, _PF_SAVE = 5, 6


def _vcall(obj: ctypes.c_void_p, index: int, argtypes: list[Any], *args: Any) -> int:
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
    fn = proto(vtbl[index])
    return int(fn(obj, *args))


def _release(obj: ctypes.c_void_p) -> None:
    if obj:
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[_RELEASE])(obj)


class RealShellLink:
    """IShellLinkW / IPersistFile を ctypes で直接呼ぶ(pywin32 を使わない)。"""

    def _open(self) -> tuple[ctypes.c_void_p, ctypes.c_void_p, bool]:
        ole32 = ctypes.WinDLL("ole32")
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        ole32.CoInitializeEx.restype = ctypes.c_long
        hr = ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED(Qt の GUI スレッドは初期化済みで S_FALSE)
        inited = hr in (0, 1)
        ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(GUID),
                                           ctypes.POINTER(ctypes.c_void_p)]
        ole32.CoCreateInstance.restype = ctypes.HRESULT
        link = ctypes.c_void_p()
        clsid, iid = _guid(CLSID_SHELLLINK), _guid(IID_ISHELLLINKW)
        ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(link))
        pf = ctypes.c_void_p()
        iid_pf = _guid(IID_IPERSISTFILE)
        _vcall(link, _QI, [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(iid_pf), ctypes.byref(pf))
        return link, pf, inited

    @staticmethod
    def _close(link: ctypes.c_void_p, pf: ctypes.c_void_p, inited: bool) -> None:
        _release(pf)
        _release(link)
        if inited:
            ole32 = ctypes.WinDLL("ole32")
            ole32.CoUninitialize.argtypes = []
            ole32.CoUninitialize.restype = None
            ole32.CoUninitialize()

    def create(self, path: Path, target: str, args: str, workdir: str, icon: str, description: str) -> None:
        link, pf, inited = self._open()
        try:
            w = [wintypes.LPCWSTR]
            _vcall(link, _SL_SETPATH, w, target)
            _vcall(link, _SL_SETARGS, w, args)
            _vcall(link, _SL_SETWD, w, workdir)
            _vcall(link, _SL_SETDESC, w, description)
            _vcall(link, _SL_SETICON, [wintypes.LPCWSTR, ctypes.c_int], icon, 0)
            _vcall(pf, _PF_SAVE, [wintypes.LPCWSTR, wintypes.BOOL], str(path), True)
        finally:
            self._close(link, pf, inited)

    def read(self, path: Path) -> tuple[str, str] | None:
        try:
            link, pf, inited = self._open()
        except OSError:
            return None
        try:
            _vcall(pf, _PF_LOAD, [wintypes.LPCWSTR, wintypes.DWORD], str(path), 0)
            buf = ctypes.create_unicode_buffer(1024)
            _vcall(link, _SL_GETPATH, [wintypes.LPWSTR, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], buf, 1024, None, 4)
            abuf = ctypes.create_unicode_buffer(8192)
            _vcall(link, _SL_GETARGS, [wintypes.LPWSTR, ctypes.c_int], abuf, 8192)
            return buf.value, abuf.value
        except OSError:
            return None
        finally:
            self._close(link, pf, inited)
