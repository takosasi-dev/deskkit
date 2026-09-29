# 書き出しのジョブ(1本ずつ。FR-11〜16・T-8・T-11)。出力はまず `<本当の名前>.cliptrim-<16進8桁>.part` に書き、検証(FR-15)を
# 通ったものだけを本当の名前に変える(INV-4)。消すのはこのジョブで作った .part と、落ちたときの残り(1時間以上前)だけ(INV-5)。
# 元の動画は ffmpeg の入力として読むだけ(INV-1)。ログ・ops.jsonl にはファイル名・パス・stderr を書かない(INV-3)。
from __future__ import annotations

import errno
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from deskkit.modules.cliptrim import keyframes, plan, probe, runner
from deskkit.modules.cliptrim.oplog import OpsLog
from deskkit.modules.cliptrim.plan import Seg
from deskkit.modules.cliptrim.probe import VideoInfo

FALLBACK_SUBDIR = "ClipTrim"
VERIFY_TIMEOUT_S = 120.0
STALE_PART_S = 3600.0

MSG_CANCELLED = "中止しました"
MSG_STALLED = "応答が無くなったため止めました"
MSG_VERIFY = "確かめられなかったため書き出しをやめました"
MSG_DISK_FULL = "空き容量が足りません"
MSG_NO_SOURCE = "元の動画が見つかりません"
MSG_SAME_FILE = "元の動画と同じファイルには書けません"
MSG_NAMES = "同じ名前のファイルが多すぎます"
MSG_RENAME = "書き出したファイルの名前を変えられませんでした"
MSG_WRITE = "書き出し先に書けませんでした"
MSG_NO_H264_SIZE = "この大きさの動画は、この PC では作り直せません。画質そのままを使ってください"
MSG_FAILED = "動画を書き出せませんでした"
MSG_JOIN_FAILED = "つなげる途中で失敗したため、何も残しませんでした"


@dataclass(frozen=True)
class ExportRequest:
    src: Path
    info: VideoInfo
    src_size: int
    mode: str                                 # copy / precise
    output: str                               # separate / join
    strip: bool                               # 撮影場所などの情報を消す
    segs: tuple[Seg, ...]                     # 時刻の順
    vkbps: int = 0                            # ぴったりの映像のビットレート
    container: tuple[str, str] | None = None  # 画質そのままの (拡張子, -f の名前)

    @property
    def join(self) -> bool:
        return self.output == "join" and len(self.segs) > 1

    def planned(self, seg: Seg) -> float:
        return seg.end - (seg.actual if self.mode == "copy" else seg.start)

    def estimates(self) -> list[int]:
        """出力ごとの大きさの見積もり(FR-11)。つなげて1本は1つ。"""
        dur = self.info.duration or 0.0
        if self.mode == "copy":
            secs = [self.planned(s) for s in self.segs]
            if self.join:
                return [plan.estimate_copy(self.src_size, sum(secs), dur, True)]
            return [plan.estimate_copy(self.src_size, s, dur, False) for s in secs]
        secs = [self.planned(s) for s in self.segs]
        if self.join:
            return [plan.estimate_precise(self.vkbps, self.info.audio_count, sum(secs))]
        return [plan.estimate_precise(self.vkbps, self.info.audio_count, s) for s in secs]


@dataclass
class OutputResult:
    index: int
    ok: bool = False
    code: str = ""              # ok / cancelled / verify_failed / no_space / timeout / error
    message: str = ""              # 画面に出す文(stderr の最後の1行を含むことがある。画面だけ)
    path: Path | None = None       # 完了した本当の名前
    seconds: float = 0.0
    bytes: int = 0


@dataclass
class JobResult:
    result: str
    outputs: list[OutputResult]
    message: str = ""
    folder: Path | None = None
    fallback: bool = False
    ms: int = 0

    @property
    def ok_count(self) -> int:
        return sum(1 for o in self.outputs if o.ok)


class JobError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


def _default_fallback() -> Path:
    from deskkit.modules.cliptrim._win32 import videos_dir

    return videos_dir() / FALLBACK_SUBDIR


def _default_free(p: Path) -> int | None:
    import shutil

    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


def _default_fs(p: Path) -> str | None:
    from deskkit.modules.cliptrim._win32 import volume_fs_name

    return volume_fs_name(p)


