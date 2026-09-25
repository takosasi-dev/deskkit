# ffmpeg の展開・照合・呼び出し・進捗・中止(S-8・S-9・FR-14〜16・INV-3)。同梱の zip から展開した exe だけを呼び、
# PATH 上の ffmpeg は使わない。引数は配列で渡してシェルを通さず、入力の前に -protocol_whitelist file,pipe を必ず置き、
# パスには file: を付ける。ログにはファイル名・stderr の中身を書かない(終了コードだけ)。
from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import sys
import threading
import time
import zipfile
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

BUNDLE_ZIP = "ffmpeg.zip"
BUNDLE_SHA = "ffmpeg.sha256"
EXE_NAME = "ffmpeg.exe"
CREATE_NO_WINDOW = 0x08000000
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

# 入れ物の形式(-f に渡す名前)。再エンコードしないときは元の拡張子のまま
REMUX_FORMATS = {".mp4": "mp4", ".m4v": "mp4", ".mov": "mov", ".mkv": "matroska", ".webm": "webm", ".avi": "avi"}
LOCATION_PREFIXES = ("location", "com.apple.quicktime.location")

STATE_MISSING = "missing"
STATE_NOT_EXTRACTED = "not_extracted"
STATE_EXTRACTED = "extracted"
STATE_VERIFY_FAILED = "verify_failed"


class VideoError(Exception):
    def __init__(self, result: str, message: str, code: str = "") -> None:
        super().__init__(message)
        self.result = result      # ops の result(error / too_large / verify_failed / cancelled)
        self.message = message    # 画面に出す文
        self.code = code or result  # ログに出す理由コード


def bundled_dir() -> Path | None:
    """同梱の ffmpeg.zip と ffmpeg.sha256 がある場所(exe の中 → ソースの順。契約 §1.4)。"""
    cands: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(Path(meipass) / "deskkit" / "_bundled")
    import deskkit

    cands.append(Path(deskkit.__file__).parent / "_bundled")
    for c in cands:
        if (c / BUNDLE_ZIP).is_file() and (c / BUNDLE_SHA).is_file():
            return c
    return None


