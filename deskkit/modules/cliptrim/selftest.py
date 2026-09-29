# ClipTrim の自己検査(AC-16)。framecrc の行・ffmpeg -i の情報の読み取り・引数の安全策・concat の一覧・時刻の読み方・名前・
# 操作記録にファイル名が出ないことを確かめる。同梱の ffmpeg があれば、一時フォルダの合成動画(testsrc)で切り出しまで通す。
# 実機の設定・「送る」・利用者の動画には触れない。run() は 0=合格 / 1=不合格。
from __future__ import annotations

import hashlib
import logging
import subprocess
import tempfile
from pathlib import Path

_SAMPLE_STDERR = """Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'file:C:\\x\\a.mov':
  Duration: 00:01:02.50, start: 0.100000, bitrate: 6000 kb/s
  Stream #0:0[0x1](und): Video: hevc (Main 10) (hvc1 / 0x31637668), yuv420p10le(tv, bt2020nc/bt2020/arib-std-b67), 1920x1080, 5800 kb/s, 29.97 fps, 29.97 tbr, 600 tbn (default)
    Side data:
      Display Matrix: rotation of -90.00 degrees
  Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 44100 Hz, stereo, fltp, 128 kb/s (default)
  Stream #0:2[0x3](und): Audio: aac (LC) (mp4a / 0x6134706D), 48000 Hz, mono, fltp, 64 kb/s
  Stream #0:3[0x4](eng): Subtitle: mov_text (tx3g / 0x67337874), 0 kb/s
"""


class _Result:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, name: str, ok: bool) -> None:
        print(f"  [{'OK' if ok else 'NG'}] {name}")
        if not ok:
            self.failed += 1


def _pure(r: _Result, tmp: Path) -> None:
    from deskkit.modules.cliptrim import keyframes as K
    from deskkit.modules.cliptrim import plan, probe
    from deskkit.modules.cliptrim.oplog import OpsLog

    lines = ["#tb 0: 1/15360", "0,      -1024,          0,      512,     4750, 0x82ec0778",
             "0,       -512,       1024,      512,      732, 0x1203576b, F=0x0",
             "0,          0,        512,      512,       49, 0x8ce81092, F=0x1",
             "0,        512,       1536,      512,       49, 0x8ce81092, F=0x5",
             "1,          0,          0,     1024,      203, 0xb1ee60de"]
    pk = [K.parse_line(x) for x in lines]
    r.check("framecrc: 切れ目の読み分け(F= 無し・0x0・0x1・0x5)と負の dts・音声の行",
            K.parse_tb(lines[0]) is not None and pk[1] is not None and pk[1].key and pk[1].dts == -1024
            and pk[2] is not None and not pk[2].key and pk[3] is not None and pk[3].key
            and pk[4] is not None and pk[4].key and pk[5] is None)
    info = probe.parse(_SAMPLE_STDERR)
    r.check("情報: 長さ・開始・回転・HDR・音声 2 本・字幕", info.duration == 62.5 and info.start == 0.1 and info.rotation == 270
            and info.display_size == (1080, 1920) and info.hdr and info.audio_count == 2 and info.subtitle_count == 1
            and info.vcodec == "hevc" and info.is_family("mov"))
    exe, src, out = Path("ffmpeg.exe"), Path("C:/v/-a.mp4"), Path("C:/v/-a_clip.mp4.cliptrim-0a1b2c3d.part")
    seg = plan.Seg(61.5, 71.5, 60.0, 60.0)
    arglists = [plan.copy_args(exe, src, seg, out, "mp4", True),
                plan.precise_args(exe, src, seg, out, 3000, 2, True),
                plan.precise_join_args(exe, src, [seg, plan.Seg(80, 90, 80, 80)], out, 3000, 2, False),
                plan.concat_args(exe, out, "mp4", True), K.around_args(exe, src, 61.5), probe.probe_args(exe, src)]
    ok = True
    for a in arglists:
        for i, x in enumerate(a):
            if x == "-i" and a[i - 2:i] != ["-protocol_whitelist", "file,pipe"]:
                ok = False
        if any(str(src) in x and not x.startswith("file:") for x in a[1:]):
            ok = False
    r.check("引数: 各 -i の直前に -protocol_whitelist file,pipe、パスは file: 付き", ok)
    lst = plan.concat_list([Path("C:/v/it's.part")]).decode("utf-8")
    r.check("concat の一覧: file: と ' の書き換え・inpoint 0", "'file:C:\\v\\it'\\''s.part'" in lst and "inpoint 0" in lst)
    r.check("時刻の読み方", plan.parse_time("1:02:10.5") == 3730.5 and plan.parse_time("2:30") == 150
            and plan.parse_time("90") == 90 and plan.parse_time("1:75") is None)
    r.check("出力の名前", plan.out_stems(Path("録画.mp4"), 1) == ["録画_clip"]
            and plan.out_stems(Path("録画.mp4"), 2) == ["録画_clip1", "録画_clip2"])
    ops = OpsLog(tmp / "ops.jsonl")
    ops.write(mode="copy", output="separate", segments=1, files_ok=1, files_failed=0, src_seconds=1.0, out_seconds=1.0,
              out_bytes=1, shift_max_s=0.5, result="ok", ms=1)
    r.check("操作記録: 決まった項目だけ", set(ops.read()[0]) == {"ts", "mode", "output", "segments", "files_ok",
                                                         "files_failed", "src_seconds", "out_seconds", "out_bytes",
                                                         "shift_max_s", "result", "ms"})


