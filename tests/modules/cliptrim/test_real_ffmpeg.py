# 同梱の ffmpeg と testsrc の合成動画で受け入れ基準を確かめる(AC-1〜AC-9・AC-12)。同梱物が無ければ skip。
# 検体は一時フォルダに作る(利用者の動画は使わない)。
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

import pytest

from deskkit.modules.cliptrim import jobs, keyframes, plan, probe, runner
from deskkit.modules.cliptrim.oplog import OpsLog

from .conftest import run_ff


def _copy_to(tmp: Path, src: Path, name: str | None = None) -> Path:
    d = tmp / "work"
    d.mkdir(parents=True, exist_ok=True)
    dst = d / (name or src.name)
    dst.write_bytes(src.read_bytes())
    return dst


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _env(exe: Path, tmp: Path, **kw: Any) -> jobs.Env:
    return jobs.Env(exe, logging.getLogger("deskkit.cliptrim"), OpsLog(tmp / "data" / "ops.jsonl"),
                    fallback_dir=lambda: tmp / "fallback", **kw)


def _export(exe: Path, tmp: Path, src: Path, segs: list[tuple[float, float]], mode: str = "copy",
            output: str = "separate", strip: bool = True, **kw: Any) -> tuple[jobs.JobResult, list[plan.Seg]]:
    info = probe.probe(exe, src)
    assert info is not None
    ss: list[plan.Seg] = []
    for s, e in segs:
        if mode == "copy":
            cs = keyframes.resolve_copy_start(exe, src, s, info.start, matroska=plan.is_matroska(info))
            ss.append(plan.Seg(s, e, cs.ss, cs.actual))
        else:
            ss.append(plan.precise_seg(s, e))
    req = jobs.ExportRequest(src, info, src.stat().st_size, mode, output, strip, tuple(ss), plan.video_kbps(info, src.stat().st_size),
                             plan.copy_container(src, info) if mode == "copy" else None)
    return jobs.run_export(jobs.Job(req), _env(exe, tmp, **kw)), ss


def _first_md5(exe: Path, path: Path, at: float | None = None) -> str:
    args = (["-ss", keyframes.fmt_ss(keyframes.floor_us(at))] if at is not None else []) + [
        "-loglevel", "error", "-i", "file:" + str(path), "-map", "0:v:0", "-frames:v", "1", "-f", "framemd5", "pipe:1"]
    r = run_ff(exe, *args)
    lines = [x for x in r.stdout.decode().splitlines() if x.startswith("0,")]
    assert lines, r.stderr.decode("utf-8", "replace")
    return lines[0].split(",")[-1].strip()


def _gray(exe: Path, path: Path, at: float | None = None) -> Any:
    import numpy as np

    args = (["-ss", keyframes.fmt_ss(keyframes.floor_us(at))] if at is not None else []) + [
        "-loglevel", "error", "-i", "file:" + str(path), "-map", "0:v:0", "-frames:v", "1", "-f", "rawvideo",
        "-pix_fmt", "gray", "pipe:1"]
    return np.frombuffer(run_ff(exe, *args).stdout, dtype=np.uint8).astype(np.float64)


def _psnr(a: Any, b: Any) -> float:
    import math

    mse = float(((a - b) ** 2).mean())
    return 99.0 if mse == 0 else 10 * math.log10(255 * 255 / mse)


def _video_packets(exe: Path, path: Path) -> list[keyframes.Packet]:
    r = run_ff(exe, "-loglevel", "error", "-i", "file:" + str(path), "-map", "0:v:0", "-c", "copy", "-f", "framecrc", "pipe:1")
    return [p for p in (keyframes.parse_line(x) for x in r.stdout.decode().splitlines()) if p is not None]


def _decode_errors(exe: Path, path: Path) -> str:
    r = run_ff(exe, "-loglevel", "error", "-i", "file:" + str(path), "-f", "null", "-")
    return r.stderr.decode("utf-8", "replace").strip()


