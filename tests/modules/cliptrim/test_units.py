# ClipTrim の純粋な部品: framecrc の行(AC-8)・ffmpeg -i の情報・まわりの読み取りの止め方(T-3)・引数の安全策(AC-12)・
# 見積もりと空き容量の文言(FR-11)・名前(T-11)・時刻の読み方(FR-3)・設定(§9 と回答 Q-2)・フィルムストリップの位置(FR-6)。
from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from deskkit.modules.cliptrim import config as C
from deskkit.modules.cliptrim import frames, plan, probe
from deskkit.modules.cliptrim import keyframes as K


# ------------------------------------------------------------------ framecrc(AC-8)
@pytest.mark.parametrize(("line", "key"), [
    ("0,      61440,      61440,      512,     5227, 0x4a970217", True),                       # F= 無し
    ("0,      61952,      61952,      512,      906, 0x8218e2d5, F=0x1", True),
    ("0,      61952,      61952,      512,      906, 0x8218e2d5, F=0x0", False),
    ("0,      -1024,      -1024,     1024,      280, 0xefb87a5c, F=0x5, S=1, Skip Samples,       10, 0x00240004", True),
    ("0,      -4096,          0,      512,     4750, 0x82ec0778", True),                        # 負の dts
])
def test_parse_line_flags(line: str, key: bool) -> None:
    pk = K.parse_line(line)
    assert pk is not None and pk.key is key


def test_parse_line_other_streams_and_headers() -> None:
    assert K.parse_line("1,          0,          0,     1024,      203, 0xb1ee60de") is None
    assert K.parse_line("#software: Lavf62.12.103") is None
    assert K.parse_line("") is None
    pk = K.parse_line("0,      -4096,          0,      512,     4750, 0x82ec0778")
    assert pk is not None and pk.dts == -4096 and pk.pts == 0 and pk.dur == 512


def test_parse_tb() -> None:
    assert K.parse_tb("#tb 0: 1/15360") == Fraction(1, 15360)
    assert K.parse_tb("#tb 1: 1/44100") is None
    assert K.parse_tb("#tb 0: 1/0") is None


def _lines(packets: list[tuple[int, int, bool]], tb: int = 1000, dur: int = 40) -> list[str]:
    out = [f"#tb 0: 1/{tb}"]
    for dts, pts, key in packets:
        out.append(f"0, {dts}, {pts}, {dur}, 100, 0x0" + ("" if key else ", F=0x0"))
    return out


def _scan(lines: list[str], t: float, start: float = 0.0) -> tuple[K.Around, int]:
    sc = K.Scanner(t, start)
    used = 0
    stopped = False
    for ln in lines:
        used += 1
        if not sc.feed(ln):
            stopped = True
            break
    return sc.result(eof=not stopped), used


def test_scanner_stops_after_next_key_once_frames_complete() -> None:
    # B フレームあり: 切れ目 0 と 2000、2000 の前のコマ(pts 1960)は 2000 の後に来る
    pk = [(-80, 0, True), (-40, 120, False), (0, 40, False), (40, 80, False), (80, 1960, False), (1920, 2080, True),
          (1960, 2000, False), (2000, 2040, False), (2040, 2200, False), (2080, 2120, False)]
    # 2つ目の切れ目は pts 2080(> t)。dts 2080 の包みで止まる
    a, used = _scan(_lines(pk), t=1.0)
    assert a.first is not None and a.first.pos == 0.0
    assert a.next_key == pytest.approx(2.08)
    assert used == len(pk) + 1 and not a.eof  # 最後の包み(dts 2080 ≥ 次の切れ目)で止まる
    assert [round(f[0], 3) for f in a.frames if f[0] < 2.08] == [0.0, 0.04, 0.08, 0.12, 1.96, 2.0, 2.04]
    assert a.frame_at(1.0) is not None and a.frame_at(1.0)[0] == pytest.approx(1.96)  # type: ignore[index]
    assert a.next_frame(0.0) == pytest.approx((0.04, 0.04))
    assert a.prev_frame(0.04) == pytest.approx((0.0, 0.04))
    assert a.prev_frame(0.0) is None
    assert a.prev_key(2.08) is not None and a.prev_key(2.08).pos == 0.0  # type: ignore[union-attr]
    assert a.covers(1.0) and not a.covers(2.08)