@dataclass
class Env:
    exe: Path
    log: logging.Logger
    ops: OpsLog
    fallback_dir: Callable[[], Path] = _default_fallback
    free_bytes: Callable[[Path], int | None] = _default_free
    fs_name: Callable[[Path], str | None] = _default_fs
    runner_factory: Callable[[], runner.Runner] = runner.Runner
    hexgen: Callable[[], str] = plan.job_hex
    stall_s: float = runner.STALL_S


@dataclass(eq=False)
class Job:
    req: ExportRequest
    progress: float = 0.0
    phase: str = ""
    token: runner.Token = field(default_factory=runner.Token)
    runner: runner.Runner | None = None
    parts: list[Path] = field(default_factory=list)   # このジョブで作った .part(消してよいのはこれだけ)
    result: JobResult | None = None

    @property
    def cancelled(self) -> bool:
        return self.token.cancelled

    def cancel(self) -> None:
        self.token.cancel()
        r = self.runner
        if r is not None:
            r.stop()


# ------------------------------------------------------------------ ファイル
def _is_disk_full(e: OSError) -> bool:
    return e.errno == errno.ENOSPC or getattr(e, "winerror", None) in (39, 112)


def _is_denied(e: OSError) -> bool:
    return isinstance(e, PermissionError) or e.errno in (errno.EACCES, errno.EPERM, errno.EROFS) \
        or getattr(e, "winerror", None) in (5, 19)


def same_file(a: Path, b: Path) -> bool:
    """INV-1: 同じ実体か(os.path.samefile、無ければ正規化したパス)。"""
    try:
        if a.exists() and b.exists():
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _create_excl(p: Path) -> None:
    fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o666)
    os.close(fd)


def remove_part(p: Path) -> None:
    """このジョブが作った .part を消す(中止・失敗・検証を通らなかったとき。INV-5: パスはメモリに持った自分の物だけ)。"""
    if not p.name.endswith(".part") or ".cliptrim-" not in p.name:
        return
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


def cleanup_stale_parts(folder: Path, now: Callable[[], float] = time.time) -> int:
    """DeskKit が落ちて残した自分の .part を消す(§10)。名前が *.cliptrim-<16進8桁>[-番号].part に合い、更新が1時間以上前の物だけ
    (INV-5)。利用者のファイルはこの名前にならない。"""
    n = 0
    try:
        entries = list(folder.iterdir())
    except OSError:
        return 0
    for p in entries:
        if not re.fullmatch(plan.PART_RE, p.name):
            continue
        try:
            if p.is_file() and now() - p.stat().st_mtime >= STALE_PART_S:
                p.unlink()
                n += 1
        except OSError:
            continue
    return n


# ------------------------------------------------------------------ 実行
class _Ctx:
    def __init__(self, job: Job, env: Env, on_update: Callable[[Job], None], steps: int) -> None:
        self.job = job
        self.env = env
        self.on_update = on_update
        self.steps = max(1, steps)
        self.step = 0
        self._last = 0.0

    def set_progress(self, frac: float) -> None:
        self.job.progress = min(1.0, (self.step + frac) / self.steps)
        now = time.monotonic()
        if now - self._last >= 0.25 or frac >= 1.0:
            self._last = now
            self.on_update(self.job)

    def check_cancel(self) -> None:
        if self.job.cancelled:
            raise JobError("cancelled", MSG_CANCELLED)

    def reserve(self, folder: Path, name: str) -> Path:
        p = folder / name
        if same_file(self.job.req.src, p):
            raise JobError("error", MSG_SAME_FILE)
        _create_excl(p)
        self.job.parts.append(p)
        return p

    def drop(self, p: Path) -> None:
        remove_part(p)
        if p in self.job.parts:
            self.job.parts.remove(p)

    def run(self, args: list[str], duration: float, stdin: bytes | None = None) -> None:
        self.check_cancel()
        r = self.env.runner_factory()
        self.job.runner = r
        if self.job.cancelled:
            r.stop()
        res = r.run(args, duration=duration, on_progress=self.set_progress, cancelled=lambda: self.job.cancelled,
                    stdin=stdin, stall_s=self.env.stall_s)
        self.job.runner = None
        if res.cancelled or self.job.cancelled:
            raise JobError("cancelled", MSG_CANCELLED)
        if res.stalled:
            raise JobError("timeout", MSG_STALLED)
        if res.returncode != 0:
            self.env.log.warning("ffmpeg exit=%d", res.returncode)  # stderr の中身はログに書かない
            low = res.stderr_text.lower()
            if "no space left" in low or "not enough space" in low:
                raise JobError("no_space", MSG_DISK_FULL)
            if self.job.req.mode == "precise" and ("h264_mf" in low or "mft" in low):
                raise JobError("error", MSG_NO_H264_SIZE)
            line = res.last_line[:200]
            raise JobError("error", f"{MSG_FAILED}: {line}" if line else MSG_FAILED)
        self.step += 1
        self.set_progress(0.0)