# ------------------------------------------------------------------ AC-1
def test_ac1_copy_h264_starts_at_keyframe(ffmpeg_exe: Path, samples: dict[str, Path], tmp_path: Path) -> None:
    exe = ffmpeg_exe
    src = _copy_to(tmp_path, samples["h264"])
    before = _sha(src)
    res, segs = _export(exe, tmp_path, src, [(61.5, 71.5)])
    assert segs[0].actual == pytest.approx(60.0, abs=1e-6)
    assert segs[0].start - segs[0].actual == pytest.approx(1.5, abs=1e-6)  # 「開始が 1.5 秒早まります」
    assert res.result == "ok"
    out = res.outputs[0].path
    assert out is not None and out.name == "h264_clip.mp4"
    assert _first_md5(exe, out) == _first_md5(exe, src, 60.0)
    assert all(p.pts >= 0 for p in _video_packets(exe, out))
    assert _sha(src) == before
    assert res.outputs[0].seconds == pytest.approx(11.5, abs=0.2)


# ------------------------------------------------------------------ AC-2
def test_ac2_precise_h264(ffmpeg_exe: Path, ff_manager: Any, samples: dict[str, Path], tmp_path: Path) -> None:
    exe = ffmpeg_exe
    if not ff_manager.check_h264(exe):
        pytest.skip("この PC では h264_mf が使えません")
    src = _copy_to(tmp_path, samples["h264"])
    res, _ = _export(exe, tmp_path, src, [(61.5, 71.5)], mode="precise")
    assert res.result == "ok"
    out = res.outputs[0].path
    assert out is not None and out.suffix == ".mp4"
    first = _gray(exe, out)
    fps = 30.0
    p_at = _psnr(first, _gray(exe, src, 61.5))
    p_before = _psnr(first, _gray(exe, src, 61.5 - 1 / fps))
    p_after = _psnr(first, _gray(exe, src, 61.5 + 1 / fps))
    assert p_at > p_before and p_at > p_after
    info = probe.probe(exe, out)
    assert info is not None and info.duration == pytest.approx(10.0, abs=0.2)


# ------------------------------------------------------------------ AC-3・AC-8(B フレームのある HEVC)
@pytest.mark.parametrize("key", ["hevc_mp4", "hevc_mkv"])
def test_ac3_hevc_bframes_actual_start_matches_output(ffmpeg_exe: Path, samples: dict[str, Path], tmp_path: Path,
                                                       key: str) -> None:
    exe = ffmpeg_exe
    src = _copy_to(tmp_path, samples[key])
    res, segs = _export(exe, tmp_path, src, [(10.3, 14.0)])
    assert res.result == "ok", res.outputs
    out = res.outputs[0].path
    assert out is not None
    assert _first_md5(exe, out) == _first_md5(exe, src, segs[0].actual)
    assert _decode_errors(exe, out) == ""


def _truth_keys(exe: Path, src: Path, start: float) -> list[float]:
    r = run_ff(exe, "-loglevel", "error", "-copyts", "-i", "file:" + str(src), "-map", "0:V:0", "-c", "copy", "-f", "framecrc",
               "pipe:1")
    tb = None
    keys: list[float] = []
    for ln in r.stdout.decode().splitlines():
        tb = tb or keyframes.parse_tb(ln)
        p = keyframes.parse_line(ln)
        if p is not None and p.key and tb is not None:
            keys.append(float(p.pts * tb) - start)
    return sorted(keys)


@pytest.mark.parametrize("key", ["hevc_mp4", "hevc_mkv"])
def test_ac8_around_keys_match_full_read(ffmpeg_exe: Path, samples: dict[str, Path], key: str) -> None:
    exe = ffmpeg_exe
    src = samples[key]
    info = probe.probe(exe, src)
    assert info is not None
    truth = _truth_keys(exe, src, info.start)
    assert len(truth) > 5
    for t in (0.5, 2.2, 5.0, 10.3, 17.77, 25.0):
        a = keyframes.read(exe, src, t, info.start)
        prev = max(k for k in truth if k <= t + 1e-6)
        nxt = min((k for k in truth if k > t + 1e-6), default=None)
        k = a.key_at_or_before(t)
        assert k is not None and k.pos == pytest.approx(prev, abs=1e-6)
        if nxt is not None:
            assert a.next_key == pytest.approx(nxt, abs=1e-6)
        # コマの並びは切れ目から次の切れ目の手前まで欠けない
        inside = [f[0] for f in a.frames if prev - 1e-6 <= f[0] < (nxt or 1e9) - 1e-6]
        steps = [round(b - a_, 4) for a_, b in zip(inside, inside[1:], strict=False)]
        assert steps and max(steps) <= 2 / (info.fps or 30)


