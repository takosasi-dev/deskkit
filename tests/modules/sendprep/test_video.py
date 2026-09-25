# 動画(S-8・S-9・FR-14〜16・INV-3)。前半は ffmpeg なしで引数・計画・展開と照合、後半は開発用の ffmpeg で実際に動かす。
# AC-9(10 秒の testsrc を 1MB 以下・location なし)・AC-10(中止で子プロセスも出力も残らない)。
from __future__ import annotations

import hashlib
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

from deskkit.modules.sendprep import jobs as J
from deskkit.modules.sendprep import video as V

from .conftest import ListHandler, jpeg_with_meta, run_all

STDERR = """Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'file:x.mov':
  Duration: 00:01:02.50, start: 0.000000, bitrate: 6368 kb/s
  Stream #0:0[0x1]: Video: hevc (Main) (hvc1 / 0x31637668), yuv420p(tv, bt709), 1920x1080, 6232 kb/s, 30 fps (default)
      Side data:
        displaymatrix: rotation of -90.00 degrees
  Stream #0:1[0x2]: Audio: aac (LC) (mp4a / 0x6134706D), 48000 Hz, stereo, fltp, 127 kb/s (default)
  Stream #0:2[0x3]: Data: none (mebx / 0x7862656D)
"""


def test_parse_probe() -> None:
    p = V.parse_probe(STDERR)
    assert p.duration == pytest.approx(62.5) and p.has_video and p.has_audio and (p.width, p.height) == (1920, 1080)
    q = V.parse_probe("  Duration: N/A, bitrate: N/A\n  Stream #0:0: Video: h264, yuv420p, 640x480\n")
    assert q.duration is None and q.has_video and not q.has_audio
    assert not V.parse_probe("garbage").has_video


def test_plan_thresholds_and_too_long() -> None:
    mb = 1_000_000
    p = V.make_plan(10 * mb, 60, True)  # 10MB・1分: 1266kbps − 128 → 720p
    assert p.short_side == 720 and 1100 < p.video_kbps < 1200 and p.audio_kbps == 128
    assert V.make_plan(10 * mb, 20, True).short_side is None
    assert V.make_plan(10 * mb, 150, False).short_side == 480
    with pytest.raises(V.VideoError) as ei:
        V.make_plan(1 * mb, 60, True)
    assert ei.value.result == "too_large" and ei.value.message.startswith("長すぎて収まりません(この上限だと約 ")
    assert V.too_long_message(10 * mb, 128) == "長すぎて収まりません(この上限だと約 3 分まで)"
    assert "秒まで" in V.too_long_message(1 * mb, 128)


def _check_inv3(args: list[str]) -> None:
    i = args.index("-i")
    assert args[i - 2:i] == ["-protocol_whitelist", "file,pipe"]
    assert args[i + 1].startswith(("file:", "pipe:"))
    assert Path(args[0]).name == "ffmpeg.exe" and Path(args[0]).is_absolute()


def test_args_inv3(tmp_path: Path) -> None:
    exe = tmp_path / "ffmpeg.exe"
    src = tmp_path / "-rf.mov"
    dst = tmp_path / "-o.mp4"
    plan = V.Plan(800, 128, 720)
    for a in (V.remux_args(exe, src, dst, "mov"), V.encode_args(exe, src, dst, plan, True), V.metadata_args(exe, src)):
        _check_inv3(a)
        assert all(not x.startswith("-rf") and not x.startswith("-o.") for x in a)
    enc = V.encode_args(exe, src, dst, plan, True)
    assert enc[-1] == "file:" + str(dst) and "h264_mf" in enc and "-map_metadata" in enc and "aac" in enc
    assert "-map_metadata" in V.remux_args(exe, src, dst, "mov") and "copy" in V.remux_args(exe, src, dst, "mov")
    assert "720" in V.scale_filter(720) and V.has_location("location-eng=+35/\n") and V.has_location("com.apple.quicktime.location.ISO6709=x")
    assert not V.has_location(";FFMETADATA1\nencoder=Lavf\n")


