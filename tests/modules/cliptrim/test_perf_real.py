# AC-10: 3 時間の検体の 10000 秒の位置で、まわりの読み取り(T-3)とコマの取り出し(T-4)がそれぞれ 1 秒以内。
# 検体を作るのに時間がかかるので win32_real(既定では動かない。`pytest -m win32_real tests/modules/cliptrim`)。
from __future__ import annotations

import time
from pathlib import Path

import pytest

from deskkit.modules.cliptrim import frames, keyframes, probe

from .conftest import make_video


@pytest.mark.win32_real
def test_ac10_three_hours(ffmpeg_exe: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    d = tmp_path_factory.mktemp("long")
    # 切れ目 10 秒おき(P-9 と同じ間隔)。作る時間を抑えるため 15 コマ/秒・小さい画面・mpeg4
    src = make_video(ffmpeg_exe, d / "long.mp4", seconds=3 * 3600, codec="mpeg4", size="320x180", fps=15, gop=150,
                     timeout=1500)
    info = probe.probe(ffmpeg_exe, src)
    assert info is not None and info.duration is not None and info.duration > 10800 - 1
    t0 = time.perf_counter()
    a = keyframes.read(ffmpeg_exe, src, 10000.0, info.start)
    t_around = time.perf_counter() - t0
    t0 = time.perf_counter()
    png = frames.grab_frame(ffmpeg_exe, src, 10000.0)
    t_frame = time.perf_counter() - t0
    print(f"around={t_around:.3f}s frame={t_frame:.3f}s")
    assert a.next_key is not None and png is not None
    assert t_around < 1.0 and t_frame < 1.0
