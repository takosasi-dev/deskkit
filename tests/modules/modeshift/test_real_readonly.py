# 実機での読み取り専用の確認(win32_real)。電源プラン・音量は読むだけで変えない。アプリも起動・終了しない。
# 実行: pytest tests/modules/modeshift -m win32_real
from __future__ import annotations

import threading

import pytest

pytestmark = pytest.mark.win32_real


def test_real_powercfg_list_and_active() -> None:
    from deskkit.modules.modeshift.actions.power import PowercfgPower

    p = PowercfgPower()
    schemes = p.list_schemes()
    active = p.get_active()
    assert schemes, "powercfg /list から GUID を1つも取れない"
    assert active is not None and active in {s.guid for s in schemes}
    assert sum(1 for s in schemes if s.active) == 1


def test_real_master_volume_and_sessions_read_only() -> None:
    from deskkit.modules.modeshift.actions.audio import CoreAudio

    a = CoreAudio()
    m = a.get_master()
    if m is None:
        pytest.skip("既定の再生デバイスが無い")
    assert 0.0 <= m.level <= 1.0 and isinstance(m.mute, bool) and m.device_id
    sessions = a.list_sessions()
    assert all(0.0 <= s.level <= 1.0 and s.pid > 0 and s.session_id for s in sessions)
    # 別スレッド(MTA)からも同じ値が読める(COM 初期化の確認)
    box: dict[str, object] = {}
    t = threading.Thread(target=lambda: box.update(m=a.get_master()))
    t.start()
    t.join(10)
    assert box.get("m") == m


def test_real_process_list() -> None:
    import os

    from deskkit.modules.modeshift._win32 import exe_path, list_processes

    procs = list_processes()
    me = [p for p in procs if p.pid == os.getpid()]
    assert me and me[0].exe.endswith(".exe")
    assert exe_path(os.getpid())
