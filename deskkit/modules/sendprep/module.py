# SendPrep モジュール本体。キュー(最大 200 件)・プリセット・ワーカー・伏せ字エディタ・クリップボード・「送る」の登録を束ね、
# Control Center の画面に操作窓口を出す。利用者が操作したときだけ動く(V-1)。Pillow・winrt は最初に使うときに読む(NFR-5)。
# ログ・ops.jsonl・diagnostics にはファイル名・パス・文字認識で読んだ文字列を書かない(VINV-4)。
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Signal

from deskkit.catalog import info
from deskkit.modules.sendprep import config as cfgmod
from deskkit.modules.sendprep import jobs as J
from deskkit.modules.sendprep import oplog, sendto
from deskkit.modules.sendprep.oplog import OpsLog
from deskkit.modules.sendprep.video import FfmpegManager, bundled_dir
from deskkit.usage import UsageSeries, count_jsonl

if TYPE_CHECKING:
    from PIL import Image
    from PySide6.QtWidgets import QWidget

    from deskkit.modules.sendprep._win32 import ShellLinkApi
    from deskkit.modules.sendprep.ocr import Candidate
    from deskkit.modules.sendprep.redact import RedactEditor

MAX_QUEUE = 200
MSG_QUEUE_FULL = "一度に整えられるのは 200 件までです"
MSG_NO_CLIP_IMAGE = "クリップボードに画像がありません"
Rect = tuple[int, int, int, int]


class Signals(QObject):
    changed = Signal()          # キューの行が増えた・減った
    job_updated = Signal(int)   # 1件の状態・進捗が変わった
    batch_done = Signal()       # 積んだ分が全部終わった(FR-18)
    info_changed = Signal()     # 設定・動画の部品・文字認識・「送る」の状態


@dataclass(frozen=True)
class AddResult:
    added: int
    unsupported: int
    duplicates: int
    over_limit: int


@dataclass(frozen=True)
class Summary:
    done: int
    failed: int
    outputs: tuple[Path, ...]


