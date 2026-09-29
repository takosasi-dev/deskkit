# ソースの決まり: 通信しない(AC-14)・消すのは .part の片づけと自分の「送る」のショートカットだけ(AC-15)・
# シェルを通さない・ffmpeg の呼び出しは runner の Popen だけ・モジュールをまたいだ import が無い(C-1)。
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "deskkit" / "modules" / "cliptrim"


def _grep(pattern: str) -> list[tuple[str, int, str]]:
    rx = re.compile(pattern)
    out = []
    for p in sorted(SRC.rglob("*.py")):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if rx.search(line):
                out.append((p.name, i, line.strip()))
    return out


def test_ac14_no_network() -> None:
    assert _grep(r"import socket|urllib|http\.client|requests|QNetwork") == []


def test_ac15_deletes_only_parts_and_own_shortcut() -> None:
    hits = _grep(r"os\.remove|os\.unlink|\.unlink\(|shutil\.rmtree|send2trash|recycle|shell=True")
    allowed = {("jobs.py", "p.unlink(missing_ok=True)"), ("jobs.py", "p.unlink()"), ("sendto.py", "link_path(folder).unlink()")}
    assert {(f, line) for f, _i, line in hits} <= allowed, hits
    text = (SRC / "jobs.py").read_text(encoding="utf-8")
    # 片づけの関数には理由のコメントがある
    assert "def remove_part" in text and "INV-5" in text and "def cleanup_stale_parts" in text


def test_subprocess_only_in_runner_and_no_path_ffmpeg() -> None:
    hits = _grep(r"subprocess\.(Popen|run|call|check_output)")
    assert {f for f, _i, _l in hits} <= {"runner.py", "selftest.py"}, hits
    assert _grep(r"shutil\.which|\[\s*[\"']ffmpeg|environ\[.PATH|getenv\(.PATH") == []  # PATH 上の ffmpeg を使わない(INV-2)


def test_no_cross_module_imports() -> None:
    hits = _grep(r"deskkit\.modules\.(?!cliptrim)")
    assert hits == [], hits
    assert _grep(r"from deskkit (import|\.)(host|loader|win32)\b|deskkit\.(host|loader|win32)\b") == []


def test_no_forbidden_hooks() -> None:
    assert _grep(r"SetWindowsHookEx|GetAsyncKeyState|GetKeyState|RegisterRawInputDevices|RegisterHotKey|SendInput|keybd_event") == []