def verify(exe: Path, path: Path, planned: float, lo: float, hi: float, audio: int, strip: bool,
           token: runner.Token | None = None, popen: runner.Popen | None = None) -> float | None:
    """FR-15。通れば出力の長さ(秒)、通らなければ None。(a) の終了コードは呼ぶ側で見ている。"""
    # (b) 映像のパケットがあり、最初が切れ目
    if not first_packet_is_key(exe, path, token, popen):
        return None
    info = probe.probe(exe, path, token=token, timeout=VERIFY_TIMEOUT_S, popen=popen)
    if info is None or info.duration is None or not info.has_video:
        return None
    # (c) 長さ。映像のある長さで見る(MKV では音声が映像の切れ目より先に始まり、入れ物の長さがのびる。CT-5)
    extent = info.video_extent or info.duration
    if not (planned + lo - 1e-3 <= extent <= planned + hi + 1e-3):
        return None
    # (d) 音声の本数
    if info.audio_count != audio:
        return None
    # (e) 撮影場所の情報
    if strip:
        md = runner.capture(plan.ffmetadata_args(exe, path), timeout=VERIFY_TIMEOUT_S, token=token, popen=popen)
        if md.returncode != 0 or plan.has_location(md.stdout.decode("utf-8", "replace")):
            return None
    return info.duration


def first_packet_is_key(exe: Path, path: Path, token: runner.Token | None = None,
                        popen: runner.Popen | None = None) -> bool:
    box: list[keyframes.Packet] = []

    def feed(line: str) -> bool:
        pk = keyframes.parse_line(line)
        if pk is None:
            return True
        box.append(pk)
        return False

    cap = runner.stream(plan.framecrc_args(exe, path), feed, timeout=VERIFY_TIMEOUT_S, token=token, popen=popen)
    return not cap.cancelled and not cap.timed_out and bool(box) and box[0].key


def finalize(part: Path, folder: Path, stem: str, ext: str, src: Path) -> Path:
    """検証を通った .part を本当の名前に変える。同名があれば ' (2)'〜' (1000)'(T-11)。上書きしない。"""
    for name in plan.name_candidates(stem, ext):
        target = folder / name
        if same_file(src, target):
            continue
        if target.exists():
            continue
        try:
            os.rename(part, target)
            return target
        except FileExistsError:
            continue
        except OSError as e:
            if _is_disk_full(e):
                raise JobError("no_space", MSG_DISK_FULL) from None
            raise JobError("error", MSG_RENAME) from None
    raise JobError("error", MSG_NAMES)


def _pick_folder(c: _Ctx, first_name: str) -> tuple[Path, Path, bool]:
    """最初の .part を元のフォルダに作る。書けなければ「ビデオ\\ClipTrim」(T-11・§10)。(フォルダ, .part, 代わりの場所か)"""
    req = c.job.req
    try:
        return req.src.parent, c.reserve(req.src.parent, first_name), False
    except JobError:
        raise
    except FileExistsError:
        raise JobError("error", MSG_WRITE) from None
    except OSError as e:
        if _is_disk_full(e):
            raise JobError("no_space", MSG_DISK_FULL) from None
        if not _is_denied(e):
            raise JobError("error", MSG_WRITE) from None
    fb = c.env.fallback_dir()
    try:
        fb.mkdir(parents=True, exist_ok=True)
        return fb, c.reserve(fb, first_name), True
    except JobError:
        raise
    except OSError as e:
        if _is_disk_full(e):
            raise JobError("no_space", MSG_DISK_FULL) from None
        raise JobError("error", MSG_WRITE) from None


def _check_source(req: ExportRequest) -> None:
    if not req.src.is_file():
        raise JobError("error", MSG_NO_SOURCE)


