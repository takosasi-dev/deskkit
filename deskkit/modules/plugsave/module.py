# PlugSave モジュール本体。設定(§9)・ドライブの抜き差しの合図(FR-7・FR-8)・待ち時間と取り消し(FR-9・B-11・B-12)・
# 実行のスレッド(copier)・結果と通知(FR-23)・思い出させる通知(FR-25)・usage/diagnostics を束ねる。
# ハンドラの中では構造体を読むだけ(INV-6)。ログ・ops・通知・状態の行には件数・コードだけ(INV-4・B-14)。
from __future__ import annotations

import datetime as _dt
import functools
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Signal

from deskkit.modules.plugsave import device, drives, reminder
from deskkit.modules.plugsave.copier import (
    CANCELLED,
    ERROR,
    NO_SPACE,
    OK,
    REMOVED,
    STAGE_COPY,
    BackupOutcome,
    BackupRequest,
    BackupRun,
    CopyIO,
)
from deskkit.modules.plugsave.oplog import OpsLog
from deskkit.modules.plugsave.planner import MAX_PLAN_FILES, QUIET_REASONS, Source, excluded_dirs, inside, norm

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

    from deskkit.modules.plugsave._win32 import DriveApi, VolumeInfo
    from deskkit.usage import UsageSeries

DEFAULTS: dict[str, Any] = {"sources": [], "drives": [], "auto_start": True, "start_delay_s": 10, "remind_days": 7,
                            "last_reminded_at": None}
MAX_SOURCES = 10
MAX_DRIVES = 3
SIGNAL_GAP_S = 30.0              # FR-8: 前の合図から 30 秒以内は無視
GAME_RECHECK_MS = 30_000         # B-11
STOP_WAIT_S = 5.0                # FR-20
ETA_AFTER_S = 10.0               # FR-19: 残り時間はコピーを始めて 10 秒後から
RESERVED_NAMES = ("_以前の版", "_作業中")
KNOWN_NAMES = {"documents": "ドキュメント", "pictures": "ピクチャ", "desktop": "デスクトップ"}

# 状態
IDLE = "idle"
WAITING = "waiting"      # 待ち時間・ゲームが終わるのを待っている
PLANNING = "planning"    # 初回の計画を作っている
RUNNING = "running"

# 通知の文面(B-14: ラベル・文字・名前を入れない)
N_START_TITLE = "バックアップを始めます"
N_FIRST_TITLE = "初めてのバックアップは画面から始めてください"
N_FIRST_TEXT = "押すと画面を開きます。"
N_MISMATCH_TITLE = "別のドライブです"
N_MISMATCH_TEXT = "登録したドライブと同じ印を持つ、別のドライブです。画面で確かめてください。"
N_DONE_TITLE = "バックアップが終わりました"
N_REMOVED_TITLE = "ドライブが外れました"
N_REMOVED_TEXT = "バックアップの途中でドライブが外れました。次に挿したときに続きをコピーします。"
N_NOSPACE_TITLE = "空きが足りません"
N_NOSPACE_TEXT = "バックアップ先の空きが足りません。画面で確かめてください。"
N_ERROR_TITLE = "バックアップできませんでした"
N_ERROR_TEXT = "画面で確かめてください。"
N_REMIND_TEXT = "バックアップ用のドライブを挿してください。"
N_NOT_CONNECTED = "登録したドライブがつながっていません"
MSG_TRUNCATED = "ファイルが多いため、残りは次の回にコピーします"
MSG_UNRESPONSIVE = "ドライブの応答がありません"
MSG_READONLY = "書き込めません。書き込み禁止になっていないか確かめてください"


def start_text(delay: int) -> str:
    return f"登録したドライブがつながりました。{delay} 秒後に始めます。やめるときは、この通知を押してください。"


@dataclass(frozen=True)
class Notice:
    kind: str   # info / ok / warn / error
    text: str


@dataclass
class Pending:
    token: int
    letter: str
    drive_id: str
    deferred: bool = False          # ゲーム・全画面が終わるのを待っている
    timer: Any = None


@dataclass
class RunState:
    token: int
    drive_id: str
    letter: str
    trigger: str                    # auto / manual / first
    plan_only: bool
    fit: bool
    fs: str
    cancel: threading.Event = field(default_factory=threading.Event)
    removed: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    stage: str = ""
    done: int = 0
    total: int = 0
    done_bytes: int = 0
    total_bytes: int = 0
    copy_started: float | None = None
    started: float = 0.0


@dataclass(frozen=True)
class SourceCheck:
    status: str       # ok / drive_root / forbidden / missing / nested / full / remote
    message: str = ""


@dataclass(frozen=True)
class Candidate:
    letter: str
    label: str
    fs: str
    total: int
    free: int
    state: str        # new(印なし)/ reuse(印あり・未登録)/ registered / broken
    drive_type: int


class _Signals(QObject):
    state = Signal()      # 状態・進捗
    drives = Signal()     # つながっている・登録
    settings = Signal()   # 設定
    result = Signal()     # 前回の結果・計画
    notices = Signal()


def _as_int(v: Any, lo: int, hi: int, default: int) -> tuple[int, bool]:
    if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi:
        return v, False
    return default, True


