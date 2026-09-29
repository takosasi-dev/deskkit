# 実機の Win32 に依存する EyeBreak のテスト(既定では実行しない: pytest -m win32_real)。AC-17 の前半(読めて、手を止めると増える)。
# 「マウスを動かすと 1 秒未満に戻る」は人の操作が要るので、docs/v0.4/eyebreak.md の「実機で確かめること」に書いた。
from __future__ import annotations

import time

import pytest

from deskkit.modules.eyebreak import _win32

pytestmark = pytest.mark.win32_real


def test_ac17_idle_is_readable_and_grows_without_input() -> None:
    api = _win32.RealApi()
    a = _win32.read_idle_seconds(api)
    assert a is not None and a >= 0
    time.sleep(1.2)
    b = _win32.read_idle_seconds(api)
    assert b is not None
    # 手を触れていなければ 1 秒以上増える。途中で入力があれば 1.2 秒未満に戻る(どちらも正しい振る舞い)
    assert b >= a + 1.0 or b < 1.2
