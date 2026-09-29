# EyeBreak の静的な受け入れ基準: AC-15(入力を読む API・通信を使わない)・AC-16(文言の禁止語)・V4AC-4・
# import 時に Qt の画面の部品を読まない(v0.3 NFR-5)。
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from deskkit.modules.eyebreak.prompts import ALL_TEXTS, FORBIDDEN_WORDS, Prompt, fmt_minutes

ROOT = Path(__file__).resolve().parents[3]
MOD = ROOT / "deskkit" / "modules" / "eyebreak"


def _grep(pattern: str) -> list[str]:
    rx = re.compile(pattern)
    hits: list[str] = []
    for p in MOD.rglob("*.py"):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if rx.search(line):
                hits.append(f"{p.name}:{i}: {line.strip()}")
    return hits


def test_ac15_no_input_hooks_or_network() -> None:
    pat = (r"SetWindowsHookEx|GetAsyncKeyState|GetKeyState|GetKeyboardState|GetCursorPos|RegisterRawInputDevices|pynput|"
           r"import keyboard|import socket|urllib|http\.client|requests|QNetwork")
    assert _grep(pat) == []


def test_v4ac4_no_hotkey_registration() -> None:
    assert _grep(r"RegisterHotKey|UnregisterHotKey") == []


def test_only_two_input_apis() -> None:
    """INV-1: 入力に関わる Win32 API は GetLastInputInfo と GetTickCount だけ。"""
    names = set(re.findall(r"\b(?:u32|k32)\.(\w+)", (MOD / "_win32.py").read_text(encoding="utf-8")))
    assert names == {"GetLastInputInfo", "GetTickCount"}


def test_ac16_no_forbidden_words_in_texts() -> None:
    texts = list(ALL_TEXTS)
    for kind in ("eye", "body"):
        for ag in (False, True):
            texts.append(Prompt(kind, ag, 90).text)
    for t in texts:
        for w in FORBIDDEN_WORDS:
            assert w not in t, (w, t)


def test_ac16_no_forbidden_words_in_ui_sources() -> None:
    """画面・窓・トレイの文言にも禁止語を使わない(prompts.py の禁止語の一覧そのものは除く)。"""
    for name in ("module.py", "page.py", "dialog.py"):
        src = (MOD / name).read_text(encoding="utf-8")
        for s in re.findall(r'"([^"\n]*[^\x00-\x7f][^"\n]*)"', src):
            for w in FORBIDDEN_WORDS:
                assert w not in s, (name, s)


def test_fmt_minutes() -> None:
    assert [fmt_minutes(m) for m in (20, 60, 90, 0, 125)] == ["20分", "1時間", "1時間30分", "0分", "2時間5分"]


def test_import_does_not_load_widgets() -> None:
    code = ("import sys; import deskkit.modules.eyebreak as m; "
            "print(any(k.startswith('PySide6.QtWidgets') for k in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=60, check=True)
    assert out.stdout.strip() == "False"
