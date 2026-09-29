# ffmpeg の呼び出し・進捗・中止(S-8・S-9・FR-14〜16・INV-3)。展開と照合は v0.4 で本体の deskkit.ffmpeg に移した(ClipTrim と共有)。
# 同梱の zip から展開した exe だけを呼び、PATH 上の ffmpeg は使わない。引数は配列で渡してシェルを通さず、入力の前に
# -protocol_whitelist file,pipe を必ず置き、パスには file: を付ける。ログにはファイル名・stderr の中身を書かない(終了コードだけ)。
from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from deskkit import ffmpeg as _ff
from deskkit.ffmpeg import (
    BUNDLE_SHA,
    BUNDLE_ZIP,
    CREATE_NO_WINDOW,
    EXE_NAME,
    STATE_EXTRACTED,
    STATE_MISSING,
    STATE_NOT_EXTRACTED,
    STATE_VERIFY_FAILED,
    arg_path,
    bundled_dir,
    sha256_file,
)

__all__ = ["BUNDLE_SHA", "BUNDLE_ZIP", "CREATE_NO_WINDOW", "EXE_NAME", "STATE_EXTRACTED", "STATE_MISSING",
           "STATE_NOT_EXTRACTED", "STATE_VERIFY_FAILED", "arg_path", "bundled_dir", "sha256_file"]

AUDIO_KBPS = 128
MIN_VIDEO_KBPS = 250
KBPS_480 = 600
KBPS_720 = 1500
RETRY_FACTOR = 0.85
MAX_RETRIES = 2
SAFETY = 0.95

MSG_MISSING = "動画の部品が見つかりません。DeskKit を入れ直してください"
MSG_BROKEN = "動画の部品が壊れています。DeskKit を入れ直してください"
MSG_NO_H264 = "この PC では動画を縮められません(「位置情報を消すだけ」は使えます)"
MSG_NO_DURATION = "動画の長さを読めませんでした"
MSG_NOT_VIDEO = "動画を読めませんでした"
MSG_DISK_FULL = "空き容量が足りません"
_FF_MESSAGES = {_ff.CODE_MISSING: MSG_MISSING, _ff.CODE_BROKEN: MSG_BROKEN, _ff.CODE_DISK_FULL: MSG_DISK_FULL}

# 入れ物の形式(-f に渡す名前)。再エンコードしないときは元の拡張子のまま
REMUX_FORMATS = {".mp4": "mp4", ".m4v": "mp4", ".mov": "mov", ".mkv": "matroska", ".webm": "webm", ".avi": "avi"}
LOCATION_PREFIXES = ("location", "com.apple.quicktime.location")


class VideoError(Exception):
    def __init__(self, result: str, message: str, code: str = "") -> None:
        super().__init__(message)
        self.result = result      # ops の result(error / too_large / verify_failed / cancelled)
        self.message = message    # 画面に出す文
        self.code = code or result  # ログに出す理由コード


def _is_disk_full(e: OSError) -> bool:
    return _ff.is_disk_full(e)


class FfmpegManager(_ff.FfmpegManager):
    """本体の deskkit.ffmpeg.FfmpegManager に、SendPrep の画面の文(VideoError)への言い換えを足したもの。
    `root` を渡せばそこへ展開する(本番は deskkit.ffmpeg.shared_root())。渡さなければ `data_dir / "ffmpeg"`(テスト用)。"""

    def __init__(self, data_dir: Path, bundle: Callable[[], Path | None] = bundled_dir,
                 log: logging.Logger | None = None, *, root: Path | None = None) -> None:
        super().__init__(root if root is not None else data_dir / "ffmpeg", bundle,
                         log or logging.getLogger("deskkit.sendprep"))

    def ensure(self, on_preparing: Callable[[], None] | None = None) -> Path:
        try:
            return super().ensure(on_preparing)
        except _ff.FfmpegError as e:
            raise VideoError("error", _FF_MESSAGES.get(e.code, MSG_BROKEN), e.code) from None


# ------------------------------------------------------------------ 情報の読み取り
@dataclass(frozen=True)
class Probe:
    duration: float | None
    has_video: bool
    has_audio: bool
    width: int | None
    height: int | None


_DUR = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_STREAM = re.compile(r"^\s*Stream #\d+:\d+.*?:\s*(Video|Audio):(.*)$", re.MULTILINE)
_DIM = re.compile(r"(?<![0-9])(\d{2,5})x(\d{2,5})(?![0-9])")


def parse_probe(stderr: str) -> Probe:
    dur: float | None = None
    m = _DUR.search(stderr)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        if dur <= 0:
            dur = None
    has_v = has_a = False
    w = h = None
    for sm in _STREAM.finditer(stderr):
        if sm.group(1) == "Video":
            if not has_v:
                dm = _DIM.search(sm.group(2))
                if dm:
                    w, h = int(dm.group(1)), int(dm.group(2))
            has_v = True
        else:
            has_a = True
    return Probe(dur, has_v, has_a, w, h)


