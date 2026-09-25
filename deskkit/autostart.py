# 自動起動のオン/オフ(C-10 / FR-14)。HKCU\...\Run の値 "DeskKit" 1つを追加・削除するだけ。
# 登録済みのパスが今の実行ファイルと違えば "mismatch" を返し、オンにし直すと今のパスで上書きする。
from __future__ import annotations

import subprocess
import winreg

from deskkit import paths

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "DeskKit"


def _command() -> str:
    return subprocess.list2cmdline(paths.launch_command() + ["--autostart"])


def _read() -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            v, _t = winreg.QueryValueEx(k, VALUE)
            return str(v)
    except OSError:
        return None


def state() -> str:
    cur = _read()
    if cur is None:
        return "off"
    return "on" if cur == _command() else "mismatch"


def enable() -> None:
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, VALUE, 0, winreg.REG_SZ, _command())


def disable() -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, VALUE)
    except FileNotFoundError:
        pass


def toggle() -> str:
    if state() == "on":
        disable()
    else:
        enable()
    return state()