def merge_defaults(section: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """足りないキーを既定値で補い、型・範囲の合わない値は既定値に戻す。(結果, 変えたか)。"""
    out = dict(section)
    changed = False
    for k, v in DEFAULTS.items():
        if k not in out:
            out[k] = list(v) if isinstance(v, list) else v
            changed = True
    srcs = out.get("sources")
    good_src: list[dict[str, str]] = []
    if isinstance(srcs, list):
        for s in srcs:
            if isinstance(s, dict) and isinstance(s.get("path"), str) and isinstance(s.get("name"), str) and s["path"] and s["name"]:
                good_src.append({"path": s["path"], "name": s["name"]})
    good_src = good_src[:MAX_SOURCES]
    if good_src != srcs:
        out["sources"] = good_src
        changed = True
    drvs = out.get("drives")
    good_drv: list[dict[str, Any]] = []
    if isinstance(drvs, list):
        for d in drvs:
            if not isinstance(d, dict) or not isinstance(d.get("id"), str) or not drives.ID_RE.match(d["id"]):
                continue
            serial = d.get("serial")
            if not isinstance(serial, int) or isinstance(serial, bool):
                continue
            good_drv.append({
                "id": d["id"], "serial": serial, "label": d.get("label") if isinstance(d.get("label"), str) else "",
                "registered_at": d.get("registered_at") if isinstance(d.get("registered_at"), str) else None,
                "last_success_at": d.get("last_success_at") if isinstance(d.get("last_success_at"), str) else None,
                "first_done": d.get("first_done") if isinstance(d.get("first_done"), bool) else False,
            })
    good_drv = good_drv[:MAX_DRIVES]
    if good_drv != drvs:
        out["drives"] = good_drv
        changed = True
    if not isinstance(out.get("auto_start"), bool):
        out["auto_start"] = True
        changed = True
    out["start_delay_s"], c1 = _as_int(out.get("start_delay_s"), 3, 60, 10)
    out["remind_days"], c2 = _as_int(out.get("remind_days"), 0, 90, 7)
    changed = changed or c1 or c2
    lr = out.get("last_reminded_at")
    if lr is not None and not isinstance(lr, str):
        out["last_reminded_at"] = None
        changed = True
    return out, changed


def human_bytes(n: int) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{int(v)} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} TB"  # pragma: no cover


def _is_drive_root(path: str) -> bool:
    p = drives.display_path(os.path.abspath(path))
    return os.path.dirname(p) == p


def _open_default(path: str) -> None:
    os.startfile(path)  # type: ignore[attr-defined,unused-ignore]  # Windows 専用。エクスプローラーで開くだけ