def run_export(job: Job, env: Env, on_update: Callable[[Job], None] = lambda _j: None) -> JobResult:
    t0 = time.monotonic()
    req = job.req
    n_out = 1 if req.join else len(req.segs)
    ext, muxer = (req.container if req.mode == "copy" and req.container else (".mp4", "mp4"))
    stems = plan.out_stems(req.src, n_out)
    hex8 = env.hexgen()
    steps = (len(req.segs) + 1) if (req.join and req.mode == "copy") else n_out
    c = _Ctx(job, env, on_update, steps)
    outputs: list[OutputResult] = []
    folder: Path | None = None
    fallback = False
    overall_code = ""
    overall_msg = ""

    def seg_tol(count: int) -> tuple[float, float]:
        return (-0.2, 0.2) if req.mode == "precise" else (-0.1, 1.0 * count)

    def main_part_name(i: int) -> str:
        return plan.part_name(stems[i] + ext, hex8)

    try:
        c.check_cancel()
        _check_source(req)
        folder, first_part, fallback = _pick_folder(c, main_part_name(0))
        problem = plan.space_problem(req.estimates(), env.free_bytes(folder), env.fs_name(folder))
        if problem is not None:
            c.drop(first_part)
            raise JobError("no_space" if problem.startswith("空き") else "error", problem)
        cleanup_stale_parts(folder)
        if not req.join:
            for i, seg in enumerate(req.segs):
                out = OutputResult(i)
                outputs.append(out)
                part: Path | None = first_part if i == 0 else None
                try:
                    c.check_cancel()
                    _check_source(req)
                    if part is None:
                        part = c.reserve(folder, main_part_name(i))
                    planned = req.planned(seg)
                    if req.mode == "copy":
                        assert req.container is not None
                        args = plan.copy_args(env.exe, req.src, seg, part, muxer, req.strip)
                    else:
                        args = plan.precise_args(env.exe, req.src, seg, part, req.vkbps, req.info.audio_count, req.strip)
                    c.run(args, planned)
                    lo, hi = seg_tol(1)
                    got = verify(env.exe, part, planned, lo, hi, req.info.audio_count, req.strip, job.token)
                    if got is None:
                        c.check_cancel()
                        raise JobError("verify_failed", MSG_VERIFY)
                    size = part.stat().st_size
                    final = finalize(part, folder, stems[i], ext, req.src)
                    job.parts.remove(part)
                    out.ok, out.code, out.path, out.seconds, out.bytes = True, "ok", final, got, size
                except JobError as e:
                    if part is not None:
                        c.drop(part)
                    out.code, out.message = e.code, e.message
                    if e.code == "cancelled":
                        raise
                except OSError as e:
                    if part is not None:
                        c.drop(part)
                    out.code = "no_space" if _is_disk_full(e) else "error"
                    out.message = MSG_DISK_FULL if _is_disk_full(e) else MSG_WRITE
                if not out.ok:
                    c.step = i + 1
        else:
            out = OutputResult(0)
            outputs.append(out)
            seg_parts: list[Path] = []
            try:
                planned = sum(req.planned(s) for s in req.segs)
                if req.mode == "copy":
                    assert req.container is not None
                    for i, seg in enumerate(req.segs):
                        _check_source(req)
                        sp = c.reserve(folder, plan.part_name(stems[0] + ext, hex8, i + 1))
                        seg_parts.append(sp)
                        c.run(plan.copy_args(env.exe, req.src, seg, sp, muxer, req.strip), req.planned(seg))
                    c.run(plan.concat_args(env.exe, first_part, muxer, req.strip), planned,
                          stdin=plan.concat_list(seg_parts))
                    for sp in seg_parts:
                        c.drop(sp)
                    seg_parts = []
                else:
                    _check_source(req)
                    c.run(plan.precise_join_args(env.exe, req.src, req.segs, first_part, req.vkbps,
                                                 req.info.audio_count, req.strip), planned)
                lo, hi = seg_tol(len(req.segs))
                got = verify(env.exe, first_part, planned, lo, hi, req.info.audio_count, req.strip, job.token)
                if got is None:
                    c.check_cancel()
                    raise JobError("verify_failed", MSG_VERIFY)
                size = first_part.stat().st_size
                final = finalize(first_part, folder, stems[0], ext, req.src)
                job.parts.remove(first_part)
                out.ok, out.code, out.path, out.seconds, out.bytes = True, "ok", final, got, size
            except JobError as e:
                out.code, out.message = e.code, e.message  # つなげて1本はどこかで失敗したら全体を失敗にする(FR-14)
                raise
            except OSError as e:
                raise JobError("no_space" if _is_disk_full(e) else "error",
                                MSG_DISK_FULL if _is_disk_full(e) else MSG_WRITE) from None
            finally:
                for sp in seg_parts:
                    c.drop(sp)
    except JobError as e:
        overall_code, overall_msg = e.code, e.message
    except Exception as e:  # noqa: BLE001 - 想定外でも .part を残さない
        env.log.error("export failed: %s", type(e).__name__)
        overall_code, overall_msg = "error", MSG_FAILED
    finally:
        job.runner = None
        for p in list(job.parts):  # 中止・失敗で残った自分の .part(FR-13・FR-14)
            remove_part(p)
        job.parts.clear()
    ok = sum(1 for o in outputs if o.ok)
    if overall_code:
        result = overall_code if ok == 0 else "partial"
        if overall_code == "cancelled" and ok:
            result = "partial"
    elif ok == len(outputs) and outputs:
        result = "ok"
    elif ok:
        result = "partial"
    else:
        codes = [o.code for o in outputs if o.code]
        result = codes[0] if codes else "error"
        overall_msg = next((o.message for o in outputs if o.message), MSG_FAILED)
    if result not in ("ok", "partial", "cancelled", "verify_failed", "no_space", "timeout", "error"):
        result = "error"
    ms = int((time.monotonic() - t0) * 1000)
    shift = max((s.start - s.actual for s in req.segs), default=0.0) if req.mode == "copy" else None
    env.ops.write(mode=req.mode, output="join" if req.join else "separate", segments=len(req.segs), files_ok=ok,
                  files_failed=max(0, n_out - ok), src_seconds=req.info.duration or 0.0,
                  out_seconds=sum(o.seconds for o in outputs if o.ok), out_bytes=sum(o.bytes for o in outputs if o.ok),
                  shift_max_s=shift, result=result, ms=ms)
    env.log.info("export mode=%s output=%s segments=%d ok=%d failed=%d result=%s fallback=%s ms=%d", req.mode,
                 "join" if req.join else "separate", len(req.segs), ok, max(0, n_out - ok), result, fallback, ms)
    job.result = JobResult(result, outputs, overall_msg, folder, fallback, ms)
    job.progress = 1.0
    on_update(job)
    return job.result


