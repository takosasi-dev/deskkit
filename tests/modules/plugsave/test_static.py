# 仕様書 §11 の grep 系の受け入れ基準(AC-19〜AC-22)をテストでも確かめる。
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3] / "deskkit" / "modules" / "plugsave"


def _hits(pattern: str) -> list[tuple[str, int, str]]:
    rx = re.compile(pattern)
    out: list[tuple[str, int, str]] = []
    for p in sorted(ROOT.rglob("*.py")):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if rx.search(line):
                out.append((p.name, i, line))
    return out


def test_ac19_no_network() -> None:
    assert _hits(r"import socket|urllib|http\.client|requests|QNetwork") == []


def test_ac20_deletions_only_own_parts_with_reason() -> None:
    hits = _hits(r"os\.remove|os\.unlink|\.unlink\(|shutil\.rmtree|send2trash|os\.rmdir|removedirs")
    assert hits, "削除の行が1つも無いのはおかしい(.part の片づけがある)"
    for name, _i, line in hits:
        assert name in ("staging.py", "copier.py"), (name, line)
        assert "#" in line and "INV-2" in line, line


def test_ac21_no_overwrite_paths() -> None:
    assert _hits(r"os\.replace|shutil\.(copy|copy2|copyfile|copytree|move)\(") == []


def test_ac22_no_hooks() -> None:
    assert _hits(r"SetWindowsHookEx|GetAsyncKeyState|GetKeyState|RegisterRawInputDevices|RegisterHotKey|UnregisterHotKey") == []


def test_no_host_internals() -> None:
    assert _hits(r"from deskkit (import (host|loader|win32)\b)|deskkit\.(host|loader|win32|nativewin)\b") == []