def _with_ffmpeg(r: _Result, tmp: Path) -> None:
    from deskkit import ffmpeg as F
    from deskkit.modules.cliptrim import jobs, keyframes, plan, probe
    from deskkit.modules.cliptrim.oplog import OpsLog

    bd = F.bundled_dir()
    if bd is None:
        print("  [--] 同梱の ffmpeg が無いので、動画の検査は飛ばします")
        return
    try:
        exe = F.FfmpegManager(tmp / "ff", lambda: bd).ensure()
    except F.FfmpegError as e:
        r.check(f"同梱の ffmpeg を展開して照合できる({e.code})", False)
        return
    secret = "SELFTEST-秘密の録画_ab12"
    src = tmp / f"{secret}.mp4"
    gen = [str(exe), "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
           "testsrc=size=160x120:rate=25:duration=8", "-f", "lavfi", "-i", "sine=duration=8", "-c:v", "mpeg4",
           "-g", "50", "-bf", "0", "-q:v", "5", "-c:a", "aac", "-metadata", "location=+35.6+139.7/", "file:" + str(src)]
    subprocess.run(gen, capture_output=True, timeout=60, creationflags=F.CREATE_NO_WINDOW, check=False)
    if not src.is_file():
        r.check("合成動画を作れる", False)
        return
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    info = probe.probe(exe, src)
    r.check("情報を読める", info is not None and info.has_video and info.duration is not None and info.audio_count == 1)
    assert info is not None
    a = keyframes.read(exe, src, 3.5, info.start)
    r.check("まわりの読み取り: 前後の切れ目", a.first is not None and abs(a.first.pos - 2.0) < 1e-3
            and a.next_key is not None and abs(a.next_key - 4.0) < 1e-3)
    cs = keyframes.resolve_copy_start(exe, src, 3.5, info.start, matroska=False)
    r.check("画質そのままの実際の開始", abs(cs.actual - 2.0) < 1e-3)
    log = logging.getLogger("deskkit.cliptrim.selftest")
    log.propagate = False
    log.setLevel(logging.DEBUG)
    h = logging.FileHandler(tmp / "cliptrim.log", encoding="utf-8")
    log.addHandler(h)
    try:
        req = jobs.ExportRequest(src, info, src.stat().st_size, "copy", "separate", True,
                                 (plan.Seg(3.5, 5.0, cs.ss, cs.actual),), 0, plan.copy_container(src, info))
        res = jobs.run_export(jobs.Job(req), jobs.Env(exe, log, OpsLog(tmp / "ops.jsonl"),
                                                      fallback_dir=lambda: tmp / "fallback"))
    finally:
        h.close()
        log.removeHandler(h)
    outp = res.outputs[0].path if res.outputs else None
    r.check("切り出し: 隣に _clip で書き、検証を通る", res.result == "ok" and outp == tmp / f"{secret}_clip.mp4")
    r.check("元の動画は変わらない", hashlib.sha256(src.read_bytes()).hexdigest() == before)
    r.check(".part が残らない", not list(tmp.glob("*.part")))
    texts = (tmp / "ops.jsonl").read_text(encoding="utf-8") + (tmp / "cliptrim.log").read_text(encoding="utf-8")
    r.check("ログと ops.jsonl にファイル名が出ない", "秘密の録画" not in texts and "ab12" not in texts and "result=ok" in texts)


def run() -> int:
    r = _Result()
    print("ClipTrim 自己検査")
    try:
        with tempfile.TemporaryDirectory(prefix="cliptrim-selftest-") as td:
            _pure(r, Path(td))
            _with_ffmpeg(r, Path(td))
    except Exception as e:  # noqa: BLE001 - 自己検査は例外も不合格として返す
        print(f"  [NG] 例外: {type(e).__name__}")
        r.failed += 1
    print("合格" if r.failed == 0 else f"不合格({r.failed} 件)")
    return 0 if r.failed == 0 else 1
