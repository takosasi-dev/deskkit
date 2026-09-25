# 1ファイル=1ジョブ。種類の判定 → 処理(メタデータ除去・縮小・動画の作り直し)→ 出力を読み直して検証(FR-9)。
# ワーカースレッドが1件ずつ順番に処理し、1件の失敗で残りを止めない(FR-17)。元のファイルは読み取り専用で開くだけ(INV-1)。
# ログと ops.jsonl にはファイル名・パスを書かない(INV-5)。例外の文(パスを含むことがある)もログに書かず、型名だけにする。
from __future__ import annotations

import errno
import io
import logging
import os
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from deskkit.modules.sendprep import metadata, video
from deskkit.modules.sendprep.oplog import OpsLog
from deskkit.modules.sendprep.video import FfmpegManager, VideoError

if TYPE_CHECKING:
    from PIL import Image

IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".webp", ".heic", ".heif", ".bmp", ".gif"})
VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"})
JPEG_EXTS = frozenset({".jpg", ".jpeg", ".jpe", ".jfif"})
MAX_NAME_TRIES = 1000
FALLBACK_SUBDIR = "SendPrep"

MSG_UNREADABLE = "読めませんでした"
MSG_BAD_IMAGE = "画像を読めませんでした"
MSG_TOO_BIG_IMAGE = "大きすぎる画像です"
MSG_TOO_LARGE = "上限に収まりません"
MSG_VERIFY = "確認できなかったため作り直せませんでした"
MSG_DISK_FULL = "空き容量が足りません"
MSG_NAMES = "同じ名前のファイルが多すぎます"
MSG_CANCELLED = "中止しました"
MSG_NO_OUTPUT = "出力が見つかりません"
MSG_FAILED = "処理できませんでした"
NOTE_ANIMATION = "アニメーションは1枚目だけになります"
PHASE_PREPARING = "動画の準備をしています(初回だけ)"

STATE_PENDING = "pending"
STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
FINISHED = frozenset({STATE_DONE, STATE_FAILED, STATE_CANCELLED})


def kind_of(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return None


def fmt_size(n: int | None) -> str:
    if n is None:
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}MB"
    return f"{max(1, round(n / 1000))}KB"


class JobError(Exception):
    def __init__(self, result: str, message: str, code: str = "") -> None:
        super().__init__(message)
        self.result = result
        self.message = message
        self.code = code or result


@dataclass(eq=False)
class Job:
    id: int
    path: Path
    kind: str
    state: str = STATE_PENDING
    preset_id: str = ""
    limit_bytes: int | None = None
    progress: float | None = None
    phase: str = ""
    in_bytes: int = 0
    out_path: Path | None = None
    out_bytes: int | None = None
    out_format: str = ""
    result: str = ""
    message: str = ""
    notes: list[str] = field(default_factory=list)
    fallback: bool = False
    redactions: int = 0
    cancel: threading.Event = field(default_factory=threading.Event)
    runner: video.Runner | None = None

    @property
    def finished(self) -> bool:
        return self.state in FINISHED

    def result_text(self) -> str:
        """結果の行に出す文(画面だけ)。"""
        if self.state == STATE_DONE:
            parts = ["位置情報なし", fmt_size(self.out_bytes)]
            if self.redactions:
                parts.append(f"伏せ字 {self.redactions} か所")
            parts.extend(self.notes)
            if self.fallback:
                parts.append("保存先: ピクチャ\\SendPrep")
            return "・".join(p for p in parts if p)
        if self.state == STATE_RUNNING:
            if self.phase:
                return self.phase
            if self.progress is not None:
                return f"処理しています… {int(self.progress * 100)}%"
            return "処理しています…"
        if self.state == STATE_QUEUED:
            return "順番を待っています"
        if self.state == STATE_PENDING:
            return "待機中"
        return self.message


# ------------------------------------------------------------------ 実行環境(テストで差し替える)
def _create_excl(p: Path) -> None:
    fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o666)
    os.close(fd)


def _default_fallback() -> Path:
    from deskkit.modules.sendprep._win32 import pictures_dir

    return pictures_dir() / FALLBACK_SUBDIR


