# 区間 → ffmpeg の引数の配列・大きさの見積もり・出力の名前(純粋な関数。T-5〜T-11・T-13・FR-11)。
# 引数はすべて配列で、各 -i の直前に -protocol_whitelist file,pipe、利用者のパスには file: を付ける(INV-2)。
# concat の一覧は標準入力で渡すための文字列を作る(ディスクに書かない。T-13)。
from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from deskkit.modules.cliptrim import runner
from deskkit.modules.cliptrim.keyframes import fmt_ss
from deskkit.modules.cliptrim.probe import VideoInfo

MAX_SEGMENTS = 20
MODES = ("copy", "precise")
OUTPUTS = ("separate", "join")
AUDIO_KBPS = 128
MIN_VKBPS = 500
MAX_VKBPS = 50_000
REENCODE_FACTOR = 1.5           # 元が HEVC・VP9・AV1 のとき(T-7。Q-3 は今の案のまま)
EFFICIENT_CODECS = ("hevc", "h265", "vp9", "av1")
COPY_MARGIN = 1.05              # FR-11
PRECISE_MARGIN = 1.3
FREE_MARGIN = 100 * 1000 * 1000
FAT32_LIMIT = 4 * 1024 ** 3
MAX_NAME_TRIES = 1000
PART_RE = r"^.+\.cliptrim-[0-9a-f]{8}(?:-\d+)?\.part$"

# 画質そのままで使う入れ物(T-6): 拡張子 → (出力の拡張子, -f に渡す名前, probe の入れ物の名前)
COPY_CONTAINERS: dict[str, tuple[str, str, str]] = {
    ".mp4": (".mp4", "mp4", "mp4"),
    ".m4v": (".mp4", "mp4", "mp4"),
    ".mov": (".mov", "mov", "mov"),
    ".mkv": (".mkv", "matroska", "matroska"),
    ".webm": (".webm", "webm", "webm"),
}
VIDEO_EXTS = frozenset({".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".ts", ".m2ts", ".mts", ".wmv", ".flv",
                        ".3gp", ".mpg", ".mpeg"})


def copy_container(src: Path, info: VideoInfo) -> tuple[str, str] | None:
    """画質そのままの (出力の拡張子, -f の名前)。選べない入れ物なら None(T-6)。"""
    c = COPY_CONTAINERS.get(src.suffix.lower())
    if c is None or not info.is_family(c[2]):
        return None
    return c[0], c[1]


def is_matroska(info: VideoInfo) -> bool:
    return info.is_family("matroska", "webm")


# ------------------------------------------------------------------ 区間
@dataclass(frozen=True)
class Seg:
    start: float           # 利用者が決めた開始
    end: float             # 終了のコマの終わり
    ss: float              # -ss に渡す値(ぴったりは start)
    actual: float          # 実際の開始(ぴったりは start)

    @property
    def length(self) -> float:
        return self.end - self.actual


def precise_seg(start: float, end: float) -> Seg:
    return Seg(start, end, start, start)


