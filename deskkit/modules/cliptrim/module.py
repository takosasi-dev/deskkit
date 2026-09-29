# ClipTrim モジュール本体。開いた動画・位置・まわりの読み取り・区間・切り方・書き出しの状態を持ち、画面の裏の読み取り(lanes)と
# 書き出し(jobs)を束ねる。利用者が操作したときだけ動く。ffmpeg は本体の deskkit.ffmpeg から受け取り、GUI スレッドの外で呼ぶ。
# ログ・ops.jsonl・diagnostics・usage にファイル名・パス・stderr を書かない(INV-3)。画面には出してよい(V-8)。
from __future__ import annotations

import functools
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Signal

from deskkit import ffmpeg as _ff
from deskkit.catalog import info as catalog_info
from deskkit.modules.cliptrim import config as cfgmod
from deskkit.modules.cliptrim import frames, jobs, keyframes, plan, probe, runner, sendto
from deskkit.modules.cliptrim.keyframes import Around, CopyStart
from deskkit.modules.cliptrim.lanes import Lane
from deskkit.modules.cliptrim.oplog import OpsLog, sum_per_day
from deskkit.modules.cliptrim.probe import VideoInfo
from deskkit.usage import UsageSeries

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

    from deskkit.modules.cliptrim._win32 import ShellLinkApi

EPS = 1e-6
SHIFT_WARN_S = 10.0

MSG_NOT_FOUND = "ファイルが見つかりません"
MSG_CLOUD = "クラウドにだけある動画です。先に『このデバイス上で常に保持する』を選んでください"
MSG_NOT_VIDEO = "動画を読めませんでした"
MSG_NO_VIDEO_STREAM = "映像の無いファイルは開けません"
MSG_NO_DURATION = "動画の長さを読めませんでした"
MSG_PREPARING = "動画の準備をしています(初回だけ)"
MSG_FF_MISSING = "動画の部品が見つかりません。DeskKit を入れ直してください"
MSG_FF_BROKEN = "動画の部品が壊れています。DeskKit を入れ直してください"
MSG_FF_DISK = "空き容量が足りないため、動画の部品を用意できません"
MSG_FRAME = "このコマを読めませんでした"
MSG_AROUND = "この位置のまわりを読めませんでした"
MSG_NO_KEY_AHEAD = "この先 60 秒には切れ目がありません"
MSG_NO_KEY_END = "この先に切れ目はありません"
MSG_FAR_KEYS = "切れ目がとても少ない動画です。1コマの移動はコマの速さからの目安になります"
MSG_NO_COPY = "この形式の動画は『画質そのまま』で切れないため、『ぴったり』で切ります"
MSG_NO_PRECISE = "この PC では『ぴったり』が使えません"
MSG_CANNOT = "この動画は切り出せません"
MSG_HDR = "HDR の動画は、ぴったりでは色が薄くなることがあります"
MSG_SUBS = "字幕は入りません"
MSG_BUSY = "書き出しの間は変えられません"
MSG_WAIT_COPY = "区間の位置を確かめています。少し待ってから押してください"
MSG_NEED_MARKS = "「ここから」と「ここまで」を決めてください"
MSG_END_BEFORE = "終わりが始まりより前です"
MSG_OVERLAP = "ほかの区間と重なっています"
MSG_TOO_MANY = f"区間は {plan.MAX_SEGMENTS} 個までです"
MSG_DISCARD = "今の区間を捨てて、新しい動画を開きますか"
FF_MESSAGES = {_ff.CODE_MISSING: MSG_FF_MISSING, _ff.CODE_BROKEN: MSG_FF_BROKEN, _ff.CODE_DISK_FULL: MSG_FF_DISK}


class Signals(QObject):
    video = Signal()           # 開いた動画・開く途中の状態
    frame = Signal()           # 表示するコマ・読み込み中
    position = Signal()        # 位置・まわりの読み取り・区間の印
    thumbs = Signal(int)       # フィルムストリップの i 番目が届いた(-1 = 作り直し)
    segments = Signal()        # 区間・切り方・書き出し方・見積もり
    job = Signal()             # 書き出しの進捗・結果
    info = Signal()            # 設定・動画の部品・「送る」
    notice = Signal(str, str)  # (種類 info/warn/error, 文) 画面の一時的なお知らせ