@dataclass
class Env:
    ffmpeg: FfmpegManager
    ops: OpsLog
    log: logging.Logger
    fallback_dir: Callable[[], Path] = _default_fallback
    rename_to_date: Callable[[], bool] = lambda: False
    now: Callable[[], datetime] = lambda: datetime.now()
    create_file: Callable[[Path], None] = _create_excl  # 出力名の予約(O_EXCL)。テストで書けないフォルダを真似る
    on_update: Callable[[Job], None] = lambda _j: None


def _is_disk_full(e: OSError) -> bool:
    return e.errno == errno.ENOSPC or getattr(e, "winerror", None) in (39, 112)


def _is_denied(e: OSError) -> bool:
    return isinstance(e, PermissionError) or e.errno in (errno.EACCES, errno.EPERM, errno.EROFS) \
        or getattr(e, "winerror", None) in (5, 19)


def _remove_own(p: Path | None) -> None:
    """自分が作った出力(書きかけ・検証に失敗したもの)を消す。元のファイルは渡さない(VINV-2 の対象外)。"""
    if p is None:
        return
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


# ------------------------------------------------------------------ 出力名(S-10・S-11)
def base_name(src: Path, rename_to_date: bool, now: datetime) -> str:
    return f"share_{now:%Y%m%d_%H%M%S}" if rename_to_date else f"{src.stem}_share"


def reserve_in(folder: Path, stem: str, ext: str, create: Callable[[Path], None]) -> Path:
    """folder に stem.ext → 'stem (2).ext' … 'stem (1000).ext' の順で空いている名前を作って返す。"""
    for n in range(1, MAX_NAME_TRIES + 1):
        p = folder / (f"{stem}{ext}" if n == 1 else f"{stem} ({n}){ext}")
        try:
            create(p)
            return p
        except FileExistsError:
            continue
    raise JobError("error", MSG_NAMES, "names_exhausted")


def reserve_output(src: Path, ext: str, env: Env) -> tuple[Path, bool]:
    stem = base_name(src, env.rename_to_date(), env.now())
    try:
        return reserve_in(src.parent, stem, ext, env.create_file), False
    except OSError as e:
        if _is_disk_full(e):
            raise JobError("error", MSG_DISK_FULL, "disk_full") from None
        if not _is_denied(e):
            raise JobError("error", MSG_FAILED, f"reserve_{type(e).__name__}") from None
    fb = env.fallback_dir()
    try:
        fb.mkdir(parents=True, exist_ok=True)
        return reserve_in(fb, stem, ext, env.create_file), True
    except OSError as e:
        if _is_disk_full(e):
            raise JobError("error", MSG_DISK_FULL, "disk_full") from None
        raise JobError("error", MSG_FAILED, f"reserve_{type(e).__name__}") from None


