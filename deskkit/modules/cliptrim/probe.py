# `ffmpeg -i <入力>` の stderr から、長さ・開始時刻・入れ物・最初の映像(コーデック・大きさ・回転・コマの速さ・ビットレート・HDR)・
# 音声と字幕の本数を読む(ffprobe は同梱しない)。stderr にはパスが入るので、ここで読むだけでログには書かない(INV-3)。
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from deskkit.modules.cliptrim import runner

_INPUT = re.compile(r"^Input #0, (.+?), from ", re.MULTILINE)
_DUR = re.compile(r"Duration:\s*(?:(\d+):(\d{2}):(\d{2}(?:\.\d+)?)|N/A)(?:,\s*start:\s*(-?\d+(?:\.\d+)?))?"
                  r"(?:,\s*bitrate:\s*(\d+)\s*kb/s)?")
_STREAM = re.compile(r"^\s*Stream #0:\d+\S*:\s*(Video|Audio|Subtitle|Data|Attachment):\s*(.*)$")
_DIM = re.compile(r"(?<![0-9])(\d{2,5})x(\d{2,5})(?![0-9])")
_FPS = re.compile(r"(\d+(?:\.\d+)?)\s*fps")
_TBR = re.compile(r"(\d+(?:\.\d+)?)(k?)\s*tbr")
_KBPS = re.compile(r"(\d+)\s*kb/s")
_VSTART = re.compile(r"(?:^|,)\s*start (-?\d+(?:\.\d+)?)")
_ROT = re.compile(r"(?:displaymatrix|display matrix):\s*rotation of\s*(-?\d+(?:\.\d+)?)\s*degrees", re.IGNORECASE)
HDR_MARKS = ("smpte2084", "arib-std-b67")


@dataclass(frozen=True)
class VideoInfo:
    duration: float | None      # 秒。読めなければ None
    start: float                # 開始時刻(秒)。画面の位置は pts − start
    formats: tuple[str, ...]    # 入れ物の名前(mov,mp4,… / matroska,webm など)
    has_video: bool
    vcodec: str = ""
    width: int | None = None    # 入れ物に書かれた大きさ(回転の前)
    height: int | None = None
    rotation: int = 0           # 0 / 90 / 180 / 270
    fps: float | None = None
    video_kbps: int | None = None
    total_kbps: int | None = None
    audio_count: int = 0
    audio_kbps: tuple[int | None, ...] = ()
    subtitle_count: int = 0
    hdr: bool = False
    video_start: float | None = None  # 映像のストリームの開始(入れ物の開始と違うときだけ ffmpeg が出す)

    @property
    def video_extent(self) -> float | None:
        """映像のある長さの目安(入れ物の終わり − 映像の開始)。音声が映像より先に始まる MKV の切り出しで使う(FR-15 (c))。"""
        if self.duration is None:
            return None
        if self.video_start is None or self.video_start <= self.start:
            return self.duration
        return self.duration - (self.video_start - self.start)

    @property
    def display_size(self) -> tuple[int, int] | None:
        if self.width is None or self.height is None:
            return None
        if self.rotation in (90, 270):
            return self.height, self.width
        return self.width, self.height

    @property
    def frame_seconds(self) -> float:
        """1コマの長さの目安(コマの速さが読めなければ 30 コマ/秒とみなす)。"""
        return 1.0 / self.fps if self.fps and self.fps > 0 else 1.0 / 30

    def is_family(self, *names: str) -> bool:
        return any(n in self.formats for n in names)


def _norm_rotation(deg: float) -> int:
    r = int(round(deg)) % 360
    return min((0, 90, 180, 270), key=lambda x: min(abs(x - r), 360 - abs(x - r)))


def parse(stderr: str) -> VideoInfo:
    formats: tuple[str, ...] = ()
    m = _INPUT.search(stderr)
    if m:
        formats = tuple(s.strip() for s in m.group(1).split(",") if s.strip())
    dur: float | None = None
    start = 0.0
    total: int | None = None
    d = _DUR.search(stderr)
    if d:
        if d.group(1) is not None:
            dur = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3))
            if dur <= 0:
                dur = None
        if d.group(4):
            start = float(d.group(4))
        if d.group(5):
            total = int(d.group(5))
    has_v = False
    vcodec = ""
    w = h = None
    fps: float | None = None
    vk: int | None = None
    rot = 0
    hdr = False
    vstart: float | None = None
    audio: list[int | None] = []
    subs = 0
    current = ""  # 今読んでいるストリームの種類("video0" は最初の映像)
    for line in stderr.splitlines():
        sm = _STREAM.match(line)
        if sm:
            kind, rest = sm.group(1), sm.group(2)
            current = ""
            if kind == "Video":
                if "(attached pic)" in rest or not rest.strip():
                    continue  # カバー画像は映像に数えない(-map 0:V:0 と同じ扱い)
                if not has_v:
                    has_v = True
                    current = "video0"
                    vcodec = rest.split(",", 1)[0].split(" ", 1)[0].strip().lower()
                    dm = _DIM.search(rest)
                    if dm:
                        w, h = int(dm.group(1)), int(dm.group(2))
                    fm = _FPS.search(rest)
                    if fm:
                        fps = float(fm.group(1))
                    else:
                        tm = _TBR.search(rest)
                        if tm:
                            fps = float(tm.group(1)) * (1000 if tm.group(2) else 1)
                    km = _KBPS.search(rest)
                    if km:
                        vk = int(km.group(1))
                    hdr = any(x in rest for x in HDR_MARKS)
                    vs = _VSTART.search(rest)
                    if vs:
                        vstart = float(vs.group(1))
            elif kind == "Audio":
                km = _KBPS.search(rest)
                audio.append(int(km.group(1)) if km else None)
            elif kind == "Subtitle":
                subs += 1
            continue
        if current == "video0":
            rm = _ROT.search(line)
            if rm:
                rot = _norm_rotation(float(rm.group(1)))
    if fps is not None and fps <= 0:
        fps = None
    return VideoInfo(dur, start, formats, has_v, vcodec, w, h, rot, fps, vk, total, len(audio), tuple(audio), subs, hdr, vstart)


def probe_args(exe: Path, src: Path) -> list[str]:
    return [str(exe), "-hide_banner", "-nostdin", "-protocol_whitelist", "file,pipe", "-i", runner.arg_path(src)]


def probe(exe: Path, src: Path, token: runner.Token | None = None, timeout: float = 60.0,
          popen: runner.Popen | None = None) -> VideoInfo | None:
    """読めなければ None(ffmpeg を起こせない・時間切れ・中止)。出力先が無いので終了コードは 1 になるのが普通。"""
    cap = runner.capture(probe_args(exe, src), timeout=timeout, token=token, popen=popen)
    if cap.cancelled or cap.timed_out or cap.returncode is None:
        return None
    return parse(cap.stderr.decode("utf-8", "replace"))
