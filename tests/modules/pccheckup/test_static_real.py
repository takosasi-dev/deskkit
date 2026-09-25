# 禁止 API が無いこと(共通 VAC-3・VAC-4、仕様書 AC-8)と、NFR-5(import 時に重いライブラリを読まない)。
# @win32_real: この PC の実機で 3 カテゴリが FR-8 の時間内に終わる(AC-6)。本物の probes は読むだけ。
from __future__ import annotations

import logging
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from deskkit.modules.pccheckup.checks.base import Cancel

SRC = Path(__file__).resolve().parents[3] / "deskkit" / "modules" / "pccheckup"


def _grep(pattern: str) -> list[str]:
    rx = re.compile(pattern)
    hits = []
    for p in SRC.rglob("*.py"):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if rx.search(line):
                hits.append(f"{p.name}:{i}: {line.strip()}")
    return hits


def test_vac3_no_network_api() -> None:
    assert _grep(r"import socket|urllib|http\.client|requests|QNetwork") == []


def test_vac4_no_direct_delete() -> None:
    assert _grep(r"os\.remove|os\.unlink|Path\.unlink|\.unlink\(|shutil\.rmtree|send2trash") == []


def test_ac8_no_state_changes() -> None:
    assert _grep(r"TerminateProcess|psutil\.Process\([^)]*\)\.(kill|terminate)|reg add|winreg\.SetValue|winreg\.DeleteValue") == []
    # 念のため: レジストリへの書き込み・プロセスの終了に使う名前を一切使わない
    assert _grep(r"winreg\.(CreateKey|SetValueEx|DeleteKey)|\.kill\(|\.terminate\(|KEY_WRITE|KEY_ALL_ACCESS") == []


def test_nfr5_import_is_light() -> None:
    code = ("import sys, deskkit.modules.pccheckup as m; "
            "print(','.join(x for x in ('psutil', 'winrt', 'PySide6.QtWidgets') if x in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         cwd=str(SRC.parents[2]))
    assert out.stdout.strip() == ""


@pytest.mark.win32_real
@pytest.mark.parametrize(("cat", "limit_s"), [("perf", 10.0), ("net", 5.0), ("storage", 70.0)])
def test_ac6_real_timing(cat: str, limit_s: float) -> None:
    from deskkit.modules.pccheckup.probes import RealProbes
    from deskkit.modules.pccheckup.runner import run_category

    log = logging.getLogger("deskkit.pccheckup.test.real")
    seen: dict[str, float] = {}
    t0 = time.monotonic()
    res = run_category(cat, RealProbes(), Cancel(), log, on_finding=lambda _c, f: seen.setdefault(f.check_id, time.monotonic() - t0))
    assert res.ms / 1000 <= limit_s
    if cat == "storage":
        # S5 を除いて 10 秒以内に結果が出る(FR-8)
        assert max(v for k, v in seen.items() if k != "S5") <= 10.0
    assert all(f.status in ("good", "warn", "bad", "info", "unknown") for f in res.findings)


@pytest.mark.win32_real
def test_real_adapters_and_power() -> None:
    from deskkit.modules.pccheckup import _win32

    ads = _win32.adapters()
    assert any(a.up for a in ads)
    assert _win32.power_status().battery_percent >= 0
    assert _win32.recycle_bin_bytes() >= 0
    assert _win32.fixed_drives()
    assert _win32.known_folder(_win32.FOLDERID_DOWNLOADS)