@dataclass(frozen=True)
class Opened:
    path: Path
    info: VideoInfo
    size: int
    exe: Path
    container: tuple[str, str] | None   # 画質そのままの (拡張子, -f の名前)。選べなければ None


@dataclass(eq=False)
class Segment:
    start: float
    end: float
    copy: CopyStart | None = None
    copy_state: str = "none"   # none / reading / ok / error

    @property
    def length(self) -> float:
        return self.end - self.start

    @property
    def shift(self) -> float:
        """画質そのままで開始が早まる秒数。"""
        return max(0.0, self.start - self.copy.actual) if self.copy is not None else 0.0


class ClipTrimModule:
    def __init__(self, ctx: Any, *, ffmpeg_mgr: _ff.FfmpegManager | None = None, shell_link: ShellLinkApi | None = None,
                 sendto_folder: Path | None = None, fallback_dir: Callable[[], Path] | None = None,
                 free_bytes: Callable[[Path], int | None] | None = None, fs_name: Callable[[Path], str | None] | None = None,
                 runner_factory: Callable[[], runner.Runner] | None = None,
                 confirm: Callable[[str, str], bool] | None = None,
                 attributes: Callable[[Path], int | None] | None = None,
                 now: Callable[[], datetime] | None = None) -> None:
        self.ctx = ctx
        self.log: logging.Logger = ctx.log
        section = dict(ctx.settings_dict())
        section.pop("enabled", None)
        merged, changed = cfgmod.normalize(section)
        self.config = cfgmod.parse(merged)
        if changed:
            try:
                ctx.write_settings(merged)
            except Exception as e:  # noqa: BLE001 - 書き戻せなくても既定値で動く
                self.log.warning("settings write-back failed: %s", type(e).__name__)
        self._ffmpeg = ffmpeg_mgr
        self._shell_link = shell_link
        self._sendto_folder = sendto_folder
        self._fallback_dir = fallback_dir
        self._free_bytes = free_bytes
        self._fs_name = fs_name
        self._runner_factory = runner_factory
        self._confirm_fn = confirm
        self._attributes = attributes
        self._now: Callable[[], datetime] = now or datetime.now
        self.signals = Signals()
        self.ops = OpsLog(Path(ctx.data_dir) / "ops.jsonl", now=lambda: self._now())   # 記録の時刻も渡された時計で(usage の区切りと揃える)
        self._stopped = False
        post = self._post
        self.lane_open = Lane("open", post)
        self.lane_frame = Lane("frame", post, delay_s=0.15)   # 最後の操作から 150ms 待つ(T-4)
        self.lane_around = Lane("around", post, delay_s=0.1)  # タイムラインを引く間の読み取りをまとめる
        self.lane_strip = Lane("strip", post)
        self.worker = jobs.ExportWorker(self._env, self._on_job_update, self._on_job_done)
        self.video: Opened | None = None
        self.pos = 0.0
        self.around: Around | None = None
        self.reading = False
        self._pending_action: Callable[[], None] | None = None
        self.frame_png: bytes | None = None
        self.frame_loading = False
        self.frame_error = ""
        self.mark_in: float | None = None
        self.mark_out: float | None = None
        self.segments: list[Segment] = []
        self.thumbs: list[frames.Thumb | None] = []
        self._gen = 0
        self.opening = False
        self.opening_phase = ""
        self.error = ""               # 開けなかった理由(画面用)
        self.job: jobs.Job | None = None
        self.last_result: jobs.JobResult | None = None
        self._last_diag: dict[str, str | int | bool] = {}

    # ================================================================ 共通
    @property
    def accent(self) -> str:
        return catalog_info("cliptrim").accent  # テーマ切替後の色を毎回読む

    def _post(self, fn: Callable[[], None]) -> None:
        if not self._stopped:
            self.ctx.call_soon(lambda: None if self._stopped else fn())

    def ffmpeg(self) -> _ff.FfmpegManager:
        if self._ffmpeg is None:
            self._ffmpeg = _ff.shared(self.log)
        return self._ffmpeg

    def _notice(self, kind: str, text: str) -> None:
        self.signals.notice.emit(kind, text)

    def _lanes(self) -> tuple[Lane, ...]:
        return self.lane_open, self.lane_frame, self.lane_around, self.lane_strip

    def _reset_video_state(self) -> None:
        self._gen += 1
        self.video = None
        self.pos = 0.0
        self.around = None
        self.reading = False
        self._pending_action = None
        self.frame_png = None
        self.frame_loading = False
        self.frame_error = ""
        self.mark_in = self.mark_out = None
        self.segments = []
        self.thumbs = []

    # ================================================================ ライフサイクル
    def start(self) -> None:
        self.ctx.add_tray_action("ClipTrim を開く", self.ctx.show_page)
        self._update_status()
        if self.config.sendto_enabled and self.sendto_status() == sendto.STATUS_REGISTERED:
            sendto.register(self._link_api(), self.sendto_folder())  # exe の場所が変わっていたら作り直す(自分の物だけ)
        self.log.info("cliptrim started")

    def stop(self) -> None:
        self._stopped = True
        self.worker.stop()  # 動いている ffmpeg を終わらせ、このジョブの .part を消してから戻る(FR-13)
        for lane in self._lanes():
            lane.stop()
        self.log.info("cliptrim stopped")

    def handle_cli(self, args: list[str]) -> tuple[int, str]:
        if args[:1] == ["open"]:
            if self.worker.busy():
                return 1, "busy"
            paths = args[1:]
            if not paths:
                return 2, "no path"
            first = paths[0]  # 最初の1つだけ開く(FR-1)
            self.ctx.show_page()
            self.ctx.call_soon(lambda: self.open_video(first))
            return 0, "opened 1"
        return 2, "unsupported"

    def create_page(self) -> QWidget:
        from deskkit.modules.cliptrim.page import ClipTrimPage

        return ClipTrimPage(self)

    def busy(self) -> bool:
        return self.worker.busy()

    def _update_status(self) -> None:
        try:
            if self.worker.busy():
                self.ctx.set_tray_status("書き出し中")
            elif self.video is not None:
                self.ctx.set_tray_status(f"区間 {len(self.segments)} 個")
            else:
                self.ctx.set_tray_status("待機中")
        except Exception:  # noqa: BLE001 - 状態表示の失敗で処理を止めない
            pass

    def _confirm(self, title: str, text: str) -> bool:
        if self._confirm_fn is not None:
            return self._confirm_fn(title, text)
        from deskkit.ui import widgets as W

        ok, _ = W.confirm(self.ctx.window_parent(), title, text, ok_text="開く")
        return ok

    # ================================================================ 開く(FR-1・FR-2)
    def open_video(self, path: str | Path) -> bool:
        if self.worker.busy():
            self._notice("warn", MSG_BUSY)
            return False
        if self.segments and not self._confirm("動画を開く", MSG_DISCARD):
            return False
        p = Path(os.path.abspath(str(path)))
        try:
            is_file = p.is_file()
        except OSError:
            is_file = False
        if not is_file:
            self._fail_open(MSG_NOT_FOUND, "not_found")
            return False
        from deskkit.modules.cliptrim import _win32

        attrs = (self._attributes or _win32.file_attributes)(p)
        if _win32.is_cloud_only(attrs):
            self._fail_open(MSG_CLOUD, "cloud_only")  # 開くと取り寄せが始まるので読まない(§10)
            return False
        for lane in self._lanes():
            lane.clear()
        self._reset_video_state()
        self.opening = True
        self.opening_phase = ""
        self.error = ""
        self.signals.video.emit()
        self.signals.segments.emit()
        self.signals.thumbs.emit(-1)
        mgr = self.ffmpeg()
        gen = self._gen

        def preparing() -> None:
            self._post(lambda: self._set_phase(MSG_PREPARING))

        def work(tok: runner.Token) -> tuple[Path, VideoInfo | None, int]:
            exe = mgr.ensure(preparing)
            info = probe.probe(exe, p, token=tok)
            size = p.stat().st_size
            return exe, info, size

        self.lane_open.submit(work, lambda r, e: self._opened(gen, p, r, e), key="open")
        self.log.info("open requested")
        return True

    def _set_phase(self, text: str) -> None:
        if self.opening:
            self.opening_phase = text
            self.signals.video.emit()

    def _fail_open(self, msg: str, code: str) -> None:
        self.opening = False
        self.opening_phase = ""
        self.error = msg
        self.log.info("open failed code=%s", code)
        self.signals.video.emit()
        self._notice("error", msg)

    def _opened(self, gen: int, p: Path, res: Any, err: BaseException | None) -> None:
        if gen != self._gen:
            return
        self.opening_phase = ""
        if err is not None:
            if isinstance(err, _ff.FfmpegError):
                self._fail_open(FF_MESSAGES.get(err.code, MSG_FF_BROKEN), err.code)
            else:
                self._fail_open(MSG_NOT_VIDEO, f"open_{type(err).__name__}")
            return
        exe, info, size = res
        if info is None:
            self._fail_open(MSG_NOT_VIDEO, "probe_failed")
            return
        if not info.has_video:
            self._fail_open(MSG_NO_VIDEO_STREAM, "no_video")
            return
        if info.duration is None:
            self._fail_open(MSG_NO_DURATION, "no_duration")
            return
        self.opening = False
        self.video = Opened(p, info, size, exe, plan.copy_container(p, info))
        self.log.info("opened seconds=%.1f fps=%s audio=%d subs=%d copy=%s rot=%d hdr=%s", info.duration, info.fps,
                      info.audio_count, info.subtitle_count, self.video.container is not None, info.rotation, info.hdr)
        self.signals.video.emit()
        self.signals.segments.emit()
        self._update_status()
        self.seek(0.0)
        self._start_filmstrip()
        mgr = self.ffmpeg()
        if mgr.h264 is None:
            def check(_tok: runner.Token) -> bool:
                return mgr.check_h264(exe)

            self.lane_open.submit(check, lambda _r, _e: self._h264_checked(), key="h264")

    def _h264_checked(self) -> None:
        self.signals.segments.emit()
        self.signals.info.emit()

    def close_video(self) -> None:
        if self.worker.busy():
            return
        for lane in self._lanes():
            lane.clear()
        self._reset_video_state()
        self.opening = False
        self.error = ""
        self.signals.video.emit()
        self.signals.segments.emit()
        self.signals.thumbs.emit(-1)
        self._update_status()

    # ================================================================ コマと位置(FR-3〜5・T-3・T-4)
    @property
    def duration(self) -> float:
        if self.video is None or self.video.info.duration is None:
            return 0.0
        return self.video.info.duration

    def frame_seconds(self) -> float:
        return self.video.info.frame_seconds if self.video is not None else 1 / 30

    def _clamp(self, t: float) -> float:
        last = max(0.0, self.duration - self.frame_seconds())
        return max(0.0, min(t, last))

    def seek(self, t: float, *, exact: bool = False) -> None:
        """位置 t へ(exact = t がコマの位置そのもの)。コマを取り出し、まわりの読み取りが要れば頼む。"""
        if self.video is None:
            return
        t = t if exact else self._clamp(t)
        self.pos = t
        self._request_frame(t)
        a = self.around
        if not (exact and a is not None and a.covers(t)):
            self._read(t, lambda res: self._settle(t, res))
        self.signals.position.emit()

    def _settle(self, t: float, a: Around) -> None:
        """読み取りの後: 表示しているコマの実際の時刻を位置にする(FR-4)。"""
        f = a.frame_at(t)
        if f is not None and abs(self.pos - t) <= EPS:
            self.pos = f[0]

    def _read(self, t: float, then: Callable[[Around], None]) -> None:
        v = self.video
        if v is None:
            return
        self.reading = True
        gen = self._gen

        def work(tok: runner.Token) -> Around:
            return keyframes.read(v.exe, v.path, t, v.info.start, token=tok)

        def done(res: Any, err: BaseException | None) -> None:
            if gen != self._gen:
                return
            self.reading = False
            if err is not None or res is None:
                self.log.info("around failed code=%s", getattr(err, "code", type(err).__name__))
                self._notice("warn", MSG_AROUND)
                self._pending_action = None
                self.signals.position.emit()
                return
            self.around = res
            then(res)
            self.signals.position.emit()
            act, self._pending_action = self._pending_action, None
            if act is not None:
                act()  # 読み取りの前に押されたボタンを1回だけ効かせる(FR-4)

        self.lane_around.submit(work, done, key="nav")

    def _request_frame(self, t: float) -> None:
        v = self.video
        if v is None:
            return
        self.frame_loading = True
        self.frame_error = ""
        gen = self._gen
        self.signals.frame.emit()

        def work(tok: runner.Token) -> bytes | None:
            return frames.grab_frame(v.exe, v.path, t, token=tok)

        def done(res: Any, err: BaseException | None) -> None:
            if gen != self._gen:
                return
            self.frame_loading = False
            if err is not None or not res:
                self.frame_error = MSG_FRAME  # 15 秒で答えが無いときも(FR-5)
            else:
                self.frame_png = res
            self.signals.frame.emit()

        self.lane_frame.submit(work, done, key="frame")

    def _defer(self, act: Callable[[], None]) -> bool:
        if self.reading:
            self._pending_action = act
            return True
        return False

    def step_frame(self, n: int) -> None:
        if self.video is None or self._defer(lambda: self.step_frame(n)):
            return
        a = self.around
        fs = self.frame_seconds()
        if a is None or a.far:
            self.seek(self.pos + n * fs)  # 切れ目が遠い: コマの速さで計算する(§10)
            return
        if n > 0:
            nf = a.next_frame(self.pos)
            if nf is not None:
                self.seek(nf[0], exact=True)
            elif a.next_key is not None:
                self.seek(a.next_key, exact=True)
            elif not a.eof:
                self.seek(self.pos + fs)
            return
        pf = a.prev_frame(self.pos)
        if pf is not None:
            self.seek(pf[0], exact=True)
            return
        if self.pos <= EPS:
            return
        orig = self.pos

        def then(a2: Around) -> None:
            p = a2.prev_frame(orig)
            self.pos = p[0] if p is not None else max(0.0, orig - fs)
            self._request_frame(self.pos)

        self._read(max(0.0, orig - fs / 2), then)
        self.signals.position.emit()

    def step_seconds(self, d: float) -> None:
        if self.video is None or self._defer(lambda: self.step_seconds(d)):
            return
        self.seek(self.pos + d)

    def step_key(self, n: int) -> None:
        if self.video is None or self._defer(lambda: self.step_key(n)):
            return
        a = self.around
        if a is None:
            return
        if n > 0:
            if a.far:
                self._notice("info", MSG_FAR_KEYS)
            elif a.next_key is not None:
                self.seek(a.next_key, exact=True)
            elif a.horizon:
                self._notice("info", MSG_NO_KEY_AHEAD)
            else:
                self._notice("info", MSG_NO_KEY_END)
            return
        k = a.prev_key(self.pos)
        if k is not None:
            self.seek(k.pos, exact=True)
            return
        if self.pos <= EPS:
            return
        orig = self.pos
        fs = self.frame_seconds()

        def then(a2: Around) -> None:
            k2 = a2.prev_key(orig)
            self.pos = k2.pos if k2 is not None else 0.0
            self._request_frame(self.pos)

        self._read(max(0.0, orig - fs / 2), then)  # 切れ目ちょうどなら1コマ前で読み直す(FR-4)
        self.signals.position.emit()

    def current_frame_seconds(self) -> float:
        a = self.around
        if a is not None and not a.far:
            d = a.duration_of(self.pos)
            if d:
                return d
            nf = a.next_frame(self.pos)
            if nf is not None and nf[0] - self.pos > EPS:
                return nf[0] - self.pos
        return self.frame_seconds()

    # ================================================================ フィルムストリップ(FR-6)
    def _start_filmstrip(self) -> None:
        v = self.video
        if v is None:
            return
        targets = frames.thumb_targets(self.duration, self.config.filmstrip_count)
        self.thumbs = [None] * len(targets)
        gen = self._gen
        self.signals.thumbs.emit(-1)

        def work(tok: runner.Token) -> int:
            n = 0
            for i, t in enumerate(targets):
                if tok.cancelled:
                    break
                th = frames.grab_thumb(v.exe, v.path, i, t, v.info.start, token=tok)
                if th.png is None and not tok.cancelled:
                    th = frames.grab_thumb(v.exe, v.path, i, t, v.info.start, token=tok)  # 1回だけ試し直す
                self._post(functools.partial(self._thumb, gen, th))
                n += 1
            return n

        self.lane_strip.submit(work, lambda _r, _e: None, key="strip")

    def _thumb(self, gen: int, th: frames.Thumb) -> None:
        if gen != self._gen or th.index >= len(self.thumbs):
            return
        self.thumbs[th.index] = th
        self.signals.thumbs.emit(th.index)

    # ================================================================ 区間(FR-7・FR-8)
    def set_in(self) -> None:
        if self.video is None or self.busy():
            return
        self.mark_in = self.pos
        self.signals.position.emit()

    def set_out(self) -> None:
        if self.video is None or self.busy():
            return
        self.mark_out = min(self.duration, self.pos + self.current_frame_seconds())  # 終了のコマは含む(FR-7)
        self.signals.position.emit()

    def add_segment(self) -> str | None:
        """足せなければ理由を返す。"""
        if self.video is None:
            return None
        if self.busy():
            return MSG_BUSY
        s, e = self.mark_in, self.mark_out
        if s is None or e is None:
            return MSG_NEED_MARKS
        if e <= s + EPS:
            return MSG_END_BEFORE
        if any(x.start < e - EPS and s < x.end - EPS for x in self.segments):
            return MSG_OVERLAP
        if len(self.segments) >= plan.MAX_SEGMENTS:
            return MSG_TOO_MANY
        seg = Segment(s, e)
        self.segments.append(seg)
        self.segments.sort(key=lambda x: x.start)  # 区間は時刻の順(T-8)
        self.mark_in = self.mark_out = None
        self.log.info("segment added count=%d", len(self.segments))
        if self.effective_mode() == "copy":
            self._resolve_copy(seg)
        self.signals.segments.emit()
        self.signals.position.emit()
        self._update_status()
        return None

    def remove_segment(self, index: int) -> None:
        if self.busy() or not 0 <= index < len(self.segments):
            return
        del self.segments[index]
        self.signals.segments.emit()
        self.signals.position.emit()
        self._update_status()

    def jump_segment(self, index: int) -> None:
        if 0 <= index < len(self.segments):
            self.seek(self.segments[index].start, exact=True)

    def _resolve_copy(self, seg: Segment) -> None:
        v = self.video
        if v is None or v.container is None:
            return
        seg.copy_state = "reading"
        gen = self._gen
        mk = plan.is_matroska(v.info)

        def work(tok: runner.Token) -> CopyStart:
            return keyframes.resolve_copy_start(v.exe, v.path, seg.start, v.info.start, matroska=mk, token=tok)

        def done(res: Any, err: BaseException | None) -> None:
            if gen != self._gen or seg not in self.segments:
                return
            if err is not None or res is None:
                seg.copy_state = "error"
                self.log.info("copy start failed code=%s", getattr(err, "code", type(err).__name__))
            else:
                seg.copy, seg.copy_state = res, "ok"
            self.signals.segments.emit()

        self.lane_around.submit(work, done)

    def copy_pending(self) -> bool:
        return any(s.copy_state == "reading" for s in self.segments)

    def overlap_note(self, index: int) -> float:
        """画質そのまま・つなげて1本で、実際の開始が前の区間の終わりより前になる秒数(§10)。"""
        if index == 0 or index >= len(self.segments):
            return 0.0
        seg = self.segments[index]
        if seg.copy is None:
            return 0.0
        return max(0.0, self.segments[index - 1].end - seg.copy.actual)

    # ================================================================ 切り方・書き出し方(FR-9・FR-10)
    def copy_ok(self) -> bool:
        return self.video is not None and self.video.container is not None

    def precise_state(self) -> str:
        """untested / available / unavailable"""
        return self.ffmpeg().h264_state()

    def effective_mode(self) -> str:
        if self.video is not None and not self.copy_ok():
            return "precise"
        if self.config.mode == "precise" and self.precise_state() == "unavailable":
            return "copy"
        return self.config.mode

    def can_cut(self) -> bool:
        return self.copy_ok() or self.precise_state() != "unavailable"

    def mode_notes(self) -> list[tuple[str, str]]:
        """(種類, 文) の並び。画面の書き出しの欄に出す。"""
        v = self.video
        out: list[tuple[str, str]] = []
        if v is None:
            return out
        if not self.can_cut():
            out.append(("error", MSG_CANNOT))
            return out
        if not self.copy_ok():
            out.append(("warn", MSG_NO_COPY))
        if self.precise_state() == "unavailable":
            out.append(("warn", MSG_NO_PRECISE))
        if self.effective_mode() == "precise" and v.info.hdr:
            out.append(("warn", MSG_HDR))
        if v.info.subtitle_count:
            out.append(("info", MSG_SUBS))
        return out

    def set_mode(self, mode: str) -> str | None:
        if self.busy():
            return MSG_BUSY
        if mode not in plan.MODES:
            return None
        err = self.update_settings(lambda s: s.__setitem__("mode", mode))
        if self.effective_mode() == "copy":
            for seg in self.segments:
                if seg.copy is None and seg.copy_state != "reading":
                    self._resolve_copy(seg)  # 画質そのままに切り替えたときにも実際の開始を確かめる(T-5)
        self.signals.segments.emit()
        return err

    def set_output(self, output: str) -> str | None:
        if self.busy():
            return MSG_BUSY
        if output not in plan.OUTPUTS:
            return None
        err = self.update_settings(lambda s: s.__setitem__("output", output))
        self.signals.segments.emit()
        return err

    def set_strip_metadata(self, strip: bool) -> str | None:
        """撮影場所などの情報を消す/残す(回答 Q-2: 選んだものを覚える)。"""
        if self.busy():
            return MSG_BUSY
        err = self.update_settings(lambda s: s.__setitem__("strip_metadata", bool(strip)))
        self.signals.segments.emit()
        return err

    def effective_output(self) -> str:
        return self.config.output if len(self.segments) > 1 else "separate"

    # ================================================================ 見積もりと書き出し(FR-11〜16)
    def build_request(self) -> jobs.ExportRequest | None:
        v = self.video
        if v is None or not self.segments:
            return None
        mode = self.effective_mode()
        segs: list[plan.Seg] = []
        for s in self.segments:
            if mode == "copy":
                if s.copy is None:
                    return None
                segs.append(plan.Seg(s.start, s.end, s.copy.ss, s.copy.actual))
            else:
                segs.append(plan.precise_seg(s.start, s.end))
        return jobs.ExportRequest(v.path, v.info, v.size, mode, self.effective_output(), self.config.strip_metadata,
                                  tuple(segs), plan.video_kbps(v.info, v.size), v.container if mode == "copy" else None)

    def estimate_text(self) -> str:
        req = self.build_request()
        if req is None:
            return ""
        est = req.estimates()
        extra = "(一時ファイルを含む)" if req.join and req.mode == "copy" else ""
        return f"見積もり 約 {plan.fmt_bytes(sum(est))}{extra}"

    def export(self) -> str | None:
        """書き出しを始める。始められなければ理由を返す。"""
        if self.video is None or not self.segments:
            return None
        if self.busy():
            return MSG_BUSY
        if not self.can_cut():
            return MSG_CANNOT
        if self.effective_mode() == "copy":
            for s in self.segments:
                if s.copy is None and s.copy_state != "reading":
                    self._resolve_copy(s)
            if self.copy_pending():
                return MSG_WAIT_COPY
        req = self.build_request()
        if req is None:
            return MSG_WAIT_COPY
        job = jobs.Job(req)
        if not self.worker.submit(job):
            return MSG_BUSY
        self.job = job
        self.last_result = None
        self.log.info("export start mode=%s output=%s segments=%d", req.mode, "join" if req.join else "separate",
                      len(req.segs))
        self.signals.job.emit()
        self.signals.segments.emit()
        self._update_status()
        return None

    def cancel_export(self) -> None:
        self.worker.cancel()
        self.log.info("export cancel requested")

    def _env(self) -> jobs.Env:
        v = self.video
        exe = v.exe if v is not None else self.ffmpeg().ensure()
        kw: dict[str, Any] = {}
        if self._fallback_dir is not None:
            kw["fallback_dir"] = self._fallback_dir
        if self._free_bytes is not None:
            kw["free_bytes"] = self._free_bytes
        if self._fs_name is not None:
            kw["fs_name"] = self._fs_name
        if self._runner_factory is not None:
            kw["runner_factory"] = self._runner_factory
        return jobs.Env(exe, self.log, self.ops, **kw)

    def _on_job_update(self, job: jobs.Job) -> None:  # 書き出しのスレッドから
        self._post(self.signals.job.emit)

    def _on_job_done(self, job: jobs.Job) -> None:  # 書き出しのスレッドから
        self._post(lambda: self._job_finished(job))

    def _job_finished(self, job: jobs.Job) -> None:
        res = job.result
        self.last_result = res
        if res is not None:
            self._last_diag = {"last_mode": job.req.mode, "last_segments": len(job.req.segs), "last_result": res.result,
                               "last_seconds": int(round(res.ms / 1000))}
        self.signals.job.emit()
        self.signals.segments.emit()
        self._update_status()
        if res is not None and res.result != "cancelled" and self.ctx.window_parent() is None:
            self.ctx.notify("ClipTrim", f"切り出しが終わりました({res.ok_count} 本)", self.ctx.show_page,
                            level="ok" if res.result == "ok" else "warn")

    def open_folder(self, path: Path | None = None) -> bool:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        target = path.parent if path is not None else (self.last_result.folder if self.last_result else None)
        if target is None:
            return False
        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(target))))

    # ================================================================ 設定
    def update_settings(self, mutate: Callable[[dict[str, Any]], None], *, restart: bool = False) -> str | None:
        sec = dict(self.ctx.settings_dict())
        sec.pop("enabled", None)
        merged, _ = cfgmod.normalize(sec)
        mutate(merged)
        merged, _ = cfgmod.normalize(merged)
        try:
            self.ctx.write_settings(merged, restart=restart)
        except Exception as e:  # noqa: BLE001 - SettingsError などを画面に返す
            self.log.warning("settings write failed: %s", type(e).__name__)
            return "設定を保存できませんでした"
        self.config = cfgmod.parse(merged)
        self.signals.info.emit()
        return None

    def set_filmstrip_count(self, n: int) -> str | None:
        n = max(cfgmod.FILMSTRIP_MIN, min(cfgmod.FILMSTRIP_MAX, int(n)))
        err = self.update_settings(lambda s: s.__setitem__("filmstrip_count", n))
        if err is None and self.video is not None:
            self.lane_strip.clear()
            self._start_filmstrip()
        return err

    # ---- 「送る」(回答 Q-5)
    def _link_api(self) -> ShellLinkApi:
        if self._shell_link is None:
            from deskkit.modules.cliptrim._win32 import RealShellLink

            self._shell_link = RealShellLink()
        return self._shell_link

    def sendto_folder(self) -> Path:
        return self._sendto_folder or sendto.default_folder()

    def sendto_status(self) -> str:
        try:
            return sendto.status(self._link_api(), self.sendto_folder())
        except OSError:
            return sendto.STATUS_ABSENT

    def set_sendto(self, enabled: bool) -> str | None:
        api, folder = self._link_api(), self.sendto_folder()
        if enabled:
            err = sendto.register(api, folder)
            if err:
                self.log.info("sendto register failed")
                return err
        else:
            r = sendto.unregister(api, folder)
            self.log.info("sendto unregister result=%s", r)
            if r == "failed":
                return "「送る」から外せませんでした"
        self.log.info("sendto enabled=%s", enabled)
        return self.update_settings(lambda s: s.__setitem__("sendto_enabled", enabled))

    # ================================================================ 利用状況・診断(FR-18・FR-19)
    def usage(self, days: int) -> list[UsageSeries]:
        rows = self.ops.read()
        today: date = self._now().date()

        def files(mode: str | None) -> Callable[[dict[str, Any]], int]:
            def v(r: dict[str, Any]) -> int:
                if mode is not None and r.get("mode") != mode:
                    return 0
                return int(r.get("files_ok") or 0)
            return v

        return [
            UsageSeries("clips", "切り出した本数", sum_per_day(rows, days, files(None), today), unit="本", primary=True,
                        hint="公開から 90 日で 0 本なら紹介から外す(R-2)"),
            UsageSeries("copy", "画質そのまま", sum_per_day(rows, days, files("copy"), today), unit="本",
                        good_when="neutral"),
            UsageSeries("precise", "ぴったり", sum_per_day(rows, days, files("precise"), today), unit="本",
                        good_when="neutral"),
        ]

    def diagnostics(self) -> dict[str, str | int | bool]:
        mgr = self.ffmpeg()
        d: dict[str, str | int | bool] = {
            "ffmpeg": mgr.state(),
            "h264_encoder": mgr.h264_state(),
            "video_open": self.video is not None,
            "segments": len(self.segments),
            "mode": self.config.mode,
            "output": self.config.output,
            "strip_metadata": self.config.strip_metadata,
            "sendto": self.sendto_status(),
            "busy": self.busy(),
        }
        d.update(self._last_diag)
        return d