def probe(exe: Path, src: Path) -> Probe:
    args = [str(exe), "-hide_banner", "-nostdin", "-protocol_whitelist", "file,pipe", "-i", arg_path(src)]
    try:
        r = subprocess.run(args, capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        raise VideoError("error", MSG_NOT_VIDEO, "probe_failed") from None
    return parse_probe(r.stderr.decode("utf-8", "replace"))


# ------------------------------------------------------------------ ビットレートの計画(S-8・FR-14)
@dataclass(frozen=True)
class Plan:
    video_kbps: int
    audio_kbps: int
    short_side: int | None  # None = 縮めない


def total_kbps(limit_bytes: int, duration: float) -> float:
    return limit_bytes * 8 * SAFETY / duration / 1000


def max_minutes(limit_bytes: int, audio_kbps: int) -> float:
    return limit_bytes * 8 * SAFETY / 1000 / (MIN_VIDEO_KBPS + audio_kbps) / 60


def too_long_message(limit_bytes: int, audio_kbps: int) -> str:
    mins = max_minutes(limit_bytes, audio_kbps)
    if mins >= 1:
        return f"長すぎて収まりません(この上限だと約 {int(mins)} 分まで)"
    return f"長すぎて収まりません(この上限だと約 {max(1, int(mins * 60))} 秒まで)"


def plan_for(video_kbps: float, audio_kbps: int) -> Plan | None:
    if video_kbps < MIN_VIDEO_KBPS:
        return None
    short = 480 if video_kbps < KBPS_480 else (720 if video_kbps < KBPS_720 else None)
    return Plan(int(video_kbps), audio_kbps, short)


def make_plan(limit_bytes: int, duration: float, has_audio: bool) -> Plan:
    a = AUDIO_KBPS if has_audio else 0
    p = plan_for(total_kbps(limit_bytes, duration) - a, a)
    if p is None:
        raise VideoError("too_large", too_long_message(limit_bytes, a), "too_long")
    return p


# ------------------------------------------------------------------ 引数(配列で渡す。INV-3)
def _base(exe: Path, src: Path) -> list[str]:
    return [str(exe), "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
            "-protocol_whitelist", "file,pipe", "-i", arg_path(src)]


def remux_args(exe: Path, src: Path, dst: Path, fmt: str) -> list[str]:
    """S-9: 再エンコードせず、メタデータ・チャプター・データトラックを落として入れ物だけを作り直す。"""
    return [*_base(exe, src), "-map", "0:v", "-map", "0:a?", "-map_metadata", "-1", "-map_chapters", "-1",
            "-c", "copy", "-progress", "pipe:1", "-nostats", "-f", fmt, arg_path(dst)]


def scale_filter(short_side: int | None) -> str:
    if short_side is None:
        return "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=nv12"
    s = short_side
    return (f"scale=w='if(gte(iw,ih),-2,min(iw,{s}))':h='if(gte(iw,ih),min(ih,{s}),-2)',"
            "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=nv12")


def encode_args(exe: Path, src: Path, dst: Path, plan: Plan, has_audio: bool) -> list[str]:
    """S-8: H.264(h264_mf)と AAC の MP4。メタデータは付けない。"""
    args = [*_base(exe, src), "-map", "0:v:0"]
    if has_audio:
        args += ["-map", "0:a:0"]
    args += ["-map_metadata", "-1", "-map_chapters", "-1", "-vf", scale_filter(plan.short_side),
             "-c:v", "h264_mf", "-b:v", f"{plan.video_kbps}k"]
    if has_audio:
        args += ["-c:a", "aac", "-b:a", f"{plan.audio_kbps}k", "-ac", "2"]
    args += ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", "-f", "mp4", arg_path(dst)]
    return args


def metadata_args(exe: Path, path: Path) -> list[str]:
    return [str(exe), "-hide_banner", "-nostdin", "-loglevel", "error", "-protocol_whitelist", "file,pipe",
            "-i", arg_path(path), "-f", "ffmetadata", "pipe:1"]


def has_location(ffmetadata: str) -> bool:
    for line in ffmetadata.splitlines():
        s = line.strip().lower()
        if s.startswith(LOCATION_PREFIXES):
            return True
    return False


def verify_output(exe: Path, path: Path) -> bool:
    """FR-9: ffmetadata に location・com.apple.quicktime.location から始まる行が無いこと。"""
    try:
        r = subprocess.run(metadata_args(exe, path), capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    if r.returncode != 0:
        return False
    return not has_location(r.stdout.decode("utf-8", "replace"))


# ------------------------------------------------------------------ 実行(進捗・中止)
@dataclass
class RunResult:
    returncode: int
    last_line: str        # stderr の最後の1行(画面にだけ出す)
    cancelled: bool


class Runner:
    """ffmpeg を1回動かす。stop() でいつでも止められる(子プロセスを終了させて待つ)。"""

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None
        self._mu = threading.Lock()
        self._stopped = False

    def stop(self) -> None:
        with self._mu:
            self._stopped = True
            p = self._proc
        if p is not None and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                pass

    @property
    def process(self) -> subprocess.Popen[bytes] | None:
        return self._proc

    def run(self, args: list[str], duration: float | None, on_progress: Callable[[float], None],
            cancelled: Callable[[], bool]) -> RunResult:
        with self._mu:
            if self._stopped:
                return RunResult(-1, "", True)
            self._proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          creationflags=CREATE_NO_WINDOW)
        p = self._proc
        tail: deque[str] = deque(maxlen=20)

        def read_err() -> None:
            assert p.stderr is not None
            for raw in p.stderr:
                s = raw.decode("utf-8", "replace").strip()
                if s:
                    tail.append(s)

        def read_out() -> None:
            assert p.stdout is not None
            for raw in p.stdout:
                s = raw.decode("ascii", "replace").strip()
                if s.startswith("out_time_us=") and duration:
                    try:
                        us = int(s.split("=", 1)[1])
                    except ValueError:
                        continue
                    on_progress(max(0.0, min(1.0, us / 1_000_000 / duration)))

        te = threading.Thread(target=read_err, daemon=True)
        to = threading.Thread(target=read_out, daemon=True)
        te.start()
        to.start()
        was_cancelled = False
        while p.poll() is None:
            if cancelled() or self._stopped:
                was_cancelled = True
                self.stop()
                break
            time.sleep(0.1)
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(10)
        te.join(5)
        to.join(5)
        return RunResult(p.returncode if p.returncode is not None else -1, tail[-1] if tail else "", was_cancelled)
