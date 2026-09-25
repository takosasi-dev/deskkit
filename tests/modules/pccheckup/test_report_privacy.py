# AC-3: コピー文字列にユーザー名・SSID・コンピューター名が入らず、プロセス名は入る(P-4)。
# AC-5: history.jsonl・ops.jsonl・ログ・diagnostics にプロセス名・パス・SSID が無い(INV-3)。
from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from deskkit.fileops import RecycleResult
from deskkit.modules.pccheckup.checks.base import GiB
from deskkit.modules.pccheckup.fakes import FakeProbes
from deskkit.modules.pccheckup.probes import ConnInfo, CpuSample, FolderSize, MemInfo, ProcUsage, SizeInfo
from deskkit.modules.pccheckup.report import Secrets, build, redact
from deskkit.modules.pccheckup.runner import BY_CATEGORY

USER = "yamada-secret"
PROFILE = f"C:\\Users\\{USER}"
SSID = "Cafe-SSID-7788"
HOST = "DESKTOP-SECRETPC"
PROC = "secretproc-x1.exe"


def _probes() -> FakeProbes:
    return FakeProbes(
        cpu_value=CpuSample(95.0, (ProcUsage(PROC, 80.0),)),
        mem=MemInfo(95.0, 16 * GiB, (ProcUsage(PROC, 9 * GiB),)),
        conn=ConnInfo(True, 3, True, 1, 10.0, "x", 1, SSID),
        downloads_path=f"{PROFILE}\\Downloads",
        downloads_size=SizeInfo(20 * GiB, 5),
        folders=[FolderSize(f"{USER}-docs", f"{PROFILE}\\{USER}-docs", SizeInfo(3 * GiB, 1)),
                 FolderSize(SSID, f"{PROFILE}\\{SSID}", SizeInfo(2 * GiB, 1))],
    )


def test_ac3_copy_text(make_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USERPROFILE", PROFILE)
    monkeypatch.setenv("USERNAME", USER)
    monkeypatch.setenv("COMPUTERNAME", HOST)
    m, _ctx, _ = make_module(_probes())
    m._ssids.add(SSID)
    m.run(list(BY_CATEGORY))
    text = m.copy_text()
    assert PROC in text
    low = text.lower()
    for secret in (USER, SSID, HOST):
        assert secret.lower() not in low
    assert "%USERNAME%-docs" in text


def test_redact_profile_path_and_case() -> None:
    s = Secrets(PROFILE, USER, HOST, (SSID,))
    assert redact(f"{PROFILE.upper()}\\Downloads", s) == "%USERPROFILE%\\Downloads"
    assert redact(f"{PROFILE.replace(chr(92), '/')}/x", s) == "%USERPROFILE%/x"
    assert redact(f"at {HOST} on {SSID}", s) == "at (PC 名) on (伏せ字)"
    # %USERPROFILE% の中の文字を後の置き換えで壊さない
    assert redact("C:\\Users\\user\\a", Secrets("C:\\Users\\user", "user", None, ())) == "%USERPROFILE%\\a"
    # 1 文字の名前は単独では置き換えない(ほかの語を壊すため)
    assert redact("a b", Secrets(None, "a", None, ())) == "a b"


def test_build_orders_by_status() -> None:
    from deskkit.modules.pccheckup.checks import perf

    fs = [perf.judge_reboot(False), perf.judge_reboot(True), perf.judge_cpu(CpuSample(99.0, ()))]
    text = build([("perf", fs, 1234)], datetime(2026, 9, 25, 10, 0), Secrets(), worse=["P1"])
    lines = [ln for ln in text.splitlines() if ln.startswith("[")]
    assert lines[0].startswith("[対処が必要]") and "(前回より悪化)" in lines[0]
    assert lines[1].startswith("[注意]") and lines[2].startswith("[問題なし]")
    assert "所要 1.2 秒" in text and "2026-09-25 10:00" in text


def test_ac5_no_names_in_history_ops_logs_diagnostics(make_module: Any, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    temp = tmp_path / f"{USER}-temp"
    temp.mkdir()
    old = temp / f"{PROC}.log"
    old.write_bytes(b"x" * 100)
    t = time.time() - 9 * 86400
    os.utime(old, (t, t))
    monkeypatch.setenv("TEMP", str(temp))

    def fake_recycle(paths: Any, _h: Any) -> RecycleResult:
        return RecycleResult(sent=[], skipped=[(p, "in_use") for p in paths])

    m, ctx, _ = make_module(_probes(), recycle_fn=fake_recycle)
    m.run(list(BY_CATEGORY))
    scans: list[Any] = []
    m.scan_temp(scans.append)
    assert scans[0] is not None and scans[0].count == 1
    results: list[Any] = []
    m.recycle_temp(scans[0], None, results.append)
    assert results[0].skipped == 1
    m.set_watch(True)
    diag = m.diagnostics()
    blobs = [
        (ctx.data_dir / "history.jsonl").read_text(encoding="utf-8"),
        (ctx.data_dir / "ops.jsonl").read_text(encoding="utf-8"),
        ctx.handler.text(),
        repr(diag),
    ]
    for b in blobs:
        low = b.lower()
        for secret in (PROC, SSID, USER, str(tmp_path), "\\users\\"):
            assert secret.lower() not in low, (secret, b[:300])
    assert '"op": "recycle_temp"' in blobs[1] and '"skipped": 1' in blobs[1]
