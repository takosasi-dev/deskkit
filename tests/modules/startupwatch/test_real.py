# 実機の Win32 に依存する StartupWatch のテスト(既定では実行しない: pytest -m win32_real)。
# test_real_read_only は読むだけ。test_ac14_* は HKCU の Run と利用者のスタートアップ フォルダに試しの物を置いて、必ず消す
# (§8・AC-14。置く物は存在しないプログラムを指すので、消し忘れてもサインインで何も起動しない)。
from __future__ import annotations

import os
import time
import winreg
from pathlib import Path
from typing import Any

import pytest

from deskkit.modules.startupwatch import sources
from deskkit.modules.startupwatch._win32 import RealApi

pytestmark = pytest.mark.win32_real
TEST_NAME = "DeskKitStartupWatchTest"
TEST_CMD = r'"C:\DeskKitStartupWatchTest\does-not-exist.exe" --test'


def test_real_read_only() -> None:
    api = RealApi()
    res = sources.read_all(api, set())
    assert res is not None
    assert set(res.locs) == {loc.key for loc in sources.LOCATIONS}
    assert res.locs["hkcu_run"].status in ("ok", "missing")
    assert res.ms < 2000


def _wait(pred: Any, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_ac14_hkcu_run_and_startup_folder_notify_within_10s(make_module: Any) -> None:
    from PySide6.QtCore import QCoreApplication

    api = RealApi()
    m0, ctx, _ = make_module(api)
    m0.start()  # 記録が無いので覚えるだけ
    m0.stop()
    m, _, _ = make_module(api, ctx, threaded=True)
    m.start()
    assert _wait(lambda: m.scans >= 1, 5)
    run = r"Software\Microsoft\Windows\CurrentVersion\Run"
    folder = api.known_folder("startup")
    assert folder
    probe = Path(folder) / f"{TEST_NAME}.txt"
    try:
        t0 = time.monotonic()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, TEST_NAME, 0, winreg.REG_SZ, TEST_CMD)
        assert _wait(lambda: len(ctx.notifications) >= 1, 10), "Run: 10 秒以内に知らせが無い"
        reg_s = time.monotonic() - t0
        t1 = time.monotonic()
        probe.write_text("DeskKit StartupWatch test", encoding="utf-8")
        assert _wait(lambda: len(ctx.notifications) >= 2, 10), "フォルダ: 10 秒以内に知らせが無い"
        folder_s = time.monotonic() - t1
        print(f"notify latency: run={reg_s:.2f}s folder={folder_s:.2f}s")
    finally:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, TEST_NAME)
        except OSError:
            pass
        if probe.exists():
            os.remove(probe)
        m.stop()
        QCoreApplication.processEvents()