def _fake_bundle(d: Path, content: bytes = b"MZfake-ffmpeg", sha: str | None = None) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(d / "ffmpeg.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("ffmpeg.exe", content)
        z.writestr("LICENSE.txt", "LGPL")
    (d / "ffmpeg.sha256").write_text((sha or hashlib.sha256(content).hexdigest()) + "\n", encoding="ascii")
    return d


def test_manager_states(tmp_path: Path) -> None:
    missing = V.FfmpegManager(tmp_path / "a", lambda: None)
    assert missing.state() == V.STATE_MISSING
    with pytest.raises(V.VideoError) as ei:
        missing.ensure()
    assert ei.value.message == V.MSG_MISSING
    good = _fake_bundle(tmp_path / "good")
    m = V.FfmpegManager(tmp_path / "b", lambda: good)
    assert m.state() == V.STATE_NOT_EXTRACTED
    calls: list[int] = []
    exe = m.ensure(lambda: calls.append(1))
    assert exe.read_bytes() == b"MZfake-ffmpeg" and calls == [1] and m.state() == V.STATE_EXTRACTED
    assert (exe.parent / "LICENSE.txt").exists()
    # 起動し直して(新しい管理者)照合: 改ざんされていれば展開し直す
    exe.write_bytes(b"tampered")
    m2 = V.FfmpegManager(tmp_path / "b", lambda: good)
    assert m2.ensure(lambda: calls.append(2)).read_bytes() == b"MZfake-ffmpeg" and calls == [1, 2]
    bad = _fake_bundle(tmp_path / "bad", sha="0" * 64)
    m3 = V.FfmpegManager(tmp_path / "c", lambda: bad)
    with pytest.raises(V.VideoError) as ei2:
        m3.ensure()
    assert ei2.value.message == V.MSG_BROKEN and m3.state() == V.STATE_VERIFY_FAILED
    assert not list((tmp_path / "c").rglob("ffmpeg.exe"))


def test_bundled_dir_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    mei = tmp_path / "mei"
    _fake_bundle(mei / "deskkit" / "_bundled")
    monkeypatch.setattr(sys, "_MEIPASS", str(mei), raising=False)
    assert V.bundled_dir() == mei / "deskkit" / "_bundled"
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "none"), raising=False)
    got = V.bundled_dir()
    assert got is None or got.name == "_bundled"


def test_fr16_broken_bundle_video_fails_image_continues(make_module: Any, tmp_path: Path) -> None:
    bad = _fake_bundle(tmp_path / "bad", sha="1" * 64)
    vid = tmp_path / "clip.mp4"
    vid.write_bytes(b"\x00\x00\x00\x18ftypisom")
    img = tmp_path / "p.jpg"
    img.write_bytes(jpeg_with_meta())
    m, ctx = make_module(bundle=lambda: bad)
    m.add_paths([vid, img])
    m.start_processing("meta")
    run_all(m, ctx)
    jv, ji = m.jobs
    assert jv.state == J.STATE_FAILED and jv.message == "動画の部品が壊れています。DeskKit を入れ直してください"
    assert ji.state == J.STATE_DONE
    assert m.diagnostics()["ffmpeg"] == V.STATE_VERIFY_FAILED


