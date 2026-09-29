# コマの取り出し(T-4)とフィルムストリップ(FR-6)。どちらも入力の前の -ss で速く飛び、PNG を pipe で受け取る。
# コマは位置をマイクロ秒で切り捨てて渡す(P-4)。フィルムストリップは切れ目だけをデコードする(1枚1プロセス。P-9)。
# 回転は ffmpeg の既定(autorotate)で画素に反映される。stderr はログに書かない。
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from deskkit.modules.cliptrim import runner
from deskkit.modules.cliptrim.keyframes import floor_us, fmt_ss

FRAME_WIDTH = 640
THUMB_WIDTH = 160
FRAME_TIMEOUT_S = 15.0   # FR-5・FR-13
THUMB_TIMEOUT_S = 15.0
_PTS_TIME = re.compile(r"pts_time:\s*(-?\d+(?:\.\d+)?)")


def _scale(width: int) -> str:
    return f"scale=w='min({width},iw)':h=-2"


def frame_args(exe: Path, src: Path, t: float, width: int = FRAME_WIDTH) -> list[str]:
    return [str(exe), "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", fmt_ss(floor_us(t)),
            "-protocol_whitelist", "file,pipe", "-i", runner.arg_path(src), "-map", "0:V:0", "-an", "-sn", "-dn",
            "-frames:v", "1", "-vf", _scale(width), "-c:v", "png", "-f", "image2pipe", "pipe:1"]


def thumb_args(exe: Path, src: Path, t: float, width: int = THUMB_WIDTH) -> list[str]:
    # showinfo はそのコマの元の時刻(-copyts)を stderr に出す。押したときにその位置へ移るために読む
    return [str(exe), "-hide_banner", "-nostdin", "-loglevel", "info", "-skip_frame", "nokey", "-noaccurate_seek",
            "-ss", fmt_ss(floor_us(t)), "-copyts", "-protocol_whitelist", "file,pipe", "-i", runner.arg_path(src),
            "-map", "0:V:0", "-an", "-sn", "-dn", "-frames:v", "1", "-vf", f"showinfo,{_scale(width)}",
            "-c:v", "png", "-f", "image2pipe", "pipe:1"]


def grab_frame(exe: Path, src: Path, t: float, *, token: runner.Token | None = None,
               timeout: float = FRAME_TIMEOUT_S, popen: runner.Popen | None = None) -> bytes | None:
    """位置 t に表示されるコマの PNG。読めなければ None(中止・時間切れを含む)。"""
    cap = runner.capture(frame_args(exe, src, t), timeout=timeout, token=token, popen=popen)
    if cap.cancelled or cap.timed_out or not cap.stdout.startswith(b"\x89PNG"):
        return None
    return cap.stdout


@dataclass(frozen=True)
class Thumb:
    index: int
    target: float          # 区切りの中央
    pos: float             # 実際のコマの位置(読めなければ target)
    png: bytes | None


def thumb_targets(duration: float, count: int) -> list[float]:
    """長さを count 等分した各区切りの中央。24 秒未満は1秒に1枚まで(FR-6)。"""
    if duration <= 0:
        return []
    n = max(1, min(count, int(duration))) if duration < count else count
    return [(i + 0.5) * duration / n for i in range(n)]


def grab_thumb(exe: Path, src: Path, index: int, t: float, start: float, *, token: runner.Token | None = None,
               timeout: float = THUMB_TIMEOUT_S, popen: runner.Popen | None = None) -> Thumb:
    cap = runner.capture(thumb_args(exe, src, t), timeout=timeout, token=token, popen=popen)
    png = cap.stdout if cap.stdout.startswith(b"\x89PNG") and not cap.cancelled and not cap.timed_out else None
    pos = t
    m = _PTS_TIME.search(cap.stderr.decode("utf-8", "replace"))
    if m and png is not None:
        pos = max(0.0, float(m.group(1)) - start)
    return Thumb(index, t, pos, png)