def write_output(p: Path, data: bytes) -> None:
    try:
        with open(p, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        _remove_own(p)
        if _is_disk_full(e):
            raise JobError("error", MSG_DISK_FULL, "disk_full") from None
        raise JobError("error", MSG_FAILED, f"write_{type(e).__name__}") from None


def read_source(p: Path) -> bytes:
    """元のファイルは読み取り専用で開く(INV-1)。"""
    try:
        with open(p, "rb") as f:
            return f.read()
    except OSError:
        raise JobError("error", MSG_UNREADABLE, "unreadable") from None


# ------------------------------------------------------------------ 画像
_heif_registered = False


def open_image(data: bytes) -> Image.Image:
    from PIL import Image as PILImage

    global _heif_registered
    if not _heif_registered and metadata.detect(data) == "heif":
        import pi_heif

        pi_heif.register_heif_opener()
        _heif_registered = True
    try:
        im = PILImage.open(io.BytesIO(data))
        im.load()
    except PILImage.DecompressionBombError:
        raise JobError("error", MSG_TOO_BIG_IMAGE, "decompression_bomb") from None
    except (OSError, ValueError, SyntaxError, EOFError, IndexError, TypeError, KeyError, MemoryError):
        raise JobError("error", MSG_BAD_IMAGE, "decode_failed") from None
    return im


def _out_ext(fmt: str, src: Path) -> str:
    if fmt == "jpeg":
        return src.suffix.lower() if src.suffix.lower() in JPEG_EXTS else ".jpg"
    return {"png": ".png", "webp": ".webp"}[fmt]


def _first_frame(im: Image.Image) -> Image.Image:
    from deskkit.modules.sendprep import shrink

    try:
        im.seek(0)
    except EOFError:
        pass
    return im.convert("RGBA") if shrink.has_alpha(im) else im.convert("RGB")


def build_image(data: bytes, fmt: str, im: Image.Image, limit: int | None,
                cancelled: Callable[[], bool] = lambda: False) -> tuple[bytes, str, list[str]]:
    """(出力のバイト列, 出力形式, 画面に出す注記)。上限に収まらなければ JobError(too_large)。"""
    from deskkit.modules.sendprep import shrink

    icc = im.info.get("icc_profile") or None
    im.info = {}  # Pillow が保存時にコメント・Exif・XMP を持ち越さないように空にする
    notes: list[str] = []
    animated = bool(getattr(im, "is_animated", False)) and int(getattr(im, "n_frames", 1)) > 1
    try:
        if fmt == "jpeg":
            out = metadata.strip_jpeg(data)
            if limit is None or len(out) <= limit:  # FR-8: すでに収まっているなら縮めない
                return out, "jpeg", notes
            enc = shrink.fit_jpeg(im, limit, icc, metadata.jpeg_orientation(data), cancelled=cancelled)
        elif fmt == "png" and not animated:
            out = metadata.strip_png(data)
            if limit is None or len(out) <= limit:
                return out, "png", notes
            enc = shrink.fit_png(im, limit, icc, cancelled=cancelled)
        elif fmt == "webp" and not animated:
            out = metadata.strip_webp(data)
            if limit is None or len(out) <= limit:
                return out, "webp", notes
            enc = shrink.fit_webp(im, limit, icc, cancelled=cancelled)
        else:
            # S-3: HEIC は JPEG(透過があれば PNG)、BMP・GIF・アニメーションは PNG に変換する
            if animated:
                notes.append(NOTE_ANIMATION)
            frame = _first_frame(im)
            if fmt == "heif" and not shrink.has_alpha(frame):
                out = shrink.encode_jpeg(frame, shrink.Q_HI, icc)
                if limit is None or len(out) <= limit:
                    return out, "jpeg", notes
                enc = shrink.fit_jpeg(frame, limit, icc, cancelled=cancelled)
            else:
                out = shrink.encode_png(frame, icc)
                if limit is None or len(out) <= limit:
                    return out, "png", notes
                enc = shrink.fit_png(frame, limit, icc, cancelled=cancelled)
    except metadata.MetadataError:
        raise JobError("error", MSG_BAD_IMAGE, "structure") from None
    if cancelled():
        raise JobError("cancelled", MSG_CANCELLED)
    if enc is None:
        raise JobError("too_large", MSG_TOO_LARGE, "too_large")
    return enc.data, enc.fmt, notes


def _write_verified(job: Job, data: bytes, fmt: str, env: Env) -> None:
    out, fallback = reserve_output(job.path, _out_ext(fmt, job.path), env)
    try:
        if job.cancel.is_set():
            raise JobError("cancelled", MSG_CANCELLED)
        write_output(out, data)
        try:
            back = out.read_bytes()
        except OSError:
            back = b""
        if not metadata.verify(fmt, back) or (job.limit_bytes is not None and len(back) > job.limit_bytes):
            raise JobError("verify_failed", MSG_VERIFY)
    except BaseException:
        _remove_own(out)
        raise
    job.out_path, job.out_bytes, job.out_format, job.fallback = out, len(back), fmt, fallback


def process_image(job: Job, env: Env) -> None:
    data = read_source(job.path)
    job.in_bytes = len(data)
    fmt = metadata.detect(data)
    if fmt is None:
        raise JobError("error", MSG_BAD_IMAGE, "unknown_format")
    im = open_image(data)
    out, ofmt, notes = build_image(data, fmt, im, job.limit_bytes, job.cancel.is_set)
    job.notes = notes
    _write_verified(job, out, ofmt, env)


# ------------------------------------------------------------------ 動画
def process_video(job: Job, env: Env) -> None:
    try:
        job.in_bytes = job.path.stat().st_size
        with open(job.path, "rb"):
            pass  # 読めるかだけ確かめる(読み取り専用)
    except OSError:
        raise JobError("error", MSG_UNREADABLE, "unreadable") from None

    def preparing() -> None:
        job.phase = PHASE_PREPARING
        env.on_update(job)

    exe = env.ffmpeg.ensure(preparing)
    job.phase = ""
    env.on_update(job)
    pr = video.probe(exe, job.path)
    if not pr.has_video:
        raise JobError("error", video.MSG_NOT_VIDEO, "no_video")
    ext = job.path.suffix.lower()
    limit = job.limit_bytes
    last = [0.0]

    def progress(frac: float) -> None:
        job.progress = frac
        if time.monotonic() - last[0] >= 0.25 or frac >= 1.0:
            last[0] = time.monotonic()
            env.on_update(job)

    def run(args: list[str], out: Path) -> None:
        job.runner = video.Runner()
        if job.cancel.is_set():
            job.runner.stop()
        res = job.runner.run(args, pr.duration, progress, job.cancel.is_set)
        job.runner = None
        if res.cancelled or job.cancel.is_set():
            _remove_own(out)
            raise JobError("cancelled", MSG_CANCELLED)
        if res.returncode != 0:
            _remove_own(out)
            env.log.warning("ffmpeg exit=%d", res.returncode)  # stderr の中身はログに書かない
            if "no space left" in res.last_line.lower():
                raise JobError("error", MSG_DISK_FULL, "disk_full")
            line = res.last_line[:200]
            raise JobError("error", f"動画を処理できませんでした: {line}" if line else "動画を処理できませんでした",
                           f"ffmpeg_exit_{res.returncode}")

    out: Path | None = None
    reencode = limit is not None and job.in_bytes > limit
    try:
        if not reencode:
            job.progress = 0.0
            out, job.fallback = reserve_output(job.path, ext, env)
            run(video.remux_args(exe, job.path, out, video.REMUX_FORMATS.get(ext, "mp4")), out)
            if limit is not None and out.stat().st_size > limit:
                _remove_own(out)
                out = None
                reencode = True
        if reencode:
            assert limit is not None
            if pr.duration is None:
                raise JobError("error", video.MSG_NO_DURATION, "no_duration")
            a = video.AUDIO_KBPS if pr.has_audio else 0
            video.make_plan(limit, pr.duration, pr.has_audio)  # 短すぎる上限は先に断る(FR-14)
            if not env.ffmpeg.check_h264(exe):
                raise JobError("error", video.MSG_NO_H264, "no_h264")
            vk = video.total_kbps(limit, pr.duration) - a
            out, job.fallback = reserve_output(job.path, ".mp4", env)
            for _attempt in range(video.MAX_RETRIES + 1):
                plan = video.plan_for(vk, a)
                if plan is None:
                    break
                job.progress = 0.0
                env.on_update(job)
                run(video.encode_args(exe, job.path, out, plan, pr.has_audio), out)
                if out.stat().st_size <= limit:
                    break
                vk *= video.RETRY_FACTOR  # S-8: 上限を超えたら 0.85 倍で作り直す(最大2回)
            else:
                plan = None
            if plan is None:
                raise JobError("too_large", MSG_TOO_LARGE, "too_large")
        assert out is not None
        size = out.stat().st_size
        if not video.verify_output(exe, out) or (limit is not None and size > limit):
            raise JobError("verify_failed", MSG_VERIFY)
    except BaseException:
        _remove_own(out)
        raise
    job.out_path, job.out_bytes, job.out_format = out, size, out.suffix.lower().lstrip(".")
    job.progress = 1.0


# ------------------------------------------------------------------ 1件の処理(結果の記録まで)
def process(job: Job, env: Env) -> None:
    t0 = time.monotonic()
    job.state = STATE_RUNNING
    job.progress = None
    job.phase = ""
    env.on_update(job)
    try:
        if job.cancel.is_set():
            raise JobError("cancelled", MSG_CANCELLED)
        if job.kind == "video":
            process_video(job, env)
        else:
            process_image(job, env)
        job.state, job.result, job.message = STATE_DONE, "ok", ""
        code = "ok"
    except (JobError, VideoError) as e:
        job.state = STATE_CANCELLED if e.result == "cancelled" else STATE_FAILED
        job.result, job.message, code = e.result, e.message, e.code
    except Exception as e:  # noqa: BLE001 - 1件の失敗で残りを止めない(FR-17)
        job.state, job.result, job.message, code = STATE_FAILED, "error", MSG_FAILED, f"unexpected_{type(e).__name__}"
    finally:
        job.runner = None
        job.phase = ""
    ms = int((time.monotonic() - t0) * 1000)
    env.ops.write(kind=job.kind, preset=job.preset_id or "meta", result=job.result, in_bytes=job.in_bytes,
                  out_bytes=job.out_bytes if job.state == STATE_DONE else None, redactions=0, ms=ms)
    env.log.info("job kind=%s preset=%s result=%s code=%s in=%d out=%s ms=%d", job.kind, job.preset_id or "meta",
                 job.result, code, job.in_bytes, job.out_bytes if job.state == STATE_DONE else "-", ms)
    env.on_update(job)


# ------------------------------------------------------------------ 伏せ字の焼き込み(FR-12)
def load_for_edit(path: Path) -> Image.Image:
    """編集用に出力を読み、Orientation を画素に反映した画像にする(伏せる範囲は見たままの座標で持つ)。"""
    from PIL import ImageOps

    data = read_source(path)
    im = open_image(data)
    im = ImageOps.exif_transpose(im)
    im.info = {}
    return im.convert("RGBA") if "A" in im.getbands() else im.convert("RGB")


def burn_output(job: Job, rects: Sequence[tuple[int, int, int, int]], style: str, env: Env) -> None:
    """伏せ字を画素に書き込んだ画像で出力を置き換える。上限(FR-7)と検証(FR-9)を守る。失敗は JobError。"""
    from deskkit.modules.sendprep import redact, shrink

    t0 = time.monotonic()
    old = job.out_path
    if old is None or not old.is_file():
        raise JobError("error", MSG_NO_OUTPUT, "no_output")
    old_size = old.stat().st_size
    data = read_source(old)
    icc = open_image(data).info.get("icc_profile") or None
    img = load_for_edit(old)
    burned = redact.burn(img, rects, style)
    limit = job.limit_bytes
    fmt = job.out_format
    enc: shrink.Encoded | None
    if fmt == "jpeg":
        b = shrink.encode_jpeg(burned, shrink.Q_HI, icc)
        enc = shrink.Encoded(b, "jpeg", burned.size, shrink.Q_HI, False) if limit is None or len(b) <= limit \
            else shrink.fit_jpeg(burned, limit, icc)
    elif fmt == "webp":
        b = shrink.encode_webp(burned, shrink.Q_HI, icc)
        enc = shrink.Encoded(b, "webp", burned.size, shrink.Q_HI, False) if limit is None or len(b) <= limit \
            else shrink.fit_webp(burned, limit, icc)
    elif fmt == "png":
        b = shrink.encode_png(burned, icc)
        enc = shrink.Encoded(b, "png", burned.size, None, False) if limit is None or len(b) <= limit \
            else shrink.fit_png(burned, limit, icc)
    else:
        raise JobError("error", MSG_FAILED, "not_image")
    if enc is None:
        raise JobError("too_large", MSG_TOO_LARGE, "too_large")
    if enc.fmt == fmt:
        tmp = old.with_name(f".{old.stem}.sendprep-{secrets.token_hex(4)}.tmp")
        dest = old
    else:
        tmp = reserve_in(old.parent, old.stem, _out_ext(enc.fmt, old), env.create_file)
        dest = tmp
    try:
        write_output(tmp, enc.data)
        back = tmp.read_bytes()
        if not metadata.verify(enc.fmt, back) or (limit is not None and len(back) > limit):
            raise JobError("verify_failed", MSG_VERIFY)
        if not old.is_file():
            raise JobError("error", MSG_NO_OUTPUT, "no_output")
        if dest == old:
            os.replace(tmp, old)
        else:
            _remove_own(old)  # 形式が変わった: 前の出力(自分が作ったもの)を消す
    except BaseException:
        if tmp != old:
            _remove_own(tmp)
        raise
    job.out_path, job.out_bytes, job.out_format = dest, len(back), enc.fmt
    job.redactions += len(rects)
    ms = int((time.monotonic() - t0) * 1000)
    env.ops.write(kind="image", preset=job.preset_id or "meta", result="ok", in_bytes=old_size, out_bytes=len(back),
                  redactions=len(rects), ms=ms)
    env.log.info("burn result=ok redactions=%d in=%d out=%d ms=%d", len(rects), old_size, len(back), ms)


# ------------------------------------------------------------------ ワーカー
class Worker:
    """ジョブを1件ずつ順番に処理するスレッド。on_idle は、積まれた分が全部終わったときに呼ぶ。"""

    def __init__(self, env: Env, on_idle: Callable[[], None]) -> None:
        self.env = env
        self._on_idle = on_idle
        self._q: deque[Job] = deque()
        self._cv = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stopping = False
        self.current: Job | None = None

    def submit(self, jobs: Sequence[Job]) -> None:
        with self._cv:
            if self._stopping:
                return
            for j in jobs:
                j.state = STATE_QUEUED
                self._q.append(j)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="sendprep-worker", daemon=True)
                self._thread.start()
            self._cv.notify_all()

    def busy(self) -> bool:
        with self._cv:
            return self.current is not None or bool(self._q)

    def cancel(self, job: Job) -> None:
        job.cancel.set()
        r = job.runner
        if r is not None:
            r.stop()

    def _loop(self) -> None:
        while True:
            with self._cv:
                if not self._q and not self._stopping:
                    self._cv.wait(30.0)  # しばらく何も積まれなければスレッドを終える(次に積まれたら作り直す)
                if self._stopping or not self._q:
                    self._thread = None
                    return
                job = self._q.popleft()
                self.current = job
            try:
                process(job, self.env)
            except Exception as e:  # noqa: BLE001 - ここまで漏れた例外でもワーカーは止めない
                self.env.log.error("worker failed: %s", type(e).__name__)
            with self._cv:
                self.current = None
                idle = not self._q
            if idle and not self._stopping:
                try:
                    self._on_idle()
                except Exception as e:  # noqa: BLE001
                    self.env.log.error("on_idle failed: %s", type(e).__name__)

    def stop(self, timeout: float = 15.0) -> None:
        """待っているジョブを中止にし、動いている ffmpeg を終了させ、書きかけの出力を消してから止まる。"""
        with self._cv:
            self._stopping = True
            pending = list(self._q)
            self._q.clear()
            cur = self.current
            th = self._thread
            self._cv.notify_all()
        for j in pending:
            j.cancel.set()
            j.state, j.result, j.message = STATE_CANCELLED, "cancelled", MSG_CANCELLED
        if cur is not None:
            self.cancel(cur)
        if th is not None and th is not threading.current_thread():
            th.join(timeout)


def expand_inputs(paths: Sequence[Any]) -> tuple[list[Path], int]:
    """(対応形式のファイル, 対応していない形式の数)。フォルダは直下のファイルだけを見る(FR-1。サブフォルダは見ない)。"""
    files: list[Path] = []
    unsupported = 0
    for raw in paths:
        p = Path(str(raw))
        try:
            if p.is_dir():
                for c in sorted(p.iterdir(), key=lambda x: x.name.casefold()):
                    try:
                        if not c.is_file():
                            continue
                    except OSError:
                        continue
                    if kind_of(c) is None:
                        unsupported += 1
                    else:
                        files.append(c)
                continue
            if p.is_file():
                if kind_of(p) is None:
                    unsupported += 1
                else:
                    files.append(p)
        except OSError:
            continue
    return files, unsupported