def test_scanner_far_key_stops_immediately() -> None:
    a, used = _scan(_lines([(0, 0, True), (40, 40, False)]), t=200.0)
    assert a.far and used == 2 and a.first is not None and a.first.pos == 0.0 and not a.covers(200.0)


def test_scanner_horizon() -> None:
    pk = [(i * 1000, i * 1000, i == 0) for i in range(80)]
    a, _ = _scan(_lines(pk), t=10.0)
    assert a.horizon and a.next_key is None
    assert all(f[0] <= 70.0 + 1e-6 for f in a.frames)


def test_scanner_start_offset_and_first_only() -> None:
    sc = K.Scanner(0.0, 1.4, first_only=True)
    for ln in _lines([(1400, 1400, True), (1440, 1440, False)]):
        if not sc.feed(ln):
            break
    a = sc.result(eof=False)
    assert a.first is not None and a.first.pos == pytest.approx(0.0)


def test_rounding_helpers() -> None:
    assert K.floor_us(61.4666666666) == 61.466666
    assert K.round_us(61.4666666666) == 61.466667
    assert K.fmt_ss(2.0) == "2.000000"


# ------------------------------------------------------------------ ffmpeg -i の情報
STDERR = """Input #0, matroska,webm, from 'file:C:\\secret\\a.mkv':
  Metadata:
    ENCODER         : Lavf62.12.103
  Duration: 00:00:10.02, start: 1.400000, bitrate: 198 kb/s
  Stream #0:0: Video: mjpeg (Baseline), yuvj420p, 300x300, 90k tbr, 90k tbn (attached pic)
  Stream #0:1: Video: h264 (High), yuv420p(tv, bt709, progressive), 1920x1080 [SAR 1:1 DAR 16:9], 59.94 fps, 59.94 tbr, 1k tbn, start 1.900000
    Side data:
      displaymatrix: rotation of 90.00 degrees
  Stream #0:2(jpn): Audio: aac (LC), 48000 Hz, stereo, fltp (default)
  Stream #0:3: Subtitle: subrip (srt)
At least one output file must be specified
"""


def test_probe_parse_mkv() -> None:
    i = probe.parse(STDERR)
    assert i.formats == ("matroska", "webm") and i.duration == pytest.approx(10.02) and i.start == pytest.approx(1.4)
    assert i.has_video and i.vcodec == "h264" and (i.width, i.height) == (1920, 1080)
    assert i.rotation == 90 and i.display_size == (1080, 1920)
    assert i.fps == pytest.approx(59.94) and i.audio_count == 1 and i.subtitle_count == 1 and not i.hdr
    assert i.video_start == pytest.approx(1.9) and i.video_extent == pytest.approx(10.02 - 0.5)


def test_probe_parse_no_video_and_no_duration() -> None:
    i = probe.parse("Input #0, mp3, from 'x':\n  Duration: N/A, bitrate: N/A\n  Stream #0:0: Audio: mp3, 44100 Hz\n")
    assert not i.has_video and i.duration is None and i.audio_count == 1


# ------------------------------------------------------------------ 引数(AC-12・INV-2)
EXE = Path("C:/app/ffmpeg.exe")
SRC = Path("C:/videos/-秘密 it's.mp4")
OUT = Path("C:/videos/-秘密 it's_clip.mp4.cliptrim-0a1b2c3d.part")


def _all_args() -> list[list[str]]:
    seg = plan.Seg(61.5, 71.5, 60.0, 60.0)
    segs = [seg, plan.Seg(80.0, 90.0, 80.0, 80.0)]
    return [plan.copy_args(EXE, SRC, seg, OUT, "mp4", True), plan.copy_args(EXE, SRC, seg, OUT, "matroska", False),
            plan.precise_args(EXE, SRC, seg, OUT, 3000, 1, True), plan.precise_args(EXE, SRC, seg, OUT, 3000, 0, False),
            plan.precise_join_args(EXE, SRC, segs, OUT, 3000, 2, True), plan.concat_args(EXE, OUT, "mp4", True),
            plan.framecrc_args(EXE, OUT), plan.ffmetadata_args(EXE, OUT), K.around_args(EXE, SRC, 61.5),
            probe.probe_args(EXE, SRC), frames.frame_args(EXE, SRC, 61.5), frames.thumb_args(EXE, SRC, 61.5)]