# ------------------------------------------------------------------ AC-4・AC-5
def test_ac4_copy_join(ffmpeg_exe: Path, samples: dict[str, Path], tmp_path: Path) -> None:
    exe = ffmpeg_exe
    for key, segs in (("h264", [(20.5, 25.0), (61.5, 71.5)]), ("hevc_mp4", [(3.1, 6.0), (10.3, 14.0)]),
                      ("hevc_mkv", [(3.1, 6.0), (10.3, 14.0)])):
        src = _copy_to(tmp_path / key, samples[key])
        sep, _ = _export(exe, tmp_path / key, src, segs, output="separate")
        assert sep.result == "ok"
        parts = [o.path for o in sep.outputs if o.path is not None]
        # つなぐ方法(P-6)そのものの警告を見る: 同じ一覧を warning で流す
        muxer = plan.copy_container(src, probe.probe(exe, src))[1]  # type: ignore[index]
        probe_out = tmp_path / key / "check.part"
        args = plan.concat_args(exe, probe_out, muxer, True)
        args[args.index("-loglevel") + 1] = "warning"
        args.remove("-progress")
        args.remove("pipe:1")
        cap = runner.capture(args, timeout=120, stdin=plan.concat_list(parts))
        warnings = [ln for ln in cap.stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
        # 音声の包みの時刻が 1 包み未満だけ戻る警告は、B フレームのある HEVC の MP4 で出る(ffmpeg が直す。CT-6)。映像の警告は 0
        assert cap.returncode == 0 and not [w for w in warnings if not w.startswith("[aost#")], (key, warnings)
        joined, _ = _export(exe, tmp_path / key, src, segs, output="join")
        assert joined.result == "ok", (key, joined.outputs)
        out = joined.outputs[0].path
        assert out is not None
        assert _decode_errors(exe, out) == "", key
        assert len(_video_packets(exe, out)) == sum(len(_video_packets(exe, p)) for p in parts), key


def test_ac5_precise_join(ffmpeg_exe: Path, ff_manager: Any, samples: dict[str, Path], tmp_path: Path) -> None:
    exe = ffmpeg_exe
    if not ff_manager.check_h264(exe):
        pytest.skip("この PC では h264_mf が使えません")
    src = _copy_to(tmp_path, samples["hevc_mkv"])
    res, _ = _export(exe, tmp_path, src, [(3.1, 6.0), (10.3, 14.0)], mode="precise", output="join")
    assert res.result == "ok", res.outputs
    info = probe.probe(exe, res.outputs[0].path)  # type: ignore[arg-type]
    assert info is not None and info.duration == pytest.approx(2.9 + 3.7, abs=0.2)
    assert info.audio_count == 1


# ------------------------------------------------------------------ AC-6・AC-7
def _meta(exe: Path, p: Path) -> str:
    return run_ff(exe, "-loglevel", "error", "-i", "file:" + str(p), "-f", "ffmetadata", "pipe:1").stdout.decode("utf-8", "replace")


def test_ac6_metadata_and_rotation(ffmpeg_exe: Path, ff_manager: Any, samples: dict[str, Path], tmp_path: Path) -> None:
    exe = ffmpeg_exe
    src = _copy_to(tmp_path, samples["rot"])
    assert plan.has_location(_meta(exe, src))
    strip, _ = _export(exe, tmp_path, src, [(2.0, 5.0)])
    out = strip.outputs[0].path
    assert strip.result == "ok" and out is not None
    assert not plan.has_location(_meta(exe, out))
    i = probe.probe(exe, out)
    assert i is not None and i.rotation == 90
    keep, _ = _export(exe, tmp_path, src, [(6.0, 8.0)], strip=False)
    kout = keep.outputs[0].path
    assert keep.result == "ok" and kout is not None and plan.has_location(_meta(exe, kout))
    ki = probe.probe(exe, kout)
    assert ki is not None and ki.rotation == 90
    if ff_manager.check_h264(exe):
        pr, _ = _export(exe, tmp_path, src, [(2.0, 5.0)], mode="precise")
        pout = pr.outputs[0].path
        assert pr.result == "ok" and pout is not None
        pi = probe.probe(exe, pout)
        assert pi is not None and (pi.width, pi.height) == (108, 192) and pi.rotation == 0
        assert not plan.has_location(_meta(exe, pout))


def test_ac7_mkv_two_audio_and_subtitles(ffmpeg_exe: Path, samples: dict[str, Path], tmp_path: Path) -> None:
    exe = ffmpeg_exe
    src = _copy_to(tmp_path, samples["multi"])
    si = probe.probe(exe, src)
    assert si is not None and si.audio_count == 2 and si.subtitle_count == 1
    res, _ = _export(exe, tmp_path, src, [(3.0, 7.0)])
    assert res.result == "ok", res.outputs
    i = probe.probe(exe, res.outputs[0].path)  # type: ignore[arg-type]
    assert i is not None and i.has_video and i.audio_count == 2 and i.subtitle_count == 0


# ------------------------------------------------------------------ AC-9・AC-12
def test_ac9_cancel_leaves_no_child_and_no_part(ffmpeg_exe: Path, ff_manager: Any, samples: dict[str, Path],
                                                 tmp_path: Path) -> None:
    import threading
    import time

    exe = ffmpeg_exe
    src = _copy_to(tmp_path, samples["h264"])
    info = probe.probe(exe, src)
    assert info is not None
    mode = "precise" if ff_manager.check_h264(exe) else "copy"
    seg = plan.precise_seg(0.0, 119.0) if mode == "precise" else plan.Seg(0.0, 119.0, 0.0, 0.0)
    req = jobs.ExportRequest(src, info, src.stat().st_size, mode, "separate", True, (seg,), 20000,
                             plan.copy_container(src, info) if mode == "copy" else None)
    made: list[runner.Runner] = []

    def factory() -> runner.Runner:
        r = runner.Runner()
        made.append(r)
        return r

    job = jobs.Job(req)
    th = threading.Thread(target=jobs.run_export, args=(job, _env(exe, tmp_path, runner_factory=factory)))
    th.start()
    t0 = time.monotonic()
    while not made or made[0]._proc is None:
        assert time.monotonic() - t0 < 30
        time.sleep(0.01)
    job.cancel()
    th.join(30)
    assert not th.is_alive()
    assert job.result is not None and job.result.result in ("cancelled", "ok")
    assert made[0]._proc.poll() is not None  # 子プロセスは終わっている
    assert not list(src.parent.glob("*.part"))


def test_ac12_join_writes_only_parts(ffmpeg_exe: Path, samples: dict[str, Path], tmp_path: Path) -> None:
    exe = ffmpeg_exe
    src = _copy_to(tmp_path, samples["h264"])
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    seen: list[set[str]] = []

    class Watch(runner.Runner):
        def run(self, args: list[str], **kw: Any) -> runner.RunResult:
            seen.append({p.name for p in src.parent.iterdir()} | {"data/" + p.name for p in data.iterdir()})
            return super().run(args, **kw)

    res, _ = _export(exe, tmp_path, src, [(20.5, 25.0), (61.5, 71.5)], output="join", runner_factory=Watch)
    assert res.result == "ok"
    for names in seen:
        extra = {n for n in names if n != src.name and not n.startswith("data/ops.jsonl")}
        assert all(n.endswith(".part") and ".cliptrim-" in n for n in extra), extra
    assert {p.name for p in src.parent.iterdir()} == {src.name, "h264_clip.mp4"}