# ------------------------------------------------------------------ 引数
def _head(exe: Path) -> list[str]:
    return [str(exe), "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]


def _input(src: Path, ss: float, dur: float) -> list[str]:
    return ["-ss", fmt_ss(ss), "-t", fmt_ss(max(0.001, dur)), "-protocol_whitelist", "file,pipe", "-i", runner.arg_path(src)]


def _meta(strip: bool) -> list[str]:
    # チャプターはどちらでも書き出さない(T-9)。回転の情報は -map_metadata -1 でも残る(P-11)
    return (["-map_metadata", "-1"] if strip else []) + ["-map_chapters", "-1"]


def _tail(muxer: str, out: Path) -> list[str]:
    return ["-progress", "pipe:1", "-nostats", "-f", muxer, runner.arg_path(out)]


def copy_args(exe: Path, src: Path, seg: Seg, out: Path, muxer: str, strip: bool) -> list[str]:
    """画質そのまま: 切れ目 K の -ss と「終了のコマの終わり − 実際の開始」の -t(T-5)。映像1本と音声の全部(T-9)。"""
    return [*_head(exe), *_input(src, seg.ss, seg.end - seg.actual), "-map", "0:V:0", "-map", "0:a?", "-c", "copy",
            *_meta(strip), *_tail(muxer, out)]


def video_kbps(info: VideoInfo, size_bytes: int) -> int:
    """ぴったりの映像のビットレート(T-7)。元の映像の値 → 無ければ 大きさ×8÷長さ − 音声。"""
    v: float | None = float(info.video_kbps) if info.video_kbps else None
    if v is None and info.duration:
        audio = sum(a or AUDIO_KBPS for a in info.audio_kbps)
        v = size_bytes * 8 / 1000 / info.duration - audio
    if v is None or v <= 0:
        v = MIN_VKBPS
    if info.vcodec in EFFICIENT_CODECS:
        v *= REENCODE_FACTOR
    return int(max(MIN_VKBPS, min(MAX_VKBPS, v)))


_VF = "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=nv12"


def _encode_opts(vkbps: int, audio: int) -> list[str]:
    out = ["-c:v", "h264_mf", "-b:v", f"{vkbps}k"]
    if audio:
        out += ["-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k"]
    return out


def precise_args(exe: Path, src: Path, seg: Seg, out: Path, vkbps: int, audio: int, strip: bool) -> list[str]:
    """ぴったり: 入力の前の -ss S と -t、h264_mf と AAC の MP4(T-7)。回転は画素に反映される(ffmpeg の既定)。"""
    return [*_head(exe), *_input(src, seg.start, seg.end - seg.start), "-map", "0:V:0", "-map", "0:a?", "-vf", _VF,
            *_encode_opts(vkbps, audio), *_meta(strip), *_tail("mp4", out)]


def precise_join_args(exe: Path, src: Path, segs: Sequence[Seg], out: Path, vkbps: int, audio: int,
                      strip: bool) -> list[str]:
    """ぴったり・つなげて1本: 区間の数だけ入力を並べ、concat フィルタで1回で作り直す(T-8)。"""
    args = _head(exe)
    for s in segs:
        args += _input(src, s.start, s.end - s.start)
    parts: list[str] = []
    labels: list[str] = []
    for i in range(len(segs)):
        parts.append(f"[{i}:V:0]{_VF}[v{i}]")
        labels.append(f"[v{i}]")
        labels.extend(f"[{i}:a:{j}]" for j in range(audio))
    outs = "[v]" + "".join(f"[a{j}]" for j in range(audio))
    graph = ";".join(parts) + ";" + "".join(labels) + f"concat=n={len(segs)}:v=1:a={audio}{outs}"
    args += ["-filter_complex", graph, "-map", "[v]"]
    for j in range(audio):
        args += ["-map", f"[a{j}]"]
    args += [*_encode_opts(vkbps, audio), *_meta(strip), *_tail("mp4", out)]
    return args


def concat_quote(p: Path) -> str:
    """concat の一覧の1項目。file: を付け、' は '\\'' にする(P-7・T-13)。"""
    return "'" + ("file:" + str(p)).replace("'", "'\\''") + "'"


def concat_list(parts: Sequence[Path]) -> bytes:
    """各ファイルに inpoint 0 を付ける(P-6。無いと B フレームのある MP4 で音声の時刻が逆戻りした)。"""
    lines = ["ffconcat version 1.0"]
    for p in parts:
        lines.append(f"file {concat_quote(p)}")
        lines.append("inpoint 0")
    return ("\n".join(lines) + "\n").encode("utf-8")


def concat_args(exe: Path, out: Path, muxer: str, strip: bool) -> list[str]:
    return [*_head(exe), "-f", "concat", "-safe", "0", "-protocol_whitelist", "file,pipe", "-i", "pipe:0",
            "-map", "0:V:0", "-map", "0:a?", "-c", "copy", *_meta(strip), *_tail(muxer, out)]


def framecrc_args(exe: Path, path: Path) -> list[str]:
    return [str(exe), "-hide_banner", "-nostdin", "-loglevel", "error", "-protocol_whitelist", "file,pipe",
            "-i", runner.arg_path(path), "-map", "0:V:0", "-c", "copy", "-f", "framecrc", "pipe:1"]


def ffmetadata_args(exe: Path, path: Path) -> list[str]:
    return [str(exe), "-hide_banner", "-nostdin", "-loglevel", "error", "-protocol_whitelist", "file,pipe",
            "-i", runner.arg_path(path), "-f", "ffmetadata", "pipe:1"]


LOCATION_PREFIXES = ("location", "com.apple.quicktime.location")


def has_location(ffmetadata: str) -> bool:
    return any(line.strip().lower().startswith(LOCATION_PREFIXES) for line in ffmetadata.splitlines())


# ------------------------------------------------------------------ 見積もり(FR-11)
def estimate_copy(size_bytes: int, seconds: float, duration: float, join: bool) -> int:
    if duration <= 0:
        return size_bytes
    est = size_bytes * (seconds / duration) * COPY_MARGIN
    return int(est * (2 if join else 1))


def estimate_precise(vkbps: int, audio: int, seconds: float) -> int:
    return int((vkbps + AUDIO_KBPS * audio) * 1000 * seconds / 8 * PRECISE_MARGIN)


def fmt_bytes(n: int) -> str:
    if n >= 1000 ** 3:
        return f"{n / 1000 ** 3:.1f}GB"
    if n >= 1000 ** 2:
        return f"{n / 1000 ** 2:.0f}MB" if n >= 10 * 1000 ** 2 else f"{n / 1000 ** 2:.1f}MB"
    return f"{max(1, round(n / 1000))}KB"


def fmt_gb(n: int) -> str:
    return f"{max(0.1, n / 1000 ** 3):.1f}"


def space_problem(estimates: Sequence[int], free: int | None, fs_name: str | None) -> str | None:
    """書き出す前の確かめ(FR-11)。問題があれば画面の文、無ければ None。"""
    need = sum(estimates)
    if free is not None and free < need + FREE_MARGIN:
        return f"空き容量が足りません(あと約 {fmt_gb(need + FREE_MARGIN - free)} GB)"
    if fs_name and fs_name.upper().startswith("FAT") and any(e >= FAT32_LIMIT for e in estimates):
        return "このドライブには 4GB を超えるファイルを書けません"
    return None


# ------------------------------------------------------------------ 名前(T-11)
def out_stems(src: Path, count: int) -> list[str]:
    """区間ごとなら <元>_clip1・_clip2…、1本なら <元>_clip。"""
    if count == 1:
        return [f"{src.stem}_clip"]
    return [f"{src.stem}_clip{i + 1}" for i in range(count)]


def name_candidates(stem: str, ext: str) -> list[str]:
    return [f"{stem}{ext}"] + [f"{stem} ({n}){ext}" for n in range(2, MAX_NAME_TRIES + 1)]


def job_hex() -> str:
    return secrets.token_hex(4)


def part_name(final_name: str, hex8: str, index: int | None = None) -> str:
    return f"{final_name}.cliptrim-{hex8}{'' if index is None else f'-{index}'}.part"


# ------------------------------------------------------------------ 表示
def fmt_time(x: float, *, precise: bool = True) -> str:
    """h:mm:ss.s(1 時間未満は m:ss.s)。"""
    x = max(0.0, x)
    tenths = int(round(x * 10))
    h, rem = divmod(tenths, 36000)
    m, rem = divmod(rem, 600)
    s, t = divmod(rem, 10)
    base = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    return f"{base}.{t}" if precise else base


def fmt_time_ms(x: float) -> str:
    """区間の表示用(ミリ秒まで)。"""
    x = max(0.0, x)
    ms = int(round(x * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, r = divmod(rem, 1000)
    base = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    return f"{base}.{r:03d}"


def fmt_seconds(x: float) -> str:
    if x >= 60:
        return fmt_time(x)
    return f"{x:.1f} 秒"


def parse_time(text: str) -> float | None:
    """`h:mm:ss.s`・`m:ss`・秒 を秒にする。読めなければ None。"""
    s = text.strip().replace("：", ":").replace("．", ".")
    if not s:
        return None
    parts = s.split(":")
    if len(parts) > 3:
        return None
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if any(n < 0 for n in nums) or any(not p.strip() for p in parts):
        return None
    if len(nums) > 1 and any(n >= 60 for n in nums[1:]):
        return None
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total