class ExportWorker:
    """書き出しを1本ずつ動かすスレッド。stop() は動いているジョブを中止し、.part を消し終えるまで待つ。"""

    def __init__(self, env_factory: Callable[[], Env], on_update: Callable[[Job], None],
                 on_done: Callable[[Job], None]) -> None:
        self._env_factory = env_factory
        self._on_update = on_update
        self._on_done = on_done
        self._thread: threading.Thread | None = None
        self.current: Job | None = None
        self._mu = threading.Lock()

    def busy(self) -> bool:
        with self._mu:
            return self.current is not None

    def submit(self, job: Job) -> bool:
        with self._mu:
            if self.current is not None:
                return False
            self.current = job
            self._thread = threading.Thread(target=self._run, args=(job,), name="cliptrim-export", daemon=True)
            self._thread.start()
            return True

    def _run(self, job: Job) -> None:
        try:
            env = self._env_factory()
            run_export(job, env, self._on_update)
        except Exception as e:  # noqa: BLE001 - ここまで漏れた例外でも片づけて終える
            logging.getLogger("deskkit.cliptrim").error("export worker failed: %s", type(e).__name__)
            for p in list(job.parts):
                remove_part(p)
            if job.result is None:
                job.result = JobResult("error", [], MSG_FAILED)
        finally:
            with self._mu:
                self.current = None
            try:
                self._on_done(job)
            except Exception as e:  # noqa: BLE001
                logging.getLogger("deskkit.cliptrim").error("export done failed: %s", type(e).__name__)

    def cancel(self) -> None:
        with self._mu:
            j = self.current
        if j is not None:
            j.cancel()

    def stop(self, timeout: float = 15.0) -> None:
        self.cancel()
        th = self._thread
        if th is not None and th is not threading.current_thread():
            th.join(timeout)
