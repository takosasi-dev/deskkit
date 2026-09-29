# JotDrop モジュール本体。ホットキー → 前面の確認(ゲーム・全画面なら出さない)→ 入力欄 → Enter の瞬間の時刻で1行と書き込み先を決め、
# 窓を閉じて元の窓へ戻し、ワーカーが末尾に足す。預かり・自動の書き直し・取り消し・照合・トレイ・画面の窓口を束ねる。
# ログ・ops.jsonl・diagnostics・usage・通知・窓の題に本文とパスを書かない(J-15・INV-3)。本文とパスは pending.jsonl だけ。
from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, QPoint, Signal

from deskkit.catalog import info
from deskkit.modules.jotdrop import compose, target, writer
from deskkit.modules.jotdrop._win32 import Win32Api
from deskkit.modules.jotdrop.oplog import OpLog
from deskkit.modules.jotdrop.pending import PendingItem, PendingStore
from deskkit.usage import UsageSeries, count_jsonl

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

    from deskkit.modules.jotdrop.popup import DoneMark, MemoPopup

HOTKEY_NAME = "jotdrop.open_input"   # ホームのホットキー一覧では「一行メモを書く」(H4-7)
PRESET_BULLET = ("- {time} {text}", False)
PRESET_HEADING = ("### [{time}] {text}", True)
DEFAULTS: dict[str, Any] = {
    "hotkey": "",
    "folder": "",
    "file_pattern": "{date}.md",
    "create_file": True,
    "new_file_header": "",
    "line_format": PRESET_BULLET[0],
    "blank_line_before": PRESET_BULLET[1],
    "separator": "",
    "newline": "auto",
    "max_chars": 1000,
    "undo_minutes": 10,
    "show_done_mark": True,
    "target_confirmed": False,
}
RANGES = {"max_chars": (20, 5000), "undo_minutes": (1, 60)}
NEWLINES = ("auto", "lf", "crlf")
RETRY_INTERVAL_MS = 60_000       # FR-12
VERIFY_DELAY_MS = 10_000         # J-19
POPUP_PREBUILD_MS = 3_000        # 入力欄を前もって作るのは start() の 3 秒後(NFR4-2 を守るため)
F12 = 0x7B


def hotkey_problem(text: str) -> str | None:
    """J-1: F12 と Windows キーを含む組み合わせは選べない。解釈できない書き方も。"""
    from deskkit.hotkeys import MOD_WIN, parse_hotkey

    if not text:
        return None
    try:
        mods, vk = parse_hotkey(text)
    except ValueError:
        return "このキーの書き方は使えません"
    if vk == F12:
        return "F12 はデバッガー用に Windows が予約しているため選べません"
    if mods & MOD_WIN:
        return "Windows キーを含む組み合わせは Windows 用に予約されているため選べません"
    return None


def normalize(section: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """型・範囲の合わない値を既定値に戻す(書式が壊れていると展開できないため)。(設定, 変えたか)。"""
    out = dict(section)
    changed = False
    for k, v in DEFAULTS.items():
        cur = out.get(k)
        ok = isinstance(cur, bool) if isinstance(v, bool) else (isinstance(cur, type(v)) and not isinstance(cur, bool))
        if not ok:
            out[k] = v
            changed = True
    for k, (lo, hi) in RANGES.items():
        n = max(lo, min(hi, int(out[k])))
        if n != out[k]:
            out[k] = n
            changed = True
    checks: list[tuple[str, Callable[[str], str | None]]] = [
        ("line_format", compose.validate_line_format),
        ("new_file_header", compose.validate_header),
        ("separator", compose.validate_separator),
        ("file_pattern", lambda p: compose.validate_pattern(p, datetime.now())),
    ]
    for k, fn in checks:
        if fn(out[k]) is not None:
            out[k] = DEFAULTS[k]
            changed = True
    if out["newline"] not in NEWLINES:
        out["newline"] = "auto"
        changed = True
    return out, changed


def format_kind(cfg: dict[str, Any]) -> str:
    pair = (cfg["line_format"], bool(cfg["blank_line_before"]))
    return "bullet" if pair == PRESET_BULLET else "heading" if pair == PRESET_HEADING else "custom"


class Notifier(QObject):
    changed = Signal()             # 預かり・取り消し・キーの状態・今日の件数が変わった


@dataclass
class LastMemo:
    """直前に送ったメモ(取り消し用。メモリだけ。DeskKit を終了すると消える)。"""

    job_id: str
    text: str
    when: datetime
    record: writer.UndoRecord | None = None
    pending: bool = False
    written: bool = False


def _default_documents() -> str:
    from PySide6.QtCore import QStandardPaths

    return QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation) or str(Path.home() / "Documents")