def sha256_file(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def arg_path(p: Path) -> str:
    """ffmpeg に渡すパス。常に file: を付ける(- で始まる名前をオプションと取り違えない。INV-3)。"""
    return "file:" + str(p)


def _is_disk_full(e: OSError) -> bool:
    return e.errno == 28 or getattr(e, "winerror", None) in (39, 112)


class FfmpegManager:
    """初めて使うときに zip から展開し、起動後の初回の使用時に SHA-256 を照合する(V-5・FR-16)。"""

    def __init__(self, data_dir: Path, bundle: Callable[[], Path | None] = bundled_dir,
                 log: logging.Logger | None = None) -> None:
        self.root = data_dir / "ffmpeg"
        self._bundle = bundle
        self.log = log or logging.getLogger("deskkit.sendprep")
        self._lock = threading.Lock()
        self._exe: Path | None = None
        self._failed = False
        self.h264: bool | None = None  # None = まだ確かめていない

    # ---- 状態(診断・画面用。重い処理はしない)
    def state(self) -> str:
        if self._exe is not None:
            return STATE_EXTRACTED
        if self._failed:
            return STATE_VERIFY_FAILED
        bd = self._bundle()
        if bd is None:
            return STATE_MISSING
        sha = self._read_sha(bd)
        if sha and (self.root / sha[:16] / EXE_NAME).is_file():
            return STATE_EXTRACTED
        return STATE_NOT_EXTRACTED

    def h264_state(self) -> str:
        return "untested" if self.h264 is None else ("available" if self.h264 else "unavailable")

    @staticmethod
    def _read_sha(bd: Path) -> str | None:
        try:
            s = (bd / BUNDLE_SHA).read_text(encoding="ascii").split()[0].strip().lower()
        except (OSError, IndexError, UnicodeDecodeError):
            return None
        return s if re.fullmatch(r"[0-9a-f]{64}", s) else None

    # ---- 展開と照合
    def ensure(self, on_preparing: Callable[[], None] | None = None) -> Path:
        with self._lock:
            if self._exe is not None:
                return self._exe
            bd = self._bundle()
            if bd is None:
                self.log.warning("ffmpeg bundle missing")
                raise VideoError("error", MSG_MISSING, "ffmpeg_missing")
            sha = self._read_sha(bd)
            if sha is None:
                self._failed = True
                self.log.warning("ffmpeg sha file broken")
                raise VideoError("error", MSG_BROKEN, "ffmpeg_broken")
            exe = self.root / sha[:16] / EXE_NAME
            if exe.is_file():
                try:
                    ok = sha256_file(exe) == sha
                except OSError:
                    ok = False
                if ok:
                    self._exe = exe
                    self._failed = False
                    self.log.info("ffmpeg verified")
                    return exe
                self.log.warning("ffmpeg verify failed; extracting again")
            if on_preparing is not None:
                on_preparing()
            t0 = time.monotonic()
            try:
                got = self._extract(bd / BUNDLE_ZIP, exe)
            except OSError as e:
                self._failed = True
                self.log.warning("ffmpeg extract failed: %s", type(e).__name__)
                if _is_disk_full(e):
                    raise VideoError("error", MSG_DISK_FULL, "disk_full") from None
                raise VideoError("error", MSG_BROKEN, "ffmpeg_broken") from None
            except (zipfile.BadZipFile, KeyError, EOFError, RuntimeError, NotImplementedError) as e:
                self._failed = True
                self.log.warning("ffmpeg extract failed: %s", type(e).__name__)
                raise VideoError("error", MSG_BROKEN, "ffmpeg_broken") from None
            if got != sha:
                self._failed = True
                exe.unlink(missing_ok=True)  # 自分が展開した照合失敗の exe(自分のキャッシュ)
                self.log.warning("ffmpeg verify failed after extract")
                raise VideoError("error", MSG_BROKEN, "ffmpeg_broken")
            self._exe = exe
            self._failed = False
            self.log.info("ffmpeg extracted ms=%d", int((time.monotonic() - t0) * 1000))
            self._cleanup_old(exe.parent)
            return exe

    def _extract(self, zpath: Path, exe: Path) -> str:
        exe.parent.mkdir(parents=True, exist_ok=True)
        tmp = exe.with_name(EXE_NAME + ".part")
        h = hashlib.sha256()
        with zipfile.ZipFile(zpath) as zf:
            names = [n for n in zf.namelist() if n.replace("\\", "/").rsplit("/", 1)[-1].lower() == EXE_NAME]
            if not names:
                raise KeyError(EXE_NAME)
            try:
                with zf.open(names[0]) as src, open(tmp, "wb") as dst:
                    while True:
                        b = src.read(4 * 1024 * 1024)
                        if not b:
                            break
                        h.update(b)
                        dst.write(b)
                lic = [n for n in zf.namelist() if n.replace("\\", "/").rsplit("/", 1)[-1].lower() == "license.txt"]
                if lic:
                    (exe.parent / "LICENSE.txt").write_bytes(zf.read(lic[0]))
            except BaseException:
                tmp.unlink(missing_ok=True)  # 書きかけの展開物(自分のキャッシュ)
                raise
        os.replace(tmp, exe)
        return h.hexdigest()

    def _cleanup_old(self, keep: Path) -> None:
        """古い版の展開物(自分のキャッシュ)を消す。名前が 16 桁の16進のフォルダだけ。"""
        try:
            for d in self.root.iterdir():
                if d == keep or not d.is_dir() or not re.fullmatch(r"[0-9a-f]{16}", d.name):
                    continue
                for f in d.iterdir():
                    if f.is_file() and f.name in (EXE_NAME, EXE_NAME + ".part", "LICENSE.txt"):
                        f.unlink(missing_ok=True)
                d.rmdir()
        except OSError:
            pass

    # ---- H.264 エンコーダーが使えるか(VQ-2)。パイプから数フレームだけ流して確かめる
    def check_h264(self, exe: Path) -> bool:
        if self.h264 is not None:
            return self.h264
        w, h, n = 64, 64, 5
        frames = bytes([128]) * (w * h * 3 // 2 * n)
        args = [str(exe), "-hide_banner", "-nostdin", "-loglevel", "error", "-protocol_whitelist", "file,pipe",
                "-f", "rawvideo", "-pix_fmt", "nv12", "-s", f"{w}x{h}", "-r", "10", "-i", "pipe:0",
                "-frames:v", str(n), "-c:v", "h264_mf", "-b:v", "200k", "-f", "null", "pipe:1"]
        try:
            r = subprocess.run(args, input=frames, capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
            ok = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        self.h264 = ok
        self.log.info("h264_mf available=%s", ok)
        return ok


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
