# StartupWatch の静的な受け入れ基準: AC-11(コマンドの行)・AC-12(書き込み・実行・終了の API を使わない)・V4AC-3・V4AC-4・
# import 時に重い物を読まない(v0.3 NFR-5)。
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from deskkit.modules.startupwatch import cmdline

ROOT = Path(__file__).resolve().parents[3]
MOD = ROOT / "deskkit" / "modules" / "startupwatch"


def _grep(pattern: str) -> list[str]:
    rx = re.compile(pattern)
    hits: list[str] = []
    for p in MOD.rglob("*.py"):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if rx.search(line):
                hits.append(f"{p.name}:{i}: {line.strip()}")
    return hits


def test_ac11_cmdline() -> None:
    def expand(s: str) -> str:
        return s.replace("%ProgramFiles%", r"C:\Program Files")

    assert cmdline.exe_name('"C:\\A B\\x.exe" --y') == "x.exe"
    assert cmdline.exe_name("C:\\A\\x.exe /s") == "x.exe"
    assert cmdline.exe_name("rundll32.exe a.dll,Run") == "rundll32.exe"
    assert cmdline.exe_name("%ProgramFiles%\\A\\x.exe", expand) == "x.exe"
    assert cmdline.exe_name(r"C:\Program Files\A B\x.exe --flag") == "x.exe"
    assert cmdline.exe_name("") == ""
    assert cmdline.exe_name('"C:\\unterminated\\y.exe') == "y.exe"
    assert cmdline.exe_name("tool --x") == "tool"
    assert cmdline.same_program('"C:\\Tools\\DeskKit.EXE" --autostart', {cmdline.norm_program(r"c:\tools\deskkit.exe")})


def test_ac12_no_write_run_or_kill_apis() -> None:
    pat = (r"SetValue|DeleteValue|DeleteKey|CreateKey|RegSetValue|RegDeleteValue|KEY_WRITE|KEY_SET_VALUE|KEY_ALL_ACCESS|"
           r"subprocess|TerminateProcess|os\.remove|shutil\.(move|rmtree)")
    assert _grep(pat) == []


def test_v4ac3_no_network() -> None:
    assert _grep(r"import socket|urllib|http\.client|requests|QNetwork") == []


def test_v4ac4_no_hooks_or_hotkey_registration() -> None:
    assert _grep(r"SetWindowsHookEx|GetAsyncKeyState|GetKeyState|RegisterRawInputDevices|RegisterHotKey|UnregisterHotKey") == []


def test_only_three_things_are_opened() -> None:
    """INV-3: 外へ開くのは FR-10 の3つだけ(os.startfile は1か所。呼ぶのは open_settings・open_taskmgr・open_folder)。"""
    assert len(_grep(r"os\.startfile\(")) == 1
    assert len(_grep(r"self\._startfile\(")) == 3


def test_create_does_not_import_qt_widgets_or_win32() -> None:
    code = ("import sys; import deskkit.modules.startupwatch as m; "
            "print(any(k.startswith('PySide6.QtWidgets') for k in sys.modules), "
            "'deskkit.modules.startupwatch._win32' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=60, check=True)
    assert out.stdout.split() == ["False", "False"]