# ------------------------------------------------------------------ 実際の ffmpeg
def _make_video(exe: Path, out: Path, seconds: int, size: str = "1280x720", bitrate: str = "4M", audio: bool = True) -> Path:
    args = [str(exe), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    args += ["-t", str(seconds), "-c:v", "h264_mf", "-b:v", bitrate]
    if audio:
        args += ["-c:a", "aac", "-b:a", "128k"]
    args += ["-metadata", "location=+35.6812+139.7671/", "-metadata", "com.apple.quicktime.location.ISO6709=+35.6812+139.7671/",
             "-metadata", "title=secret-title", "-movflags", "use_metadata_tags", str(out)]
    subprocess.run(args, check=True, capture_output=True, timeout=300)
    return out


def _meta(exe: Path, p: Path) -> str:
    r = subprocess.run(V.metadata_args(exe, p), capture_output=True, timeout=60)
    return r.stdout.decode("utf-8", "replace")


def test_ac9_encode_to_1mb(make_module: Any, ffmpeg_exe: Path, shared_ffmpeg: Any, tmp_path: Path, logs: ListHandler) -> None:
    src = _make_video(ffmpeg_exe, tmp_path / "-家族旅行.mov", 10)
    assert src.stat().st_size > 1_000_000 and "location" in _meta(ffmpeg_exe, src)
    section = {"presets": [{"id": "custom-1", "label": "1MB", "limit_mb": 1}]}
    m, ctx = make_module(section, data_dir=shared_ffmpeg.data, bundle=lambda: shared_ffmpeg.bundle)
    m.add_paths([src])
    m.start_processing("custom-1")
    run_all(m, ctx, timeout=300)
    j = m.jobs[0]
    assert j.state == J.STATE_DONE, j.message
    assert j.out_path is not None and j.out_path.name == "-家族旅行_share.mp4"
    assert j.out_path.stat().st_size <= 1_000_000
    md = _meta(ffmpeg_exe, j.out_path)
    assert not V.has_location(md) and "secret-title" not in md
    assert m.ffmpeg.h264_state() == "available"
    assert "家族旅行" not in logs.text() and "家族旅行" not in (shared_ffmpeg.data / "ops.jsonl").read_text(encoding="utf-8")


def test_s9_remux_keeps_quality(make_module: Any, ffmpeg_exe: Path, shared_ffmpeg: Any, tmp_path: Path) -> None:
    src = _make_video(ffmpeg_exe, tmp_path / "a.mov", 3, "640x360", "1M")
    m, ctx = make_module(data_dir=shared_ffmpeg.data, bundle=lambda: shared_ffmpeg.bundle)
    m.add_paths([src])
    m.start_processing("discord")  # 10MB 以下なので作り直さない(FR-8)
    run_all(m, ctx, timeout=120)
    j = m.jobs[0]
    assert j.state == J.STATE_DONE, j.message
    assert j.out_path is not None and j.out_path.suffix == ".mov"
    assert not V.has_location(_meta(ffmpeg_exe, j.out_path))
    assert abs(j.out_path.stat().st_size - src.stat().st_size) < src.stat().st_size * 0.05
    assert j.result_text().startswith("位置情報なし・")


def test_too_long_message(make_module: Any, ffmpeg_exe: Path, shared_ffmpeg: Any, tmp_path: Path) -> None:
    src = _make_video(ffmpeg_exe, tmp_path / "long.mp4", 40, "640x360", "600k")
    section = {"presets": [{"id": "custom-1", "label": "1MB", "limit_mb": 1}]}
    m, ctx = make_module(section, data_dir=shared_ffmpeg.data, bundle=lambda: shared_ffmpeg.bundle)
    m.add_paths([src])
    m.start_processing("custom-1")
    run_all(m, ctx, timeout=120)
    j = m.jobs[0]
    assert j.state == J.STATE_FAILED and j.message.startswith("長すぎて収まりません(この上限だと約 ") and j.result == "too_large"
    assert not list(tmp_path.glob("long_share*"))


def test_ac10_cancel(make_module: Any, ffmpeg_exe: Path, shared_ffmpeg: Any, tmp_path: Path) -> None:
    src = _make_video(ffmpeg_exe, tmp_path / "c.mp4", 60, "1920x1080", "8M")
    section = {"presets": [{"id": "custom-1", "label": "50MB", "limit_mb": 50}]}
    m, ctx = make_module(section, data_dir=shared_ffmpeg.data, bundle=lambda: shared_ffmpeg.bundle)
    m.add_paths([src])
    m.start_processing("custom-1")
    j = m.jobs[0]
    proc = None
    t0 = time.monotonic()
    while time.monotonic() - t0 < 60:
        r = j.runner
        if r is not None and r.process is not None and (j.progress or 0) > 0.02:
            proc = r.process
            break
        time.sleep(0.02)
    assert proc is not None, "エンコードが始まらない"
    m.cancel_job(j)
    run_all(m, ctx, timeout=60)
    assert proc.poll() is not None  # 子プロセスが残らない
    assert j.state == J.STATE_CANCELLED and j.result == "cancelled"
    assert not list(tmp_path.glob("c_share*"))
    assert m.ops.read()[-1]["result"] == "cancelled"


def test_stop_kills_ffmpeg(make_module: Any, ffmpeg_exe: Path, shared_ffmpeg: Any, tmp_path: Path) -> None:
    src = _make_video(ffmpeg_exe, tmp_path / "s.mp4", 60, "1920x1080", "8M")
    section = {"presets": [{"id": "custom-1", "label": "50MB", "limit_mb": 50}]}
    m, ctx = make_module(section, data_dir=shared_ffmpeg.data, bundle=lambda: shared_ffmpeg.bundle)
    m.add_paths([src])
    m.start_processing("custom-1")
    j = m.jobs[0]
    t0 = time.monotonic()
    while time.monotonic() - t0 < 60 and not (j.runner is not None and j.runner.process is not None):
        time.sleep(0.02)
    proc = j.runner.process if j.runner is not None else None
    m.stop()
    assert proc is None or proc.poll() is not None
    assert not list(tmp_path.glob("s_share*"))
