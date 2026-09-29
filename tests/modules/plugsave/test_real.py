# 実機の Win32 を読むだけのテスト(win32_real。既定では走らない)。ドライブには何も書かない。
# USB の抜き差し(AC-23)は手で確かめる: docs/v0.4/plugsave.md の「実機で確かめること」。
from __future__ import annotations

import pytest

from deskkit.modules.plugsave import _win32


@pytest.mark.win32_real
def test_real_drive_api_reads_only() -> None:
    api = _win32.RealDriveApi()
    letters = _win32.letters_of(api.logical_drives())
    assert letters
    old = api.set_thread_error_mode(_win32.SEM_FAILCRITICALERRORS)
    try:
        for lt in letters:
            root = api.root(lt)
            t = api.drive_type(root)
            assert 0 <= t <= 6
            if t in _win32.BACKUP_DRIVE_TYPES:
                v = api.volume_info(root)   # 読むだけ(ラベルは出さない)
                if v is not None:
                    assert v.fs
                    total, free = api.disk_usage(root)
                    assert total >= free >= 0
                    print(lt, "type", t, "fs", v.fs)
    finally:
        if old >= 0:
            api.set_thread_error_mode(old)