def test_every_input_has_whitelist_and_user_paths_have_file_prefix() -> None:
    for a in _all_args():
        assert a[0] == str(EXE) and "-nostdin" in a
        n_inputs = 0
        for i, x in enumerate(a):
            if x == "-i":
                n_inputs += 1
                assert a[i - 2:i] == ["-protocol_whitelist", "file,pipe"], a
                assert a[i + 1].startswith("file:") or a[i + 1] == "pipe:0"
        assert n_inputs >= 1
        for x in a[1:]:
            if "秘密" in x:
                assert x.startswith("file:"), x


def test_copy_args_shape() -> None:
    a = plan.copy_args(EXE, SRC, plan.Seg(61.5, 71.5, 60.0, 60.0), OUT, "mp4", True)
    i = a.index("-i")
    assert a[a.index("-ss") + 1] == "60.000000" and a.index("-ss") < i and a[a.index("-t") + 1] == "11.500000"
    assert a[a.index("-map") + 1] == "0:V:0" and "0:a?" in a and a[a.index("-c") + 1] == "copy"
    assert "-map_metadata" in a and "-map_chapters" in a and a[-3:-1] == ["-f", "mp4"]
    keep = plan.copy_args(EXE, SRC, plan.Seg(61.5, 71.5, 60.0, 60.0), OUT, "mp4", False)
    assert "-map_metadata" not in keep and "-map_chapters" in keep


def test_precise_join_graph() -> None:
    segs = [plan.Seg(1, 2, 1, 1), plan.Seg(5, 7, 5, 5)]
    a = plan.precise_join_args(EXE, SRC, segs, OUT, 3000, 2, True)
    g = a[a.index("-filter_complex") + 1]
    assert "[v0][0:a:0][0:a:1][v1][1:a:0][1:a:1]concat=n=2:v=1:a=2[v][a0][a1]" in g
    assert a.count("-i") == 2 and "h264_mf" in a and a.count("-b:a") == 1


def test_concat_list_quotes_and_inpoint() -> None:
    txt = plan.concat_list([Path("C:/d/it's 日本.part"), Path("C:/d/b.part")]).decode("utf-8")
    assert txt.startswith("ffconcat version 1.0\n")
    assert "file 'file:C:\\d\\it'\\''s 日本.part'\ninpoint 0\n" in txt
    assert txt.count("inpoint 0") == 2