def _default_startfile(target_path: str) -> None:
    os.startfile(target_path)  # type: ignore[attr-defined,unused-ignore]  # 既定の動詞(開く)だけ


class JotDropModule:
    def __init__(self, ctx: Any, *, api: Win32Api | None = None, threaded: bool = True,
                 clock: Callable[[], datetime] = datetime.now, mono: Callable[[], float] = time.monotonic,
                 documents: Callable[[], str] | None = None, startfile: Callable[[str], None] | None = None,
                 make_popup: bool = True, retry_sleep: Callable[[float], bool] | None = None) -> None:
        self.ctx = ctx
        self.log: logging.Logger = ctx.log
        self.accent = info("jotdrop").accent
        section = dict(ctx.settings_dict())
        section.pop("enabled", None)
        self.cfg, changed = normalize(section)
        if changed:
            try:
                ctx.write_settings(self.cfg)
            except Exception as e:  # noqa: BLE001 - 書き戻せなくても既定値で動く
                self.log.warning("既定値の書き戻しに失敗: %s", type(e).__name__)
        if api is None:
            from deskkit.modules.jotdrop._win32 import RealWin32

            api = RealWin32()
        self.api: Win32Api = api
        self._threaded = threaded
        self._clock = clock
        self._mono = mono
        self._documents_fn = documents or _default_documents
        self._documents: str | None = None
        self._startfile = startfile or _default_startfile
        self._make_popup = make_popup
        self.notifier = Notifier()
        self.pending = PendingStore(ctx.data_dir / "pending.jsonl")
        self.ops = OpLog(ctx.data_dir / "ops.jsonl", lambda: self._clock().astimezone())
        self.worker = writer.Worker(self.api, self.framing, self.log, threaded=threaded, sleep=retry_sleep)
        self.popup: MemoPopup | None = None
        self.done_mark: DoneMark | None = None
        self.draft = ""
        self.last: LastMemo | None = None
        self._undone_ids: set[str] = set()       # 取り消したメモ。10 秒後の照合で「見つからない」にしない(ワーカーのスレッドで足す)
        self.hotkey_ok: bool | None = None       # None = キー未設定
        self.hotkey_error: str | None = None
        self.blocked_count = 0
        self._blocked_notified = False
        self.last_result = "none"
        self._prev_hwnd = 0
        self._confirm_armed = False
        self._stopping = False

    # ================================================================ ライフサイクル
    def start(self) -> None:
        self._stopping = False
        try:
            self.ops.prune()                       # FR-20
        except OSError as e:
            self.log.warning("ops prune failed: %s", type(e).__name__)
        self.worker.start()
        self._register_hotkey()
        self.ctx.add_tray_action("メモを書く", lambda: self.open_popup(None))
        self.ctx.add_tray_action("直前のメモを取り消す", self.undo_last)
        self.ctx.add_tray_action("書き込み先を開く", self.open_target)
        if self._make_popup:
            # J-2: 入力欄は前もって作って隠しておく。ただし初めて作るときは Qt の字形の準備などで約 1 秒かかるので、
            # DeskKit の起動(NFR4-2)を遅らせないよう start() の少しあとに作る。その前にキーが押されたらその場で作る
            self.ctx.start_timer(POPUP_PREBUILD_MS, self._prebuild_popup, single_shot=True)
        self.ctx.start_timer(RETRY_INTERVAL_MS, lambda: self.retry_pending("timer"))
        self.retry_pending("start")
        self._update_status()
        self.log.info("jotdrop started hotkey_set=%s registered=%s pending=%d", bool(self.cfg["hotkey"]),
                      self.hotkey_ok, self.pending.count())

    def stop(self) -> None:
        self._stopping = True
        left = self.worker.stop(2.0)      # §10: 2 秒まで待ち、書けていない分を預ける
        for t in left:
            if t.job is not None:
                self._store_pending(t.job, target.REASON_BUSY)
        for w in (self.popup, self.done_mark):
            if w is not None:
                try:
                    w.hide()
                    w.deleteLater()
                except RuntimeError:
                    pass
        self.popup = None
        self.done_mark = None
        self.log.info("jotdrop stopped left=%d", len(left))

    def handle_cli(self, args: list[str]) -> tuple[int, str]:
        return 2, "unsupported"   # CLI からの追記はしない(§3)

    def create_page(self) -> QWidget:
        from deskkit.modules.jotdrop.page import JotDropPage

        return JotDropPage(self)

    def _prebuild_popup(self) -> None:
        if self.popup is None and not self._stopping:
            self._build_popup()

    def _build_popup(self) -> None:
        from deskkit.modules.jotdrop.popup import DoneMark, MemoPopup

        t0 = time.perf_counter()
        p = MemoPopup(self.accent, foreground=self.api.foreground_window)
        p.winId()               # 窓のハンドルを先に作っておく(最初に出すときを速くする)
        p.ensurePolished()
        p.submitted.connect(self.ctx.safe(self.on_submit, "popup:submit"))
        p.closed.connect(self.ctx.safe(self.close_popup, "popup:closed"))
        p.undo_requested.connect(self.ctx.safe(self.undo_last, "popup:undo"))
        p.pending_clicked.connect(self.ctx.safe(self._open_page_from_popup, "popup:pending"))
        p.edit.textChanged.connect(self.ctx.safe(self._on_text_changed, "popup:text"))
        self.popup = p
        self.done_mark = DoneMark(self.accent)
        self.log.info("popup built ms=%d", int((time.perf_counter() - t0) * 1000))

    def _post(self, fn: Callable[[], None]) -> None:
        if self._threaded:
            self.ctx.call_soon(fn)
        else:
            fn()

    # ================================================================ 設定
    def framing(self) -> writer.Framing:
        c = self.cfg
        return writer.Framing(str(c["newline"]), bool(c["blank_line_before"]), str(c["separator"]),
                              bool(c["create_file"]), str(c["new_file_header"]))

    def now(self) -> datetime:
        return self._clock()

    def documents(self) -> str:
        if self._documents is None:
            self._documents = self._documents_fn()
        return self._documents

    def folder(self) -> str:
        return target.folder_of(str(self.cfg["folder"]), self.documents())

    def target_path(self, when: datetime) -> str:
        return target.resolve(self.folder(), str(self.cfg["file_pattern"]), when)

    def save_settings(self, changes: dict[str, Any], *, restart: bool = False) -> str | None:
        """自分のセクションに書く。失敗したら理由の文。folder・file_pattern を変えたら target_confirmed を戻す(J-17)。"""
        merged = {**self.cfg, **changes}
        if ("folder" in changes and changes["folder"] != self.cfg["folder"]) or (
                "file_pattern" in changes and changes["file_pattern"] != self.cfg["file_pattern"]):
            merged["target_confirmed"] = False
        try:
            self.ctx.write_settings(merged, restart=restart)
        except Exception as e:  # noqa: BLE001 - 設定ファイルが壊れているときなど
            return f"設定を保存できませんでした({type(e).__name__})"
        self.cfg = normalize(merged)[0]
        self._confirm_armed = False
        self.notifier.changed.emit()
        return None

    # ================================================================ ホットキー(FR-1・FR-2)
    def _register_hotkey(self) -> None:
        hk = str(self.cfg["hotkey"])
        self.hotkey_ok = None
        self.hotkey_error = None
        if not hk:
            return
        problem = hotkey_problem(hk)
        if problem is not None:
            self.hotkey_ok = False
            self.hotkey_error = problem
            self.log.warning("hotkey not registered: reserved")
            return
        self.ctx.hotkeys.triggered(HOTKEY_NAME).connect(self.on_hotkey)
        ok = bool(self.ctx.hotkeys.register_text(HOTKEY_NAME, hk))
        self.hotkey_ok = ok
        if not ok:
            self.hotkey_error = "このキーはほかのアプリが使っています。別のキーを選んでください"
            self.log.warning("hotkey not registered: conflict")

    def on_hotkey(self) -> None:
        if self.popup is not None and self.popup.isVisible():
            self.close_popup("hotkey")      # FR-2: 出ているときに押されたら閉じる(書きかけは残す)
            return
        fg = self.ctx.foreground()
        if fg.is_game or fg.is_fullscreen is not False:
            self._blocked()                 # J-5・INV-5: 出さず、前面の窓に何もしない
            return
        self.retry_pending("hotkey")
        self.open_popup(fg.hwnd)

    def _blocked(self) -> None:
        self.blocked_count += 1
        self.ops.write("blocked")
        self.log.info("input blocked (game or fullscreen)")
        if not self._blocked_notified:
            self._blocked_notified = True
            self.ctx.notify("JotDrop", "ゲーム中・全画面のあいだはメモ欄を出しません", level="info")

    # ================================================================ 入力欄
    def _own_window(self, hwnd: int) -> bool:
        try:
            return self.api.window_pid(hwnd) == os.getpid()
        except OSError:
            return True

    def open_popup(self, remember: int | None) -> None:
        """入力欄を出す。remember は戻す先の窓(トレイ・クイックアクションから開いたときは None。J-4)。"""
        if self.popup is None:
            if not self._make_popup:
                return
            self._build_popup()
        assert self.popup is not None
        hwnd = int(remember or 0)
        self._prev_hwnd = hwnd if hwnd and not self._own_window(hwnd) else 0
        rect = self.api.window_rect(hwnd) if hwnd else None
        self._confirm_armed = False
        self._refresh_popup_info()
        self.popup.open_at(rect, self.draft)

    def _refresh_popup_info(self) -> None:
        if self.popup is None:
            return
        path = self.target_path(self._clock())
        self.popup.set_info(os.path.basename(path), path, self.undo_label(), self.pending.count())

    def _on_text_changed(self, text: str) -> None:
        """FR-7: max_chars を超えたら切って知らせる(貼り付けを含む)。"""
        n = int(self.cfg["max_chars"])
        if self.popup is not None and len(text) > n:
            self.popup.edit.blockSignals(True)
            self.popup.edit.setText(text[:n])
            self.popup.edit.blockSignals(False)
            self.popup.show_message(f"{n} 文字までです", "warn")

    def close_popup(self, reason: str) -> None:
        """reason: submit / esc / hotkey / outside。outside と自分の窓・無い窓には戻さない(J-4)。"""
        p = self.popup
        if p is None:
            return
        if reason != "submit":
            self.draft = p.edit.text()        # FR-6: 書きかけは残す(メモリだけ)
        self._confirm_armed = False
        p.hide_now()
        prev, self._prev_hwnd = self._prev_hwnd, 0
        if reason == "outside" or not prev:
            return
        if not self.api.is_window(prev) or self._own_window(prev):
            return
        if not self.api.set_foreground(prev):
            self.log.info("restore foreground refused")   # 理由コードだけ

    def on_submit(self, raw: str) -> str:
        """Enter(J-6 を通ったもの)。戻り値: sent / empty / confirm。"""
        n = int(self.cfg["max_chars"])
        text = compose.clean_text(raw, n)
        if not text:
            if self.popup is not None:
                self.popup.show_message("空のメモは書きません", "warn")
            return "empty"
        when = self._clock()
        path = self.target_path(when)
        if not self.cfg["target_confirmed"] and not self._confirm_armed:
            self._confirm_armed = True        # J-17: 書き込み先を変えたあとの最初の1回だけ確かめる
            if self.popup is not None:
                self.popup.show_message(f"書き込み先: {path}\nもう一度 Enter で書きます", "info")
            return "confirm"
        confirming = self._confirm_armed
        line = compose.expand(str(self.cfg["line_format"]), when, text)
        job = writer.Job(uuid.uuid4().hex, when, path, line)
        self.draft = ""
        self.last = LastMemo(job.id, text, when)
        if self.popup is not None:
            self.popup.edit.clear()
        self.close_popup("submit")
        self._enqueue_write(job, confirming)
        return "sent"

    # ================================================================ 書く(ワーカー)
    def _enqueue_write(self, job: writer.Job, confirming: bool = False) -> None:
        self.worker.submit(writer.Task("call", job=job, fn=lambda: self._write_task(job, confirming)))

    def _ensure_default_folder(self) -> None:
        """既定のフォルダ(ドキュメント\\JotDrop)だけは作る(J-16)。ほかのフォルダは作らない。"""
        if str(self.cfg["folder"]).strip():
            return
        d = self.folder()
        if not self.api.is_dir(d) and self.api.is_dir(self.documents()):
            try:
                self.api.make_dir(d)
            except OSError as e:
                self.log.warning("default folder create failed: %s", type(e).__name__)

    def _write_task(self, job: writer.Job, confirming: bool) -> None:
        """ワーカーのスレッドで動く。同じファイルの預かり分(自動の理由)を先に書き、順番を崩さない(J-9)。"""
        self._ensure_default_folder()
        key = os.path.normcase(job.path)
        blocked_reason: str | None = None
        for it in self.pending.items():
            if os.path.normcase(it.path) != key or it.reason not in target.AUTO_RETRY:
                continue
            r = self._write_pending_item(it)
            if not r.ok:
                blocked_reason = r.reason or target.REASON_BUSY
                break
        if blocked_reason is not None:
            res = writer.WriteResult(False, blocked_reason)
        else:
            res = writer.append_line(self.api, job, self.framing(), sleep=self.worker.sleep, now=self._mono)
        if not res.ok:
            self._store_pending(job, res.reason or target.REASON_OPEN_FAILED)
            self.ops.write("pending", res.reason, res.retries, res.ms)
        else:
            self.ops.write("sent", None, res.retries, res.ms)
        self.log.info("write result ok=%s reason=%s retries=%d ms=%d", res.ok, res.reason or "-", res.retries, res.ms)
        self._post(lambda: self._on_written(job, res, confirming))

    def _write_pending_item(self, it: PendingItem) -> writer.WriteResult:
        """預かり分を1件書く(ワーカーのスレッド)。書けたら預かりから消す。"""
        when = it.created_dt() or self._clock()
        job = writer.Job(it.id, when, it.path, it.line)
        res = writer.append_line(self.api, job, self.framing(), sleep=self.worker.sleep, now=self._mono)
        if res.ok:
            self.pending.remove(it.id)
            self.ops.write("retry_ok", None, res.retries, res.ms)
            self._post(lambda: self._on_retry_ok(it, res))
        else:
            reason = res.reason or target.REASON_OPEN_FAILED
            if reason != it.reason:
                self.pending.set_reason(it.id, reason)
        return res

    def _store_pending(self, job: writer.Job, reason: str) -> None:
        self.pending.add(PendingItem(job.id, job.created.isoformat(timespec="seconds"), job.path, job.line, reason))

    def _on_written(self, job: writer.Job, res: writer.WriteResult, confirming: bool) -> None:
        last = self.last if self.last is not None and self.last.job_id == job.id else None
        if res.ok:
            self.last_result = "sent"
            if last is not None:
                last.record = res.record
                last.written = True
            if confirming and not self.cfg["target_confirmed"]:
                self.save_settings({"target_confirmed": True})   # FR-14: 確かめて書けたら
            self._flash_done()
            self.ctx.start_timer(VERIFY_DELAY_MS, lambda: self._verify(job), single_shot=True)
        else:
            reason = res.reason or target.REASON_OPEN_FAILED
            self.last_result = reason
            if last is not None:
                last.pending = True
            auto = reason in target.AUTO_RETRY
            tail = "あとで自動で書きます。" if auto else "JotDrop の画面で「もう一度書く」を押してください。"
            self.ctx.notify("JotDrop", f"メモをまだ書けていません({target.REASON_TEXT.get(reason, reason)})。{tail}",
                            on_click=self.ctx.show_page, level="warn")
        self._update_status()
        self.notifier.changed.emit()

    def _on_retry_ok(self, it: PendingItem, res: writer.WriteResult) -> None:
        self.last_result = "retry_ok"
        job = writer.Job(it.id, it.created_dt() or self._clock(), it.path, it.line)
        self.ctx.start_timer(VERIFY_DELAY_MS, lambda: self._verify(job), single_shot=True)   # J-19: 書き直しも照合する
        if self.last is not None and self.last.job_id == it.id:
            self.last.pending = False
            self.last.written = True
            self.last.record = res.record
        self._update_status()
        self.notifier.changed.emit()

    def _flash_done(self) -> None:
        if not self.cfg["show_done_mark"] or self.done_mark is None:
            return
        try:
            p = self.popup
            if p is not None:
                c = p.geometry().center()
                self.done_mark.flash(QPoint(c.x(), c.y()))
        except RuntimeError:
            pass

    # ================================================================ 自動の書き直し(FR-12)・手での書き直し(FR-13)
    def retry_pending(self, why: str) -> bool:
        """使用中・開けないで預かったメモを書き直す。一時停止中は自動では書かない。"""
        if self._stopping or self.ctx.is_snoozed():
            return False
        if not any(it.reason in target.AUTO_RETRY for it in self.pending.items()):
            return False
        self.worker.submit(writer.Task("call", fn=self._retry_task))
        return True

    def _retry_task(self) -> None:
        stuck: set[str] = set()          # 書けなかったファイル(あとのメモを先に書かない)
        for it in self.pending.items():
            key = os.path.normcase(it.path)
            if it.reason not in target.AUTO_RETRY or key in stuck:
                continue
            if not self._write_pending_item(it).ok:
                stuck.add(key)
        self._post(self.notifier.changed.emit)

    def rewrite(self, item_id: str) -> None:
        """画面の「もう一度書く」。理由を問わず1件だけ書き直す。"""
        def task() -> None:
            it = self.pending.get(item_id)
            if it is None:
                return
            res = self._write_pending_item(it)
            self._post(lambda: self._after_rewrite(res))

        self.worker.submit(writer.Task("call", fn=task))

    def _after_rewrite(self, res: writer.WriteResult) -> None:
        if not res.ok:
            self.ctx.notify("JotDrop", f"まだ書けません({target.REASON_TEXT.get(res.reason or '', '')})", level="warn")
        self._update_status()
        self.notifier.changed.emit()

    def discard(self, item_id: str) -> None:
        self.pending.remove(item_id)
        if self.last is not None and self.last.job_id == item_id:
            self.last = None
        self._update_status()
        self.notifier.changed.emit()

    # ================================================================ 照合(J-19)
    def _verify(self, job: writer.Job) -> None:
        def task() -> None:
            if job.id in self._undone_ids:
                return                                   # 照合の前に取り消した
            res = writer.verify(self.api, job.path, job.line)
            self.log.info("verify result=%s", res)
            if res == writer.VERIFY_MISSING:
                self._store_pending(job, target.REASON_MISSING)
                self.ops.write("missing", target.REASON_MISSING)
                self._post(self._on_missing)

        self.worker.submit(writer.Task("call", fn=task))

    def _on_missing(self) -> None:
        self.last_result = target.REASON_MISSING
        self.ctx.notify("JotDrop", "書いたメモがファイルに見つかりません。JotDrop の画面の「書けていないメモ」で確かめてください。",
                        on_click=self.ctx.show_page, level="warn")
        self._update_status()
        self.notifier.changed.emit()

    # ================================================================ 取り消し(J-18・FR-15)
    def undo_label(self) -> str | None:
        """取り消せるメモがあれば「14:05」のような時刻。"""
        last = self.last
        if last is None:
            return None
        if last.pending and self.pending.get(last.job_id) is not None:
            return last.when.strftime("%H:%M")
        rec = last.record
        if rec is not None and self._mono() - rec.written_at <= int(self.cfg["undo_minutes"]) * 60:
            return last.when.strftime("%H:%M")
        return None

    def undo_last(self) -> None:
        last = self.last
        if last is None or self.undo_label() is None:
            self._say("取り消せるメモはありません", "info")
            return
        if last.pending and self.pending.get(last.job_id) is not None:
            def pending_task() -> None:
                # まだ書いていない: 預かりから外す。自動の書き直しと同じワーカーの列で行い、先に書かれていたら取り消さない
                removed = self.pending.remove(last.job_id) is not None
                if removed:
                    self._undone_ids.add(last.job_id)
                    self.ops.write("undo", None)
                self._post(lambda: self._on_pending_undo(last, removed))

            self.worker.submit(writer.Task("call", fn=pending_task))
            return
        rec = last.record
        if rec is None:
            self._say("取り消せるメモはありません", "info")
            return

        def task() -> None:
            res = writer.undo(self.api, rec)
            if res == writer.UNDO_OK:
                self._undone_ids.add(last.job_id)
            self.ops.write("undo" if res == writer.UNDO_OK else "undo_refused", None if res == writer.UNDO_OK else res)
            self.log.info("undo result=%s", res)
            self._post(lambda: self._on_undo(last, res))

        self.worker.submit(writer.Task("call", fn=task))

    def _on_pending_undo(self, last: LastMemo, removed: bool) -> None:
        if removed:
            self._undone(last)
        else:
            self._say("取り消す前に書き込みが済みました。取り消すにはもう一度押してください", "info")

    def _on_undo(self, last: LastMemo, res: str) -> None:
        if res == writer.UNDO_OK:
            self._undone(last)
        elif res == writer.UNDO_BUSY:
            self._say("今は取り消せません。少し待ってからもう一度", "warn")
        else:
            self._say("そのあとファイルが変わったので取り消せません", "warn")

    def _undone(self, last: LastMemo) -> None:
        """取り消した本文を入力欄に戻す(Enter でもう一度書ける)。"""
        self.last = None
        self.draft = last.text
        if self.popup is not None and self.popup.isVisible():
            self.popup.edit.setText(last.text)
            self._refresh_popup_info()
        else:
            self.open_popup(None)
        if self.popup is not None:
            self.popup.show_message("取り消しました。Enter でもう一度書けます", "ok")
        self._update_status()
        self.notifier.changed.emit()

    def _say(self, text: str, level: str) -> None:
        if self.popup is not None and self.popup.isVisible():
            self.popup.show_message(text, level)
        else:
            self.ctx.notify("JotDrop", text, level=level)

    # ================================================================ 開く
    def open_target(self) -> bool:
        """FR-18: 今日のファイルを既定のアプリで開く(無ければフォルダを開く)。"""
        path = self.target_path(self._clock())
        folder = os.path.dirname(path)
        pick = path if os.path.isfile(path) else folder if os.path.isdir(folder) else None
        if pick is None:
            self._say("書き込み先のフォルダがまだありません", "info")
            return False
        try:
            self._startfile(pick)
            return True
        except OSError as e:
            self.log.warning("open target failed: %s", type(e).__name__)
            return False

    def _open_page_from_popup(self) -> None:
        self.close_popup("esc")
        self.ctx.show_page()

    # ================================================================ 状態・利用状況・診断
    def today_count(self) -> int:
        today = self._clock().date().isoformat()
        return sum(1 for r in self.ops.rows()
                   if r.get("event") in ("sent", "retry_ok") and str(r.get("ts", "")).startswith(today))

    def _update_status(self) -> None:
        parts = []
        if self.last is not None and self.last.written:
            parts.append(f"最後のメモ {self.last.when.strftime('%H:%M')}")   # FR-16
        n = self.pending.count()
        if n:
            parts.append(f"預かり中 {n} 件")
        if not parts:
            parts.append("キー未設定" if not self.cfg["hotkey"] else "待機中" if self.hotkey_ok else "キーが使えません")
        try:
            self.ctx.set_tray_status("・".join(parts))
        except Exception as e:  # noqa: BLE001 - 状態表示の失敗で書き込みを止めない
            self.log.warning("status update failed: %s", type(e).__name__)

    def usage(self, days: int) -> list[UsageSeries]:
        sent = count_jsonl(self.ops.path, days, lambda r: r.get("event") in ("sent", "retry_ok"))
        undo = count_jsonl(self.ops.path, days, lambda r: r.get("event") == "undo")
        return [
            UsageSeries("sent", "書いたメモ", sent, "件", primary=True,
                        hint="v0.4.0 の公開から 90 日で 0 件なら README の紹介から外す(R-2)"),
            UsageSeries("undo", "取り消したメモ", undo, "件", good_when="neutral"),
        ]

    def diagnostics(self) -> dict[str, str | int | bool]:
        """FR-22。本文・パス・ファイル名は入れない(件数・真偽・理由コードだけ)。"""
        known = (*target.REASONS, "sent", "retry_ok", "none")
        return {
            "hotkey_set": bool(self.cfg["hotkey"]),
            "hotkey_registered": bool(self.hotkey_ok),
            "target_default": not str(self.cfg["folder"]).strip(),
            "pattern_has_date": "{date" in str(self.cfg["file_pattern"]),
            "format": format_kind(self.cfg),
            "target_confirmed": bool(self.cfg["target_confirmed"]),
            "pending": self.pending.count(),
            "last_result": self.last_result if self.last_result in known else "other",
            "blocked": self.blocked_count,
        }