class SendPrepModule:
    def __init__(self, ctx: Any, *, shell_link: ShellLinkApi | None = None, sendto_folder: Path | None = None,
                 fallback_dir: Callable[[], Path] | None = None, bundle: Callable[[], Path | None] | None = None,
                 ocr_find: Callable[[Image.Image, Sequence[str]], list[Candidate]] | None = None,
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
        self._now: Callable[[], datetime] = now or datetime.now
        self.signals = Signals()
        self.ops = OpsLog(Path(ctx.data_dir) / "ops.jsonl")
        self.ffmpeg = FfmpegManager(Path(ctx.data_dir), bundle or bundled_dir, self.log)
        self._shell_link = shell_link
        self._sendto_folder = sendto_folder
        self._ocr_find = ocr_find
        env_kw: dict[str, Any] = {}
        if fallback_dir is not None:
            env_kw["fallback_dir"] = fallback_dir
        self.env = J.Env(self.ffmpeg, self.ops, self.log, rename_to_date=lambda: self.config.rename_to_date,
                         now=self._now, on_update=self._on_job_update, **env_kw)
        self.worker = J.Worker(self.env, self._on_idle)
        self.jobs: list[J.Job] = []
        self._next_id = 1
        self.unsupported = 0
        self.queue_full = False
        self.batch: list[J.Job] = []
        self.summary: Summary | None = None
        self._editors: dict[int, RedactEditor] = {}
        self._clip_editor: RedactEditor | None = None
        self._stopped = False
        self._ocr_state = "untested"

    # ================================================================ ライフサイクル
    @property
    def accent(self) -> str:
        return info("sendprep").accent  # テーマ切替後の色を毎回読む

    def start(self) -> None:
        self.ctx.add_tray_action("SendPrep を開く", self.ctx.show_page)
        self.ctx.add_tray_action("クリップボードの画像を整える", self.open_clipboard_editor)
        self._update_status()
        self.log.info("sendprep started presets=%d", len(self.config.presets))

    def stop(self) -> None:
        self._stopped = True
        self.worker.stop()
        eds: list[RedactEditor] = list(self._editors.values())
        if self._clip_editor is not None:
            eds.append(self._clip_editor)
        for ed in eds:
            try:
                ed.close()
            except RuntimeError:
                pass
        self._editors.clear()
        self._clip_editor = None
        self.log.info("sendprep stopped")

    def handle_cli(self, args: list[str]) -> tuple[int, str]:
        if args[:1] == ["open"]:
            res = self.add_paths(args[1:1 + MAX_QUEUE])
            if len(args) - 1 > MAX_QUEUE:
                self.queue_full = True  # §10: 201 件以上は先頭の 200 件だけ積み、FR-3 の文を出す
                self.signals.changed.emit()
            self.ctx.show_page()
            return 0, f"queued {res.added}"
        return 2, "unsupported"

    def create_page(self) -> QWidget:
        from deskkit.modules.sendprep.page import SendPrepPage

        return SendPrepPage(self)

    # ================================================================ キュー(FR-1〜4)
    def active_jobs(self) -> list[J.Job]:
        return [j for j in self.jobs if not j.finished]

    def pending_jobs(self) -> list[J.Job]:
        return [j for j in self.jobs if j.state == J.STATE_PENDING]

    def busy(self) -> bool:
        return self.worker.busy()

    def add_paths(self, paths: Iterable[Any]) -> AddResult:
        files, unsupported = J.expand_inputs(list(paths))
        if self.jobs and not self.busy() and all(j.finished for j in self.jobs) and not self._editors:
            self.clear_finished()  # 前回の分が全部終わっていれば、新しい一覧として始める
        keys = {os.path.normcase(os.path.abspath(j.path)) for j in self.active_jobs()}
        added = dup = over = 0
        for f in files:
            key = os.path.normcase(os.path.abspath(f))
            if key in keys:
                dup += 1  # §10: 同じファイルは2回積まない(何も出さない)
                continue
            if len(self.active_jobs()) >= MAX_QUEUE:
                over += 1
                continue
            kind = J.kind_of(f)
            if kind is None:
                continue
            self.jobs.append(J.Job(self._next_id, Path(os.path.abspath(f)), kind))
            self._next_id += 1
            keys.add(key)
            added += 1
        self.unsupported += unsupported
        if over:
            self.queue_full = True
        self.log.info("queue add added=%d unsupported=%d duplicates=%d over=%d", added, unsupported, dup, over)
        self.signals.changed.emit()
        self._update_status()
        return AddResult(added, unsupported, dup, over)

    def remove_job(self, job: J.Job) -> None:
        if job.state in (J.STATE_QUEUED, J.STATE_RUNNING):
            return
        if job in self.jobs:
            self.jobs.remove(job)
        if len(self.active_jobs()) < MAX_QUEUE:
            self.queue_full = False
        self.signals.changed.emit()
        self._update_status()

    def clear_finished(self) -> None:
        self.jobs = [j for j in self.jobs if not j.finished]
        self.unsupported = 0
        self.queue_full = False
        self.summary = None
        self.batch = [j for j in self.batch if not j.finished]
        self.signals.changed.emit()
        self._update_status()

    def job(self, jid: int) -> J.Job | None:
        return next((j for j in self.jobs if j.id == jid), None)

    # ================================================================ 処理
    def start_processing(self, preset_id: str) -> int:
        preset = self.config.preset(preset_id) or self.config.preset("meta")
        assert preset is not None
        pend = self.pending_jobs()
        if not pend:
            return 0
        if preset.id != self.config.last_preset:
            pid = preset.id
            self.update_settings(lambda s: s.__setitem__("last_preset", pid))
        for j in pend:
            j.preset_id = preset.id
            j.limit_bytes = preset.limit_bytes
        if not self.busy():
            self.batch = []
            self.summary = None
        self.batch.extend(pend)
        self.worker.submit(pend)
        self.log.info("process start preset=%s count=%d", preset.id, len(pend))
        self.signals.changed.emit()
        self._update_status()
        return len(pend)

    def cancel_job(self, job: J.Job) -> None:
        self.worker.cancel(job)
        self.log.info("job cancel requested kind=%s", job.kind)

    def _on_job_update(self, job: J.Job) -> None:  # ワーカーのスレッドから
        jid = job.id
        self.ctx.call_soon(lambda: self._emit_job(jid))

    def _emit_job(self, jid: int) -> None:
        if not self._stopped:
            self.signals.job_updated.emit(jid)
            self._update_status()

    def _on_idle(self) -> None:  # ワーカーのスレッドから
        self.ctx.call_soon(self._batch_finished)

    def _batch_finished(self) -> None:
        if self._stopped or self.busy():
            return
        done = [j for j in self.batch if j.state == J.STATE_DONE]
        failed = [j for j in self.batch if j.state in (J.STATE_FAILED, J.STATE_CANCELLED)]
        self.summary = Summary(len(done), len(failed), tuple(j.out_path for j in done if j.out_path is not None))
        self.log.info("batch done ok=%d failed=%d", len(done), len(failed))
        self.signals.batch_done.emit()
        self.signals.info_changed.emit()
        self._update_status()
        if self.batch and self.ctx.window_parent() is None:
            self.ctx.notify("SendPrep", f"完了 {len(done)} 件・失敗 {len(failed)} 件", self.ctx.show_page,
                            level="ok" if not failed else "warn")

    def _update_status(self) -> None:
        n = len([j for j in self.jobs if j.state in (J.STATE_QUEUED, J.STATE_RUNNING)])
        try:
            self.ctx.set_tray_status(f"処理中 {n} 件" if n else "待機中")
        except Exception:  # noqa: BLE001 - 状態表示の失敗で処理を止めない
            pass

    # ================================================================ 結果の受け渡し(FR-18)
    def outputs(self) -> list[Path]:
        return [j.out_path for j in self.jobs if j.state == J.STATE_DONE and j.out_path is not None and j.out_path.exists()]

    def copy_outputs(self) -> int:
        """出力をファイル(CF_HDROP)としてクリップボードに置く。エクスプローラー・Discord に貼り付けられる。"""
        from PySide6.QtCore import QMimeData, QUrl
        from PySide6.QtGui import QGuiApplication

        outs = self.outputs()
        if not outs:
            return 0
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(str(p)) for p in outs])
        md.setData('application/x-qt-windows-mime;value="Preferred DropEffect"', b"\x01\x00\x00\x00")  # コピー
        QGuiApplication.clipboard().setMimeData(md)
        self.log.info("outputs copied count=%d", len(outs))
        return len(outs)

    def open_output_folder(self) -> bool:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        outs = self.outputs()
        if not outs:
            return False
        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(outs[-1].parent))))

    # ================================================================ 伏せ字(FR-10〜13)
    def find_candidates(self, img: Image.Image) -> list[Candidate]:
        words = list(self.config.my_words)
        if self._ocr_find is not None:
            return self._ocr_find(img, words)
        from deskkit.modules.sendprep import ocr

        try:
            lines = ocr.recognize(img)  # 使えなければ OcrUnavailableError(エディタが FR-13 の文を出す)
        except ocr.OcrUnavailableError:
            self._ocr_state = "unavailable"
            raise
        self._ocr_state = "available"
        return ocr.find_candidates(lines, words)

    def _save_style(self, style: str) -> None:
        if style != self.config.redact_style:
            self.update_settings(lambda s: s.__setitem__("redact_style", style))

    def _run_bg(self, name: str, work: Callable[[], Any], done: Callable[[Any, BaseException | None], None]) -> None:
        def run() -> None:
            res: Any = None
            err: BaseException | None = None
            try:
                res = work()
            except Exception as e:  # noqa: BLE001 - 結果として画面へ返す
                err = e
            self.ctx.call_soon(lambda: done(res, err))

        threading.Thread(target=run, name=f"sendprep-{name}", daemon=True).start()

    def open_editor(self, job: J.Job) -> None:
        if job.kind != "image" or job.state != J.STATE_DONE or job.out_path is None:
            return
        ed = self._editors.get(job.id)
        if ed is not None:
            try:
                ed.raise_()
                ed.activateWindow()
                return
            except RuntimeError:
                self._editors.pop(job.id, None)
        out = job.out_path

        def loaded(img: Any, err: BaseException | None) -> None:
            from deskkit.ui import widgets as W

            if err is not None or img is None:
                msg = J.MSG_NO_OUTPUT if isinstance(err, J.JobError) and err.code == "unreadable" else J.MSG_BAD_IMAGE
                W.message(self.ctx.window_parent(), "伏せ字", msg, kind="error")
                return
            self._show_editor(job, img)

        self._run_bg("load", lambda: J.load_for_edit(out), loaded)

    def _show_editor(self, job: J.Job, img: Image.Image) -> RedactEditor:
        from deskkit.modules.sendprep.redact import RedactEditor

        holder: list[RedactEditor] = []

        def apply(rects: list[Rect], style: str) -> None:
            ed = holder[0]

            def done(_r: Any, err: BaseException | None) -> None:
                try:
                    if err is None:
                        ed.finish()
                    else:
                        msg = err.message if isinstance(err, J.JobError) else J.MSG_FAILED
                        if not isinstance(err, J.JobError):
                            self.log.error("burn failed: %s", type(err).__name__)
                        ed.fail(msg, close=isinstance(err, J.JobError) and err.code == "no_output")
                except RuntimeError:
                    pass  # エディタが先に閉じられた
                self.signals.job_updated.emit(job.id)
                self.signals.info_changed.emit()

            self._run_bg("burn", lambda: J.burn_output(job, rects, style, self.env), done)

        ed = RedactEditor(self.ctx.window_parent(), img, accent=self.accent, style=self.config.redact_style,
                          on_apply=apply, title=job.path.name, find=self.find_candidates, on_style=self._save_style)
        holder.append(ed)
        self._editors[job.id] = ed
        jid = job.id
        ed.finished.connect(lambda _r=0: self._editors.pop(jid, None))
        ed.show()
        return ed

    # ================================================================ クリップボードの画像(FR-19)
    def open_clipboard_editor(self) -> bool:
        from PySide6.QtGui import QGuiApplication

        from deskkit.modules.sendprep.redact import RedactEditor, qimage_to_pil

        if self._clip_editor is not None:
            try:
                self._clip_editor.raise_()
                self._clip_editor.activateWindow()
                return True
            except RuntimeError:
                self._clip_editor = None
        cb = QGuiApplication.clipboard()
        md = cb.mimeData()
        q = cb.image() if md is not None and md.hasImage() else None
        if q is None or q.isNull():
            self.ctx.notify("SendPrep", MSG_NO_CLIP_IMAGE, level="info")
            return False
        img = qimage_to_pil(q)  # 利用者が押したときに1回だけ読む(監視はしない)
        in_bytes = img.width * img.height * 4
        holder: list[RedactEditor] = []

        def apply(rects: list[Rect], style: str) -> None:
            from deskkit.modules.sendprep.redact import burn

            t0 = time.monotonic()
            ed = holder[0]

            def done(res: Any, err: BaseException | None) -> None:
                ms = int((time.monotonic() - t0) * 1000)
                if err is not None or res is None:
                    self.log.error("clipboard burn failed: %s", type(err).__name__ if err else "none")
                    self.ops.write(kind="clipboard", preset="meta", result="error", in_bytes=in_bytes, out_bytes=None,
                                   redactions=len(rects), ms=ms)
                    try:
                        ed.fail(J.MSG_FAILED)
                    except RuntimeError:
                        pass
                    return
                from deskkit.modules.sendprep.redact import pil_to_qimage

                qi = pil_to_qimage(res)  # 画素だけの新しい画像(メタデータを持たない)
                QGuiApplication.clipboard().setImage(qi)
                self.ops.write(kind="clipboard", preset="meta", result="ok", in_bytes=in_bytes,
                               out_bytes=qi.width() * qi.height() * 4, redactions=len(rects), ms=ms)
                self.log.info("clipboard result=ok redactions=%d", len(rects))
                self.signals.info_changed.emit()
                try:
                    ed.finish()
                except RuntimeError:
                    pass
                self.ctx.notify("SendPrep", "整えた画像をクリップボードに置きました", level="ok")

            self._run_bg("clip", lambda: burn(img, rects, style), done)

        ed = RedactEditor(self.ctx.window_parent(), img, accent=self.accent, style=self.config.redact_style,
                          on_apply=apply, title="クリップボードの画像", clipboard_mode=True, find=self.find_candidates,
                          on_style=self._save_style)
        holder.append(ed)
        self._clip_editor = ed
        ed.finished.connect(lambda _r=0: setattr(self, "_clip_editor", None))
        ed.show()
        return True

    # ================================================================ 設定
    def update_settings(self, mutate: Callable[[dict[str, Any]], None], *, restart: bool = False) -> str | None:
        """設定を書き換えて保存する。成功なら None、失敗なら利用者向けの短い理由。"""
        sec = dict(self.ctx.settings_dict())
        sec.pop("enabled", None)
        merged, _ = cfgmod.normalize(sec)
        try:
            mutate(merged)
        except ValueError as e:
            return str(e)
        merged, _ = cfgmod.normalize(merged)
        try:
            self.ctx.write_settings(merged, restart=restart)
        except Exception as e:  # noqa: BLE001 - SettingsError などを画面に返す
            self.log.warning("settings write failed: %s", type(e).__name__)
            return "設定を保存できませんでした"
        self.config = cfgmod.parse(merged)
        self.signals.info_changed.emit()
        return None

    def add_preset(self, label: str, limit_mb: int) -> str | None:
        def mut(s: dict[str, Any]) -> None:
            cfgmod.add_custom(s, label, limit_mb)

        return self.update_settings(mut)

    def rename_preset(self, pid: str, label: str) -> str | None:
        return self.update_settings(lambda s: cfgmod.rename_custom(s, pid, label))

    def set_preset_limit(self, pid: str, limit_mb: int) -> str | None:
        return self.update_settings(lambda s: cfgmod.set_custom_limit(s, pid, limit_mb))

    def delete_preset(self, pid: str) -> str | None:
        return self.update_settings(lambda s: cfgmod.delete_custom(s, pid))

    def set_my_words(self, words: Sequence[str]) -> str | None:
        cleaned = [w for w in (cfgmod.clean_word(x) for x in words) if w is not None]
        if len(cleaned) != len(list(words)):
            return f"言葉は 1〜{cfgmod.WORD_MAX_CHARS} 文字で入れてください"
        if len(cleaned) > cfgmod.MAX_WORDS:
            return f"登録できる言葉は {cfgmod.MAX_WORDS} 個までです"
        return self.update_settings(lambda s: s.__setitem__("my_words", cleaned))

    # ---- 「送る」(FR-20)
    def _link_api(self) -> ShellLinkApi:
        if self._shell_link is None:
            from deskkit.modules.sendprep._win32 import RealShellLink

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

    # ================================================================ 利用状況・診断(FR-22・FR-23)
    def _count(self, days: int, pick: Callable[[dict[str, Any]], bool], today: date | None = None) -> list[int]:
        t = today or self._now().date()
        total = [0] * days
        for p in self.ops.paths():
            per = count_jsonl(p, days, pick, today=t)
            total = [a + b for a, b in zip(total, per, strict=True)]
        return total

    def usage(self, days: int) -> list[UsageSeries]:
        prepared = self._count(days, oplog.is_prepared)
        redacted = self._count(days, oplog.is_redacted)
        return [
            UsageSeries("prepared", "整えた件数", prepared, unit="件", primary=True,
                        hint="公開から 90 日で 0 件なら紹介から外す(R-2)"),
            UsageSeries("redacted", "伏せ字を焼き込んだ件数", redacted, unit="件", good_when="neutral"),
        ]

    def today_prepared(self) -> int:
        return self._count(1, oplog.is_prepared)[0]

    def ocr_state(self) -> str:
        return self._ocr_state

    def check_ocr(self) -> str:
        """文字認識が使えるか(winrt を読むので GUI スレッドの外で呼ぶ)。"""
        from deskkit.modules.sendprep import ocr

        ok, _reason = ocr.availability()
        self._ocr_state = "available" if ok else "unavailable"
        return self._ocr_state

    def diagnostics(self) -> dict[str, str | int | bool]:
        return {
            "ffmpeg": self.ffmpeg.state(),
            "h264_encoder": self.ffmpeg.h264_state(),
            "ocr": self._ocr_state,
            "sendto": self.sendto_status(),
            "presets": len(self.config.presets),
            "custom_presets": len(self.config.custom_presets()),
            "queue": len(self.active_jobs()),
            "busy": self.busy(),
            "rename_to_date": self.config.rename_to_date,
            "redact_style": self.config.redact_style,
            "my_words": len(self.config.my_words),
        }