class PlugSaveModule:
    def __init__(self, ctx: Any, *, api: DriveApi | None = None, io: CopyIO | None = None, env: dict[str, str] | None = None,
                 threaded: bool = True, now: Callable[[], _dt.datetime] | None = None,
                 mono: Callable[[], float] | None = None, sleep: Callable[[float], None] | None = None,
                 opener: Callable[[str], None] | None = None, max_plan_files: int = MAX_PLAN_FILES,
                 probe_wait_s: float | None = None) -> None:
        self.ctx = ctx
        self.log: logging.Logger = ctx.log
        section = dict(ctx.settings_dict())
        section.pop("enabled", None)
        merged, changed = merge_defaults(section)
        self.config: dict[str, Any] = merged
        if changed:
            try:
                ctx.write_settings(merged)
            except Exception as e:  # noqa: BLE001 - 書き戻せなくても既定値で動く
                self.log.warning("既定値の書き戻しに失敗: %s", type(e).__name__)
        if api is None:
            from deskkit.modules.plugsave._win32 import RealDriveApi

            api = RealDriveApi()
        self.api: DriveApi = api
        self.io = io
        self.env = env
        self.threaded = threaded
        self._now = now or (lambda: _dt.datetime.now().astimezone())
        self._mono = mono or time.monotonic
        self._sleep = sleep or time.sleep
        self._opener = opener or _open_default
        self.max_plan_files = max_plan_files
        self.probe_wait_s = drives.PROBE_RETRY_WAIT_S if probe_wait_s is None else probe_wait_s
        self.data_dir = Path(ctx.data_dir)
        self.ops = OpsLog(self.data_dir / "ops.jsonl", now=self._now)
        self.signals = _Signals()
        self.state = IDLE
        self.connected: dict[str, drives.DriveProbe] = {}     # 登録 ID → つながっているドライブ(シリアルも合う)
        self.mismatched: dict[str, drives.DriveProbe] = {}    # 登録 ID → 印は合うがシリアルが違うドライブ
        self.pending: Pending | None = None
        self.queue: list[str] = []
        self.run: RunState | None = None
        self._stuck: threading.Thread | None = None
        self.last: BackupOutcome | None = None
        self.last_meta: dict[str, Any] = {}
        self.previews: dict[str, BackupOutcome] = {}
        self.notices: list[Notice] = []
        self._last_signal: dict[str, float] = {}
        self._probing: set[str] = set()
        self._token = 0
        self._alive = False
        self._tray_cancel: Any = None
        self.last_result_code = "none"

    # ================================================================ ライフサイクル
    def start(self) -> None:
        self._alive = True
        self.ctx.on_native(device.WM_DEVICECHANGE, self._on_native)          # FR-7
        self.ctx.add_tray_action("今すぐバックアップ", functools.partial(self.backup_now, from_tray=True))
        self._tray_cancel = self.ctx.add_tray_action("やめる", self.cancel_any)
        self._update_tray_items()
        self.ctx.start_timer(reminder.FIRST_CHECK_S * 1000, self.check_reminder, single_shot=True)   # FR-25
        self.ctx.start_timer(reminder.CHECK_INTERVAL_S * 1000, self.check_reminder)
        self.refresh_drives()   # B-15: つながっていても自動では始めない(状態を読むだけ)
        self._set_status()
        self.log.info("plugsave started sources=%d drives=%d auto_start=%s", len(self.config["sources"]),
                      len(self.config["drives"]), self.config["auto_start"])

    def stop(self) -> None:
        self._alive = False
        self._drop_pending()
        self.queue.clear()
        r = self.run
        if r is not None:
            r.cancel.set()
            th = r.thread
            if th is not None and th.is_alive():
                th.join(STOP_WAIT_S)      # FR-20: 最大 5 秒。超えたらデーモンのスレッドを待たずに止まった扱い
        self.run = None
        self._token += 1
        self.state = IDLE
        self.log.info("plugsave stopped")

    def handle_cli(self, args: list[str]) -> tuple[int, str]:
        return 2, "unsupported"      # FR-29

    def create_page(self) -> QWidget:
        from deskkit.modules.plugsave.page import PlugSavePage

        self.refresh_drives()
        return PlugSavePage(self)

    # ================================================================ 小物
    def _spawn(self, fn: Callable[[], None], name: str) -> threading.Thread | None:
        if not self.threaded:
            fn()
            return None
        t = threading.Thread(target=fn, name=name, daemon=True)
        t.start()
        return t

    def _post(self, fn: Callable[[], None]) -> None:
        """別スレッド → 画面のスレッド。例外はログに型名だけ(INV-4)。"""

        def run() -> None:
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self.log.error("callback failed: %s", type(e).__name__)

        self.ctx.call_soon(run)

    def _save(self) -> str | None:
        try:
            self.ctx.write_settings(dict(self.config))
        except Exception as e:  # noqa: BLE001 - 保存できなかったことを画面に返す
            self.log.warning("settings write failed: %s", type(e).__name__)
            return f"設定を保存できませんでした({type(e).__name__})"
        self.signals.settings.emit()
        return None

    def _notify(self, title: str, text: str, on_click: Callable[[], None] | None = None, level: str = "info") -> None:
        try:
            self.ctx.notify(title, text, on_click, level=level)
        except Exception as e:  # noqa: BLE001 - 通知が出せなくても続ける
            self.log.warning("notify failed: %s", type(e).__name__)

    def _set_notices(self, items: list[Notice]) -> None:
        self.notices = items
        self.signals.notices.emit()

    def _add_notice(self, kind: str, text: str) -> None:
        self.notices = [*self.notices, Notice(kind, text)]
        self.signals.notices.emit()

    def _in_game(self) -> bool:
        try:
            fg = self.ctx.foreground()
        except Exception:  # noqa: BLE001 - 読めなければ始めてよい扱い
            return False
        return bool(fg.is_game) or fg.is_fullscreen is True

    def _snoozed(self) -> bool:
        try:
            return bool(self.ctx.is_snoozed())
        except Exception:  # noqa: BLE001
            return False

    def drive_cfg(self, drive_id: str) -> dict[str, Any] | None:
        for d in self.config["drives"]:
            if d["id"] == drive_id:
                return d  # type: ignore[no-any-return]
        return None

    def slot_of(self, drive_id: str) -> int:
        for i, d in enumerate(self.config["drives"]):
            if d["id"] == drive_id:
                return i + 1
        return 1

    def sources(self) -> list[Source]:
        return [Source(s["path"], s["name"]) for s in self.config["sources"]]

    def excluded(self) -> list[str]:
        """FR-2 のフォルダと DeskKit 自身のデータフォルダ(§10: ドライブの直下を足しても中に入らない)。"""
        return excluded_dirs(self.env, [str(self.data_dir.parent), str(self.data_dir)])

    def pc(self) -> str:
        return drives.pc_name(self.env)

    # ================================================================ 抜き差しの合図(FR-7・FR-8)
    def _on_native(self, wparam: int, lparam: int) -> None:
        """隠しウィンドウの WM_DEVICECHANGE。構造体を読んで文字の集合を作り、call_soon で渡して戻るだけ(INV-6)。"""
        ev = device.parse(wparam, lparam)
        if ev is None:
            return
        self.ctx.call_soon(functools.partial(self.on_device_event, ev.arrived, set(ev.letters)))

    def on_device_event(self, arrived: bool, letters: set[str]) -> None:
        if not self._alive:
            return
        if not arrived:
            self._on_removed(letters)
            return
        now = self._mono()
        for letter in sorted(letters):
            p = self.pending
            r = self.run
            if letter in self._probing or (p is not None and p.letter == letter) or (r is not None and r.letter == letter):
                continue
            if letter in self.queue:
                continue
            last = self._last_signal.get(letter)
            if last is not None and now - last < SIGNAL_GAP_S:
                continue
            self._last_signal[letter] = now
            self._probing.add(letter)
            self._spawn(functools.partial(self._probe_worker, letter), f"plugsave-probe-{letter}")

    def _probe_worker(self, letter: str) -> None:
        try:
            probe = drives.probe_letter(self.api, letter, wait_s=self.probe_wait_s, sleep=self._sleep)
        except Exception as e:  # noqa: BLE001 - 読めなければ何もしない
            self.log.warning("probe failed: %s", type(e).__name__)
            probe = None
        self._post(lambda: self._on_probed(letter, probe))

    def _on_probed(self, letter: str, probe: drives.DriveProbe | None) -> None:
        self._probing.discard(letter)
        if not self._alive:
            return
        self._forget_letter(letter)
        if probe is None or probe.marker.status != "ok" or probe.volume is None:
            self.signals.drives.emit()
            return
        cfg = self.drive_cfg(probe.marker.id or "")
        if cfg is None:
            self.signals.drives.emit()
            return   # 登録していないドライブ: 何もしない(AC-23)
        did = cfg["id"]
        match = probe.volume.serial == cfg["serial"]
        if match:
            self.connected[did] = probe
        else:
            self.mismatched[did] = probe
        self.signals.drives.emit()
        self._set_status()
        self.log.info("registered drive arrived slot=%d serial_match=%s first_done=%s", self.slot_of(did), match,
                      cfg["first_done"])
        self._auto_flow(letter, cfg, match)

    def _forget_letter(self, letter: str) -> None:
        for m in (self.connected, self.mismatched):
            for k in [k for k, v in m.items() if v.letter == letter]:
                m.pop(k, None)

    def _on_removed(self, letters: set[str]) -> None:
        for letter in letters:
            self._last_signal.pop(letter, None)
            if letter in self.queue:
                self.queue.remove(letter)
            p = self.pending
            if p is not None and p.letter == letter:
                self._drop_pending()
                self.log.info("pending start dropped: drive removed")
            r = self.run
            if r is not None and r.letter == letter:
                r.removed.set()           # FR-21
            self._forget_letter(letter)
        self.signals.drives.emit()
        self.signals.state.emit()
        self._set_status()

    # ================================================================ 自動の流れ(FR-9・FR-10・B-10・B-11)
    def _auto_flow(self, letter: str, cfg: dict[str, Any], serial_match: bool) -> None:
        if self._snoozed():
            return     # B-11: 一時停止中は始めず、通知も出さない
        if not serial_match:
            self._notify(N_MISMATCH_TITLE, N_MISMATCH_TEXT, self._show_page, level="warn")   # §10・AC-11
            return
        if not self.config["sources"]:
            return
        if not cfg["first_done"]:
            if not self._in_game():
                self._notify(N_FIRST_TITLE, N_FIRST_TEXT, self._show_page)                 # FR-10
            return
        if not self.config["auto_start"]:
            return
        if self.pending is not None or self.run is not None:
            if letter not in self.queue:
                self.queue.append(letter)      # §10: 同時に動く回は1つ。挿された順に1台ずつ
            return
        self._begin_wait(letter, cfg["id"])

    def _begin_wait(self, letter: str, drive_id: str) -> None:
        if self._snoozed():
            return
        self._token += 1
        token = self._token
        pend = Pending(token, letter, drive_id)
        self.pending = pend
        self.state = WAITING
        if self._in_game():
            pend.deferred = True       # B-11: 30 秒ごとに確かめる
            pend.timer = self.ctx.start_timer(GAME_RECHECK_MS, functools.partial(self._recheck_game, token))
            self.log.info("auto start deferred: game or fullscreen")
        else:
            delay = int(self.config["start_delay_s"])

            def on_click() -> None:
                self.cancel_pending(token, open_page=True)

            self._notify(N_START_TITLE, start_text(delay), on_click)
            pend.timer = self.ctx.start_timer(delay * 1000, functools.partial(self._wait_done, token), single_shot=True)
        self.signals.state.emit()
        self._update_tray_items()
        self._set_status()

    def _recheck_game(self, token: int) -> None:
        p = self.pending
        if p is None or p.token != token:
            return
        if self._snoozed():
            self._drop_pending()
            self._next_in_queue()
            return
        if self._in_game():
            return
        self._stop_timer(p.timer)
        self.pending = None
        self._begin_wait(p.letter, p.drive_id)

    def _wait_done(self, token: int) -> None:
        p = self.pending
        if p is None or p.token != token:
            return
        self.pending = None
        self._stop_timer(p.timer)
        self.state = IDLE
        if self._snoozed():
            self._next_in_queue()
            return
        if self._in_game():
            self._begin_wait(p.letter, p.drive_id)
            return
        if p.drive_id not in self.connected:
            self._next_in_queue()
            return
        err = self.start_backup(p.drive_id, trigger="auto")
        if err:
            self.log.info("auto start skipped")
            self._next_in_queue()

    @staticmethod
    def _stop_timer(t: Any) -> None:
        if t is not None:
            try:
                t.stop()
            except Exception:  # noqa: BLE001 - 止まっていれば良い
                pass

    def _drop_pending(self) -> None:
        p = self.pending
        if p is not None:
            self._stop_timer(p.timer)
        self.pending = None
        if self.run is None:
            self.state = IDLE
        self.signals.state.emit()
        self._update_tray_items()
        self._set_status()

    def cancel_pending(self, token: int | None = None, *, open_page: bool = False) -> bool:
        """B-12: 通知を押す・「やめる」で、この合図の分は始めない。"""
        p = self.pending
        hit = p is not None and (token is None or p.token == token)
        if hit:
            self._drop_pending()
            self._add_notice("info", "バックアップを取りやめました。")
            self.log.info("pending start cancelled by user")
        if open_page:
            self._show_page()
        if hit:
            self._next_in_queue()
        return hit

    def _next_in_queue(self) -> None:
        while self.queue and self.pending is None and self.run is None:
            letter = self.queue.pop(0)
            probe = next((v for v in self.connected.values() if v.letter == letter), None)
            if probe is None or probe.marker.id is None:
                continue
            cfg = self.drive_cfg(probe.marker.id)
            if cfg is None or not cfg["first_done"] or not self.config["auto_start"]:
                continue
            self._begin_wait(letter, cfg["id"])

    def _show_page(self) -> None:
        try:
            self.ctx.show_page()
        except Exception as e:  # noqa: BLE001
            self.log.warning("show_page failed: %s", type(e).__name__)

    # ================================================================ 手で始める(FR-11)
    def backup_now(self, drive_id: str | None = None, *, from_tray: bool = False) -> str | None:
        """「今すぐバックアップ」。一時停止中・ゲーム中でも始める。始められないときは理由の文言を返す。"""
        if not self.config["drives"]:
            return self._manual_error("ドライブを登録してください", from_tray)
        if not self.config["sources"]:
            return self._manual_error("コピー元を足してください", from_tray)
        if self.run is not None:
            return self._manual_error("バックアップ中です", from_tray)
        if drive_id is not None:
            return self._manual_start(drive_id, from_tray)

        def then() -> None:
            ids = [d["id"] for d in self.config["drives"] if d["id"] in self.connected]
            if not ids:
                self._manual_error(N_NOT_CONNECTED, from_tray)
            elif len(ids) > 1:
                self._add_notice("info", "どのドライブにバックアップするか、下の一覧で選んでください。")
                self._show_page()
            else:
                self._manual_start(ids[0], from_tray)

        self.refresh_drives(then)
        return None

    def _manual_error(self, text: str, from_tray: bool) -> str:
        if from_tray:
            self._notify("PlugSave", text, self._show_page, level="warn")
        else:
            self._add_notice("warn", text)
        return text

    def _manual_start(self, drive_id: str, from_tray: bool) -> str | None:
        cfg = self.drive_cfg(drive_id)
        if cfg is None or drive_id not in self.connected:
            return self._manual_error(N_NOT_CONNECTED, from_tray)
        if not cfg["first_done"]:
            self._show_page()            # B-10: 初回は画面で計画を見せてから
            return self.make_preview(drive_id)
        if self.pending is not None:
            self._drop_pending()
        return self.start_backup(drive_id, trigger="manual")

    # ================================================================ 実行
    @property
    def busy(self) -> bool:
        return self.run is not None

    def make_preview(self, drive_id: str) -> str | None:
        """FR-10: 初回の計画(件数・合計の大きさ)を作る。コピーはしない。"""
        return self.start_backup(drive_id, trigger="first", plan_only=True)

    def start_backup(self, drive_id: str, *, trigger: str, fit: bool = False, plan_only: bool = False) -> str | None:
        if self.run is not None:
            return "バックアップ中です"
        if self._stuck is not None and self._stuck.is_alive():
            return MSG_UNRESPONSIVE
        cfg = self.drive_cfg(drive_id)
        probe = self.connected.get(drive_id)
        if cfg is None or probe is None or probe.volume is None:
            return N_NOT_CONNECTED
        if not self.config["sources"]:
            return "コピー元を足してください"
        if trigger == "first" and not plan_only and cfg["first_done"]:
            trigger = "manual"
        self._token += 1
        token = self._token
        fs = probe.volume.fs
        rs = RunState(token, drive_id, probe.letter, trigger, plan_only, fit, fs, started=self._mono())
        req = BackupRequest(root=probe.root, fs=fs, pc=self.pc(), sources=self.sources(), excluded=self.excluded(),
                            fit=fit, plan_only=plan_only, max_files=self.max_plan_files,
                            start=self._now().replace(tzinfo=None))
        self.run = rs
        self.state = PLANNING if plan_only else RUNNING
        if not plan_only:
            self._set_notices([])
        self.signals.state.emit()
        self._update_tray_items()
        self._set_status()
        self.log.info("backup start trigger=%s slot=%d fs=%s plan_only=%s fit=%s sources=%d", trigger,
                      self.slot_of(drive_id), drives.fs_kind(fs), plan_only, fit, len(req.sources))

        def progress(stage: str, done: int, total: int, done_b: int, total_b: int) -> None:
            self._post(lambda: self._on_progress(token, stage, done, total, done_b, total_b))

        job = BackupRun(req, self.api, cancel=rs.cancel, removed=rs.removed, progress=progress, io=self.io)

        def work() -> None:
            out = job.run()
            self._post(lambda: self._on_run_done(token, out))

        rs.thread = self._spawn(work, "plugsave-run")
        return None

    def _on_progress(self, token: int, stage: str, done: int, total: int, done_b: int, total_b: int) -> None:
        r = self.run
        if r is None or r.token != token:
            return
        if stage == STAGE_COPY and r.copy_started is None:
            r.copy_started = self._mono()
        r.stage, r.done, r.total, r.done_bytes, r.total_bytes = stage, done, total, done_b, total_b
        self.signals.state.emit()
        self._set_status()

    def percent(self) -> int:
        r = self.run
        if r is None or r.stage != STAGE_COPY:
            return 0
        if r.total_bytes > 0:
            return min(100, int(r.done_bytes * 100 / r.total_bytes))
        return min(100, int(r.done * 100 / r.total)) if r.total else 0

    def eta_seconds(self) -> int | None:
        """FR-19: コピーを始めて 10 秒後から、それまでの平均の速さで出す。"""
        r = self.run
        if r is None or r.copy_started is None:
            return None
        el = self._mono() - r.copy_started
        if el < ETA_AFTER_S or r.done_bytes <= 0:
            return None
        rate = r.done_bytes / el
        return int(max(0, r.total_bytes - r.done_bytes) / rate)

    def cancel_run(self) -> None:
        """FR-20: 中止。最大 5 秒待ち、戻らなければ止まった扱い(ドライブの応答がありません)。"""
        r = self.run
        if r is None:
            return
        r.cancel.set()
        self.ctx.start_timer(int(STOP_WAIT_S * 1000), functools.partial(self._cancel_timeout, r.token), single_shot=True)
        self.signals.state.emit()

    def cancel_any(self) -> None:
        if self.pending is not None:
            self.cancel_pending()
        elif self.run is not None:
            self.cancel_run()

    def _cancel_timeout(self, token: int) -> None:
        r = self.run
        if r is None or r.token != token:
            return
        th = r.thread
        if th is not None and th.is_alive():
            self._stuck = th
            self.run = None
            self._token += 1
            self.state = IDLE
            self._add_notice("error", MSG_UNRESPONSIVE)
            self.last_result_code = CANCELLED
            if not r.plan_only:
                self._write_ops(r, BackupOutcome(result=CANCELLED))   # 件数は分からないので 0 で残す
            self.log.warning("backup thread did not stop in time")
            self.signals.state.emit()
            self._update_tray_items()
            self._set_status()
            self._next_in_queue()

    def _write_ops(self, r: RunState, out: BackupOutcome) -> None:
        try:
            self.ops.write(trigger=r.trigger, result=out.result, drive_slot=self.slot_of(r.drive_id), fs=drives.fs_kind(r.fs),
                           new=out.new, changed=out.changed, unchanged=out.unchanged, skipped=out.skipped,
                           failed=out.failed, bytes=out.bytes, ms=out.ms)
        except (TypeError, ValueError) as e:
            self.log.error("ops write refused: %s", type(e).__name__)

    def _on_run_done(self, token: int, out: BackupOutcome) -> None:
        r = self.run
        if r is None or r.token != token:
            return
        self.run = None
        self.state = IDLE
        if r.plan_only:
            self.previews[r.drive_id] = out
            self.log.info("plan ready result=%s new=%d changed=%d unchanged=%d skipped=%d ms=%d", out.result, out.plan_new,
                          out.plan_changed, out.unchanged, out.skipped_total + out.failed, out.ms)
            self.signals.result.emit()
            self.signals.state.emit()
            self._update_tray_items()
            self._set_status()
            self._next_in_queue()
            return
        self.previews.pop(r.drive_id, None)
        self.last = out
        self.last_meta = {"drive_id": r.drive_id, "trigger": r.trigger, "fit": r.fit, "letter": r.letter}
        self.last_result_code = out.result
        self._write_ops(r, out)
        self.log.info("backup done result=%s trigger=%s new=%d changed=%d moved_old=%d unchanged=%d skipped=%d failed=%d "
                      "bytes=%d ms=%d cleaned=%d exc=%s", out.result, r.trigger, out.new, out.changed, out.moved_old,
                      out.unchanged, out.skipped_total, out.failed, out.bytes, out.ms, out.cleaned_parts, out.exc_type or "-")
        cfg = self.drive_cfg(r.drive_id)
        if out.result == OK and cfg is not None:
            cfg["last_success_at"] = self._now().isoformat(timespec="seconds")   # B-13
            cfg["first_done"] = True
            self._save()
        self._result_notices(out)
        self._result_notify(out)
        self.signals.result.emit()
        self.signals.state.emit()
        self.signals.drives.emit()
        self._update_tray_items()
        self._set_status()
        self._next_in_queue()

    def _result_notices(self, out: BackupOutcome) -> None:
        n: list[Notice] = []
        if out.result == OK:
            n.append(Notice("ok", "バックアップが終わりました。"))
        elif out.result == CANCELLED:
            n.append(Notice("info", "中止しました。コピーの済んだファイルは残っています。"))
        elif out.result == REMOVED:
            n.append(Notice("warn", N_REMOVED_TEXT))
        elif out.result == NO_SPACE:
            if out.need_more:
                n.append(Notice("warn", f"バックアップ先の空きが足りません。あと {human_bytes(out.need_more)} 要ります。"))
            elif out.left_out:
                n.append(Notice("warn", f"空きが足りないため、今回コピーしなかったファイル: {out.left_out:,} 件"))
            else:
                n.append(Notice("warn", "コピーの途中で空きが足りなくなりました。コピーの済んだファイルは残っています。"))
        elif out.error == "readonly":
            n.append(Notice("error", MSG_READONLY))
        elif out.error == "no_sources":
            n.append(Notice("error", "コピー元が1つも見つかりませんでした"))
        else:
            n.append(Notice("error", "バックアップの途中で問題が起きました。もう一度お試しください。"))
        if out.truncated:
            n.append(Notice("warn", MSG_TRUNCATED))
        if out.missing_sources and out.error != "no_sources":
            n.append(Notice("warn", f"見つからなかったコピー元: {out.missing_sources} 個"))
        if out.utime_failed_count:
            n.append(Notice("info", f"日時を写せませんでした: {out.utime_failed_count:,} 件"))
        self._set_notices(n)

    def _result_notify(self, out: BackupOutcome) -> None:
        if out.result == OK:
            skipped = out.skipped_total + out.failed
            if out.copied == 0:
                text = "変わったファイルはありませんでした"
                if skipped:
                    text += f"・飛ばした {skipped:,} 件"
            else:
                text = f"コピー {out.copied:,} 件・飛ばした {skipped:,} 件"
            loud = out.failed > 0 or any(k not in QUIET_REASONS for k in out.skipped)
            self._notify(N_DONE_TITLE, text, self._show_page, level="warn" if loud else "ok")    # FR-23
        elif out.result == REMOVED:
            self._notify(N_REMOVED_TITLE, N_REMOVED_TEXT, self._show_page, level="warn")        # FR-21
        elif out.result == NO_SPACE:
            self._notify(N_NOSPACE_TITLE, N_NOSPACE_TEXT, self._show_page, level="warn")        # §10
        elif out.result == ERROR:
            self._notify(N_ERROR_TITLE, N_ERROR_TEXT, self._show_page, level="error")

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """テスト・自己検査用: 実行のスレッドの終わりを待つ(画面のスレッドへの返しは呼ぶ側が流す)。"""
        r = self.run
        th = r.thread if r is not None else None
        if th is None:
            return True
        th.join(timeout)
        return not th.is_alive()

    # ================================================================ つながっているドライブ
    def refresh_drives(self, then: Callable[[], None] | None = None) -> None:
        """つながっている登録したドライブを読み直す(別スレッド)。自動では始めない(B-15)。"""
        skip = {drives.system_letter(self.env)}

        def work() -> None:
            try:
                probes = drives.scan_letters(self.api, skip=skip)
            except Exception as e:  # noqa: BLE001
                self.log.warning("drive scan failed: %s", type(e).__name__)
                probes = []
            self._post(lambda: self._on_refreshed(probes, then))

        self._spawn(work, "plugsave-scan")

    def _on_refreshed(self, probes: list[drives.DriveProbe], then: Callable[[], None] | None) -> None:
        conn: dict[str, drives.DriveProbe] = {}
        mism: dict[str, drives.DriveProbe] = {}
        for p in probes:
            if p.marker.status != "ok" or p.marker.id is None or p.volume is None:
                continue
            cfg = self.drive_cfg(p.marker.id)
            if cfg is None:
                continue
            (conn if p.volume.serial == cfg["serial"] else mism)[cfg["id"]] = p
        r = self.run
        if r is not None and r.drive_id in self.connected:
            conn.setdefault(r.drive_id, self.connected[r.drive_id])   # 実行中のドライブは忙しくて読めないことがある
        self.connected, self.mismatched = conn, mism
        self.signals.drives.emit()
        self._set_status()
        if then is not None:
            then()

    def use_this_drive(self, drive_id: str) -> str | None:
        """§10: 印は合うがシリアルが違うドライブを、登録したドライブとして使う(シリアルを覚え直す)。"""
        p = self.mismatched.get(drive_id)
        cfg = self.drive_cfg(drive_id)
        if p is None or cfg is None or p.volume is None:
            return N_NOT_CONNECTED
        cfg["serial"] = p.volume.serial
        err = self._save()
        self.mismatched.pop(drive_id, None)
        self.connected[drive_id] = p
        self.signals.drives.emit()
        self.log.info("drive serial updated slot=%d", self.slot_of(drive_id))
        return err

    # ================================================================ 登録(FR-4〜FR-6)
    def list_candidates(self, done: Callable[[list[Candidate]], None]) -> None:
        """FR-4: 登録の候補を読む(別スレッド)。システムドライブとコピー元のあるドライブは出さない。"""
        skip = {drives.system_letter(self.env)}
        for s in self.config["sources"]:
            lt = drives.letter_of_path(s["path"])
            if lt:
                skip.add(lt)
        registered = {d["id"] for d in self.config["drives"]}

        def work() -> None:
            out: list[Candidate] = []
            try:
                for p in drives.scan_letters(self.api, skip=skip, need_volume_without_marker=True):
                    if p.volume is None:
                        continue
                    try:
                        total, free = self.api.disk_usage(p.root)
                    except OSError:
                        total, free = 0, 0
                    if p.marker.status == "broken":
                        state = "broken"
                    elif p.marker.status == "ok":
                        state = "registered" if p.marker.id in registered else "reuse"
                    else:
                        state = "new"
                    out.append(Candidate(p.letter, p.volume.label, p.volume.fs, total, free, state, p.drive_type))
            except Exception as e:  # noqa: BLE001
                self.log.warning("candidate scan failed: %s", type(e).__name__)
            self._post(lambda: done(out))

        self._spawn(work, "plugsave-candidates")

    def register_drive(self, letter: str, *, replace_broken: bool = False,
                       done: Callable[[str | None], None] | None = None) -> None:
        """FR-5: 印とフォルダを用意して登録する(別スレッド)。done(エラーの文言 or None)。"""
        if len(self.config["drives"]) >= MAX_DRIVES:
            self._finish_register(None, None, "登録できるドライブは 3 台までです", done)
            return
        root = self.api.root(letter)

        def work() -> None:
            err: str | None = None
            outcome: drives.RegisterOutcome | None = None
            vol: VolumeInfo | None = None
            try:
                old = self.api.set_thread_error_mode(1)
                try:
                    vol = self.api.volume_info(root)
                    if vol is None:
                        err = "ドライブを読めませんでした"
                    else:
                        outcome = drives.prepare_drive(root, replace_broken=replace_broken, now=self._now())
                finally:
                    if old >= 0:
                        self.api.set_thread_error_mode(old)
            except ValueError:
                err = "印を読めません"
            except OSError as e:
                self.log.warning("register failed: %s", type(e).__name__)
                err = "ドライブに書き込めませんでした。書き込み禁止になっていないか確かめてください"
            where = (letter, root, vol) if vol is not None else None
            self._post(lambda: self._finish_register(outcome, where, err, done))

        self._spawn(work, "plugsave-register")

    def _finish_register(self, outcome: drives.RegisterOutcome | None, where: tuple[str, str, VolumeInfo] | None,
                         err: str | None, done: Callable[[str | None], None] | None) -> None:
        if err is None and outcome is not None and where is not None:
            letter, root, vol = where
            if self.drive_cfg(outcome.id) is not None:
                err = "このドライブは登録済みです"
            elif len(self.config["drives"]) >= MAX_DRIVES:
                err = "登録できるドライブは 3 台までです"
            else:
                self.config["drives"] = [*self.config["drives"], {
                    "id": outcome.id, "serial": int(vol.serial), "label": vol.label,
                    "registered_at": self._now().isoformat(timespec="seconds"), "last_success_at": None, "first_done": False,
                }]
                err = self._save()
                self.connected[outcome.id] = drives.DriveProbe(letter, root, 3, drives.MarkerRead("ok", outcome.id), vol)
                self.log.info("drive registered slot=%d reused_id=%s readme=%s", len(self.config["drives"]), outcome.reused,
                              outcome.readme_written)
                self.signals.drives.emit()
                self._set_status()
        if done is not None:
            done(err)

    def unregister_drive(self, drive_id: str) -> str | None:
        """FR-6: 設定から外すだけ。ドライブには触れない。"""
        if self.run is not None and self.run.drive_id == drive_id:
            return "バックアップ中は外せません"
        self.config["drives"] = [d for d in self.config["drives"] if d["id"] != drive_id]
        self.connected.pop(drive_id, None)
        self.mismatched.pop(drive_id, None)
        self.previews.pop(drive_id, None)
        p = self.pending
        if p is not None and p.drive_id == drive_id:
            self._drop_pending()
        err = self._save()
        self.signals.drives.emit()
        self._set_status()
        self.log.info("drive unregistered remaining=%d", len(self.config["drives"]))
        return err

    # ================================================================ コピー元(FR-1〜FR-3)
    def check_source(self, path: str) -> SourceCheck:
        if not path or not os.path.isdir(path):
            return SourceCheck("missing", "フォルダが見つかりません")
        try:
            remote = drives.is_remote_path(self.api, path)
        except OSError:
            remote = True
        if remote:
            return SourceCheck("remote", "ネットワークのドライブのフォルダは選べません")
        n = norm(path)
        is_root = _is_drive_root(path)
        if is_root and drives.letter_of_path(path) == drives.system_letter(self.env):
            return SourceCheck("forbidden", "システムドライブの直下は選べません")
        if any(inside(n, x) for x in excluded_dirs(self.env)):
            return SourceCheck("forbidden", "Windows・プログラム・アプリのデータのフォルダは選べません")
        if inside(n, norm(str(self.data_dir.parent))):
            return SourceCheck("forbidden", "DeskKit のデータのフォルダは選べません")
        for p in [*self.connected.values(), *self.mismatched.values()]:
            if inside(n, norm(p.root)):
                return SourceCheck("forbidden", "バックアップ先のドライブの中は選べません")
        for s in self.config["sources"]:
            sn = norm(s["path"])
            if inside(n, sn) or inside(sn, n):
                return SourceCheck("nested", "選んだフォルダの中か、それを含むフォルダは足せません")
        if len(self.config["sources"]) >= MAX_SOURCES:
            return SourceCheck("full", "コピー元は 10 個までです")
        if is_root:
            return SourceCheck("drive_root", "ドライブの直下は、時間がかかります。続けますか")
        return SourceCheck("ok")

    def source_name_for(self, path: str, kind: str | None = None) -> str:
        """FR-3: 候補の3つは決まった名前、それ以外はフォルダ名、ドライブの直下は「<文字>ドライブ」。重なれば ` (2)` から。"""
        if kind in KNOWN_NAMES:
            base = KNOWN_NAMES[kind]
        elif _is_drive_root(path):
            base = f"{drives.letter_of_path(path) or '?'}ドライブ"
        else:
            base = drives.safe_name(os.path.basename(os.path.normpath(path)), "フォルダ")
        taken = {s["name"].casefold() for s in self.config["sources"]} | {r.casefold() for r in RESERVED_NAMES}
        name = base
        i = 2
        while name.casefold() in taken:
            name = f"{base} ({i})"
            i += 1
        return name

    def add_source(self, path: str, kind: str | None = None, *, confirmed_root: bool = False) -> str | None:
        """ドライブの直下の確認は画面が済ませてから confirmed_root=True で呼ぶ。足せなければ文言を返す。"""
        chk = self.check_source(path)
        if chk.status == "drive_root" and not confirmed_root:
            return chk.message
        if chk.status not in ("ok", "drive_root"):
            return chk.message
        name = self.source_name_for(path, kind)
        self.config["sources"] = [*self.config["sources"], {"path": os.path.normpath(path), "name": name}]
        self.log.info("source added count=%d", len(self.config["sources"]))
        return self._save()

    def remove_source(self, index: int) -> str | None:
        srcs = list(self.config["sources"])
        if not 0 <= index < len(srcs):
            return None
        srcs.pop(index)
        self.config["sources"] = srcs
        self.log.info("source removed count=%d", len(srcs))
        return self._save()

    # ================================================================ 設定
    def set_auto_start(self, v: bool) -> str | None:
        self.config["auto_start"] = bool(v)
        return self._save()

    def set_int(self, key: str, v: int) -> str | None:
        lo, hi = {"start_delay_s": (3, 60), "remind_days": (0, 90)}[key]
        self.config[key] = max(lo, min(hi, int(v)))
        return self._save()

    # ================================================================ 思い出させる(FR-25)
    def check_reminder(self) -> None:
        now = self._now()
        n = reminder.due(self.config["drives"], int(self.config["remind_days"]), self.config.get("last_reminded_at"), now,
                         self._snoozed())
        if n is None:
            return
        self.config["last_reminded_at"] = now.isoformat(timespec="seconds")
        self._save()
        self._notify(f"{n} 日バックアップしていません", N_REMIND_TEXT, self._on_reminder_click)
        self.log.info("reminder shown days=%d", n)

    def _on_reminder_click(self) -> None:
        def then() -> None:
            ids = [d["id"] for d in self.config["drives"] if d["id"] in self.connected and d["first_done"]]
            if ids and self.config["sources"] and self.run is None:
                self.start_backup(ids[0], trigger="manual")
            else:
                self._show_page()

        self.refresh_drives(then)

    # ================================================================ 開く
    def open_backup_folder(self, drive_id: str | None = None) -> str | None:
        did = drive_id or self.last_meta.get("drive_id")
        p = self.connected.get(str(did)) if did else None
        if p is None and self.connected:
            p = next(iter(self.connected.values()))
        if p is None:
            return N_NOT_CONNECTED
        target = drives.pc_dir(p.root, self.pc())
        if not os.path.isdir(target):
            target = drives.backup_dir(p.root)
        try:
            self._opener(drives.display_path(target))
        except OSError as e:
            self.log.warning("open folder failed: %s", type(e).__name__)
            return "開けませんでした"
        return None

    # ================================================================ 状態表示・usage・diagnostics
    def days_since_success(self) -> int | None:
        return reminder.days_since(reminder.last_success(self.config["drives"]), self._now())

    def status_text(self) -> str:
        """FR-26。名前・文字は入れない(B-14)。"""
        if self.run is not None and not self.run.plan_only:
            return f"バックアップ中 {self.percent()}%"
        if self.pending is not None or self.run is not None:
            return "待機中"
        d = self.days_since_success()
        if d is None:
            return "まだバックアップしていません"
        return "最後のバックアップ: 今日" if d == 0 else f"最後のバックアップ: {d} 日前"

    def _set_status(self) -> None:
        try:
            self.ctx.set_tray_status(self.status_text())
        except Exception:  # noqa: BLE001 - 状態表示は失敗しても続ける
            pass

    def _update_tray_items(self) -> None:
        t = self._tray_cancel
        if t is None:
            return
        try:
            t.set_visible(self.pending is not None or self.run is not None)
        except Exception:  # noqa: BLE001
            pass

    def usage(self, days: int) -> list[UsageSeries]:
        """FR-27: 日ごとの「バックアップした回数」(primary)と「コピーしたファイル数」。ops.jsonl の数だけ。"""
        from deskkit.usage import UsageSeries

        today = self._now().date()   # 渡された時計の日付で区切る(テストの固定の時計と実際の日付がずれても数え違えない)
        runs = self.ops.per_day(days, lambda r: 1 if r.get("result") == "ok" else 0, today)
        files = self.ops.per_day(days, lambda r: int(r.get("new", 0)) + int(r.get("changed", 0)), today)
        return [
            UsageSeries("backups", "バックアップした回数", runs, unit="回", primary=True,
                        hint="公開から 90 日で 0 回のままなら紹介から外す(R-2)"),
            UsageSeries("copied", "コピーしたファイル", files, unit="件", good_when="neutral"),
        ]

    def diagnostics(self) -> dict[str, str | int | bool]:
        """FR-28: 件数・コード・設定だけ(ラベル・文字・パスは入れない。INV-4)。"""
        d = self.days_since_success()
        return {
            "state": self.state,
            "drives": len(self.config["drives"]),
            "connected": len(self.connected),
            "sources": len(self.config["sources"]),
            "last_result": self.last_result_code,
            "days_since_success": -1 if d is None else d,
            "auto_start": bool(self.config["auto_start"]),
            "remind_days": int(self.config["remind_days"]),
            "start_delay_s": int(self.config["start_delay_s"]),
        }