def test_video_kbps() -> None:
    base = probe.VideoInfo(100.0, 0.0, ("mov",), True, "h264", video_kbps=6000, audio_count=1, audio_kbps=(128,))
    assert plan.video_kbps(base, 0) == 6000
    hevc = probe.VideoInfo(100.0, 0.0, ("mov",), True, "hevc", video_kbps=6000)
    assert plan.video_kbps(hevc, 0) == 9000
    none = probe.VideoInfo(100.0, 0.0, ("mov",), True, "h264", audio_count=1, audio_kbps=(None,))
    assert plan.video_kbps(none, 100 * 1000 * 1000 // 8 * 3) == 3000 - 128
    assert plan.video_kbps(probe.VideoInfo(100.0, 0.0, (), True, "h264", video_kbps=100), 0) == 500
    assert plan.video_kbps(probe.VideoInfo(100.0, 0.0, (), True, "av1", video_kbps=40000), 0) == 50000


def test_copy_container() -> None:
    mp4 = probe.VideoInfo(1.0, 0.0, ("mov", "mp4", "m4a"), True)
    mkv = probe.VideoInfo(1.0, 0.0, ("matroska", "webm"), True)
    assert plan.copy_container(Path("a.MP4"), mp4) == (".mp4", "mp4")
    assert plan.copy_container(Path("a.m4v"), mp4) == (".mp4", "mp4")
    assert plan.copy_container(Path("a.mov"), mp4) == (".mov", "mov")
    assert plan.copy_container(Path("a.mkv"), mkv) == (".mkv", "matroska")
    assert plan.copy_container(Path("a.webm"), mkv) == (".webm", "webm")
    assert plan.copy_container(Path("a.avi"), probe.VideoInfo(1.0, 0.0, ("avi",), True)) is None
    assert plan.copy_container(Path("a.mp4"), mkv) is None  # 拡張子と中身が違う
    assert plan.is_matroska(mkv) and not plan.is_matroska(mp4)


# ------------------------------------------------------------------ 見積もり(FR-11)
def test_estimates_and_space_problem() -> None:
    assert plan.estimate_copy(1_000_000_000, 30, 3600, False) == int(1_000_000_000 * 30 / 3600 * 1.05)
    assert plan.estimate_copy(1_000_000_000, 30, 3600, True) == 2 * int(1_000_000_000 * 30 / 3600 * 1.05)
    assert plan.estimate_precise(6000, 1, 60) == int((6000 + 128) * 1000 * 60 / 8 * 1.3)
    assert plan.space_problem([50_000_000], 10**12, "NTFS") is None
    msg = plan.space_problem([2_000_000_000], 1_000_000_000, "NTFS")
    assert msg == "空き容量が足りません(あと約 1.1 GB)"
    assert plan.space_problem([5 * 1024**3], 10**13, "FAT32") == "このドライブには 4GB を超えるファイルを書けません"
    assert plan.space_problem([3 * 1024**3], 10**13, "FAT32") is None
    assert plan.space_problem([5 * 1024**3], 10**13, "exFAT") is None


# ------------------------------------------------------------------ 名前・時刻
def test_names() -> None:
    assert plan.out_stems(Path("C:/v/録画.mp4"), 1) == ["録画_clip"]
    assert plan.out_stems(Path("C:/v/録画.mp4"), 3) == ["録画_clip1", "録画_clip2", "録画_clip3"]
    c = plan.name_candidates("録画_clip", ".mp4")
    assert c[0] == "録画_clip.mp4" and c[1] == "録画_clip (2).mp4" and c[-1] == "録画_clip (1000).mp4"
    assert plan.part_name("a_clip.mp4", "0a1b2c3d") == "a_clip.mp4.cliptrim-0a1b2c3d.part"
    assert plan.part_name("a_clip.mp4", "0a1b2c3d", 2) == "a_clip.mp4.cliptrim-0a1b2c3d-2.part"
    import re

    assert re.fullmatch(plan.PART_RE, "a_clip.mp4.cliptrim-0a1b2c3d-2.part")
    assert not re.fullmatch(plan.PART_RE, "a_clip.mp4.cliptrim-0A1B2C3D.part")
    assert not re.fullmatch(plan.PART_RE, "a_clip.mp4.part")


@pytest.mark.parametrize(("text", "sec"), [("1:02:10.5", 3730.5), ("2:30", 150.0), ("90", 90.0), ("0.5", 0.5),
                                           ("1：02", 62.0), ("", None), ("1:60", None), ("a", None), ("-1", None),
                                           ("1:2:3:4", None), ("1::2", None)])
def test_parse_time(text: str, sec: float | None) -> None:
    assert plan.parse_time(text) == sec


def test_fmt_time() -> None:
    assert plan.fmt_time(3730.5) == "1:02:10.5" and plan.fmt_time(61.46) == "1:01.5"
    assert plan.fmt_time_ms(60.0) == "1:00.000" and plan.fmt_seconds(30.8) == "30.8 秒"


# ------------------------------------------------------------------ 設定(§9・回答 Q-2・Q-5)
def test_config_defaults_and_fixups() -> None:
    s, changed = C.normalize({})
    assert changed and s == {"mode": "copy", "output": "separate", "strip_metadata": True, "filmstrip_count": 24,
                             "sendto_enabled": False}
    assert "keep_metadata" not in s
    s2, ch2 = C.normalize({"mode": "fast", "output": 3, "strip_metadata": "yes", "filmstrip_count": 49,
                           "sendto_enabled": 1, "other": "kept"})
    assert ch2 and s2["mode"] == "copy" and s2["output"] == "separate" and s2["strip_metadata"] is True
    assert s2["filmstrip_count"] == 24 and s2["sendto_enabled"] is False and s2["other"] == "kept"
    s3, ch3 = C.normalize({**s, "strip_metadata": False, "filmstrip_count": 8})
    assert not ch3 and C.parse(s3).strip_metadata is False


def test_thumb_targets() -> None:
    t = frames.thumb_targets(240.0, 24)
    assert len(t) == 24 and t[0] == pytest.approx(5.0) and t[-1] == pytest.approx(235.0)
    assert len(frames.thumb_targets(10.0, 24)) == 10          # 24 秒未満は1秒に1枚まで
    assert len(frames.thumb_targets(30.0, 48)) == 30
    assert len(frames.thumb_targets(0.4, 24)) == 1
    assert frames.thumb_targets(0.0, 24) == []
