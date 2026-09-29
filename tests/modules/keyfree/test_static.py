# ソースの静的な確認: 禁止 API(AC-10・V4AC-3・V4AC-4)・host の内部を import しない(契約 §0・§3)・通信しない。
from __future__ import annotations

import re
from pathlib import Path

import deskkit.modules.keyfree as pkg

SRC = Path(pkg.__file__).parent


def _sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in SRC.glob("*.py")}


def test_no_forbidden_hotkey_or_input_api() -> None:
    """AC-10: RegisterHotKey・UnregisterHotKey・フック・キーの状態・入力の送信を使わない(INV-1・INV-3)。"""
    pat = re.compile(r"RegisterHotKey|UnregisterHotKey|SetWindowsHookEx|GetAsyncKeyState|import keyboard|pynput|SendInput|"
                     r"keybd_event|PostMessage")
    hits = [(n, m.group(0)) for n, s in _sources().items() for m in pat.finditer(s)]
    assert hits == []


def test_v4ac4_patterns() -> None:
    pat = re.compile(r"SetWindowsHookEx|GetAsyncKeyState|GetKeyState|RegisterRawInputDevices|RegisterHotKey|UnregisterHotKey")
    assert [n for n, s in _sources().items() if pat.search(s)] == []


def test_no_network() -> None:
    """V4AC-3。"""
    pat = re.compile(r"import socket|urllib|http\.client|requests|QNetwork")
    assert [n for n, s in _sources().items() if pat.search(s)] == []


def test_no_host_internals() -> None:
    pat = re.compile(r"^\s*(from|import)\s+(deskkit\.(host|loader|win32|context|ipc|settings)|overlaykit)\b", re.M)
    assert [n for n, s in _sources().items() if pat.search(s)] == []
