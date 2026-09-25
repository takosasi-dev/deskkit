# host との結合: 設定の読み込みと既定値の補完、監視スレッド・定期フルスキャン・作業スレッドの管理、
# トレイ項目・通知(保留つき)・ホットキー・CLI 転送・Control Center の画面生成・スヌーズとモード連携の停止。
# 重い処理(スキャン・移動・undo・CLI・ルールの実績集計)は作業スレッド1本で順に行い、結果は ctx.call_soon で返す。
from __future__ import annotations

import concurrent.futures as cf
import dataclasses
import ntpath
import threading
import time
from collections.abc import Callable
from typing import Any

from deskkit.modules.dropsort.cli import run_command
from deskkit.modules.dropsort.config import Config, ConfigError, fill_defaults, load_config
from deskkit.modules.dropsort.notifier import Notifier
from deskkit.modules.dropsort.oplog import LockBusyError
from deskkit.modules.dropsort.service import BatchResult, CycleResult, DropSortService, UndoResult, reason_text
from deskkit.modules.dropsort.stats import RuleStats

TITLE = "DropSort"
# stop() が作業スレッドを待つ上限(秒)。ファイル1件の移動の途中では止めない(打ち切らない)ので、
# 別ドライブへの大きなコピー中などで上限を超えたら待つのをやめて戻り、作業スレッドはその1件を終えてから止まる。
STOP_WAIT_S = 5.0
CLI_LOCK_TIMEOUT_S = 10.0


class DropSortModule:
    def __init__(self, ctx: Any, *, api: Any = None, watch_api: Any = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.ctx = ctx
        sec, changed = fill_defaults(dict(ctx.settings()))
        self.cfg: Config = load_config(sec)  # 全体が不正なら ConfigError → host がこのモジュールだけ停止中にする
        if changed:
            try:
                ctx.write_settings(sec)  # 足りないキーを既定値で書き戻す
            except Exception as e:  # noqa: BLE001 - 書けなくても既定値で動く
                ctx.log.warning("既定値を settings.json に書き戻せません: %s", e)
        if api is None:
            from deskkit.modules.dropsort._win32 import RealWin32

            api = RealWin32()
        self.api = api
        self._watch_api = watch_api
        self.service = DropSortService(api, ctx.data_dir, self.cfg, clock=clock, log=ctx.log)
        self.notifier = Notifier(ctx)
        self._executor: cf.ThreadPoolExecutor | None = None
        self._inflight: set[cf.Future[Any]] = set()
        self._inflight_mu = threading.Lock()
        self._watcher: Any = None
        self._watch_dead: str | None = None
        self._scan_running = False
        self._rescan = False
        self._running = False
        self._unresolved_notified = False
        self._listeners: list[Callable[[], None]] = []
        self._timers: dict[str, Any] = {}
        self._tray: dict[str, Any] = {}
        self.last_cycle: CycleResult | None = None
        self.hotkey_ok: bool | None = None
        self._page_request: tuple[str, str | None] | None = None
        self._manual_kick = False
        self._current_mode: str | None = None   # modeshift.switched で知った今のモード(reverted で None)
        self._stats_cache: tuple[tuple[Any, ...], dict[str, RuleStats]] | None = None
        self._stats_waiters: list[Callable[[dict[str, RuleStats]], None]] | None = None

    # ------------------------------------------------------------ 起動・停止
    def start(self) -> None:
        self._running = True
        self.service.cancel.clear()
        self._executor = cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="dropsort")
        self._build_tray()
        self._build_quick_actions()
        if self.cfg.hotkey_undo_last:
            self.hotkey_ok = self.ctx.hotkeys.register_text("undo_last", self.cfg.hotkey_undo_last)
            if self.hotkey_ok:
                self.ctx.hotkeys.triggered("undo_last").connect(lambda: self.undo(1))
        self._timers["notify"] = self.ctx.start_timer(3000, self.notifier.poll)
        self._timers["full"] = self.ctx.start_timer(int(self.cfg.full_scan_interval_s * 1000),
                                                    lambda: self.request_scan(0))
        self._timers["debounce"] = self.ctx.start_timer(600, self._kick, single_shot=True)
        self._timers["debounce"].stop()
        self._timers["follow"] = self.ctx.start_timer(1000, self._kick, single_shot=True)
        self._timers["follow"].stop()
        self._subscribe()
        self.service.resolve()
        self._ensure_watcher()
        self._update_status()
        self.request_scan(0)

    def stop(self) -> None:
        """停止を要求し、作業スレッドを最大 STOP_WAIT_S 秒だけ待つ(メインスレッドを長く止めない)。
        処理中のファイル1件は最後まで終える(途中で打ち切らない)。残りのファイルは次の起動時のスキャンで扱う。"""
        self._running = False
        self.service.cancel.set()
        self._stop_watcher()
        ex, self._executor = self._executor, None
        if ex is None:
            return
        ex.shutdown(wait=False, cancel_futures=True)
        with self._inflight_mu:
            busy = [f for f in self._inflight if not f.done()]
        if busy:
            _done, not_done = cf.wait(busy, timeout=STOP_WAIT_S)
            if not_done:
                self.ctx.log.warning("作業スレッドが %.0f 秒で終わりませんでした。処理中の1件を終えてから止まります",
                                     STOP_WAIT_S)

    # ------------------------------------------------------------ 作業スレッド
    def _job(self, fn: Callable[[], Any]) -> Callable[[], Any]:
        def job() -> Any:
            try:
                return fn()
            except Exception as e:  # noqa: BLE001 - 結果としてメインスレッドへ渡す
                self.ctx.log.exception("作業スレッドで例外")
                return e

        return job

    def _track(self, fut: cf.Future[Any]) -> None:
        def forget(f: cf.Future[Any]) -> None:
            with self._inflight_mu:
                self._inflight.discard(f)

        with self._inflight_mu:
            self._inflight.add(fut)
        fut.add_done_callback(forget)

    def _submit(self, fn: Callable[[], Any], done: Callable[[Any], None] | None = None) -> bool:
        ex = self._executor
        if ex is None:
            return False
        try:
            fut = ex.submit(self._job(fn))
        except RuntimeError:  # 停止処理と行き違い
            return False
        self._track(fut)
        if done is not None:
            fut.add_done_callback(lambda f: self.ctx.call_soon(lambda: done(f.result()) if not f.cancelled() else None))
        return True

    def run_blocking(self, fn: Callable[[], Any]) -> Any:
        """CLI 用: 作業スレッドで fn を実行して結果を待つ。メインスレッドならローカルのイベントループを回し、
        待つ間もトレイ・ホットキー・他モジュールを止めない。例外は結果として返す。"""
        ex = self._executor
        fut: cf.Future[Any] | None = None
        if ex is not None:
            try:
                fut = ex.submit(self._job(fn))
            except RuntimeError:
                fut = None
        if fut is None:
            return self._job(fn)()  # 停止中(作業スレッドが無い): その場で実行する
        self._track(fut)
        from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

        if QCoreApplication.instance() is None or threading.current_thread() is not threading.main_thread():
            return fut.result()
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(30)
        timer.timeout.connect(lambda: loop.quit() if fut.done() else None)
        timer.start()
        if not fut.done():
            loop.exec()
        timer.stop()
        return fut.result()

    # ------------------------------------------------------------ 監視
    def _ensure_watcher(self) -> None:
        dl = self.service.downloads
        want = self.cfg.watch_mode == "rdcw" and dl is not None and self._watch_dead is None and self._running
        if self._watcher is not None and (not want or self._watcher.path != dl):
            self._stop_watcher()
        if want and self._watcher is None and dl is not None:
            from deskkit.modules.dropsort.watcher import DirWatcher

            wapi = self._watch_api
            if wapi is None:
                from deskkit.modules.dropsort._win32 import RealWatchApi

                wapi = self._watch_api = RealWatchApi()
            self._watcher = DirWatcher(wapi, dl, self._on_fs_signal, self._on_watch_dead, self.ctx.log)
            self._watcher.start()

    def _stop_watcher(self) -> None:
        w, self._watcher = self._watcher, None
        if w is not None:
            w.stop()

    def _on_fs_signal(self) -> None:  # 監視スレッドから
        self.ctx.call_soon(lambda: self.request_scan(600))

    def _on_watch_dead(self, reason: str) -> None:  # 監視スレッドから
        def main() -> None:
            self._watch_dead = reason
            self._watcher = None
            self.ctx.log.warning("監視を停止し、定期フルスキャンだけで続けます: %s", reason)
            self._update_status()
            self._fire_listeners()

        self.ctx.call_soon(main)

    @property
    def watch_label(self) -> str:
        if self.cfg.watch_mode == "poll":
            return "定期スキャンのみ"
        if self._watch_dead:
            return "定期スキャン(監視停止中)"
        return "リアルタイム監視"

    # ------------------------------------------------------------ スキャン
    def request_scan(self, delay_ms: int = 600, *, manual: bool = False) -> None:
        """manual=True は利用者が押した「今すぐスキャン」。スヌーズ・モード連携の停止中でも1回動かす(契約 §1)。"""
        if not self._running:
            return
        t = self._timers.get("debounce")
        if t is None:
            return
        if manual:
            self._manual_kick = True
        t.start(max(0, int(delay_ms)))

    def _kick(self) -> None:
        if not self._running:
            return
        manual, self._manual_kick = self._manual_kick, False
        if self.cfg.paused:
            self._update_status()
            return
        if not manual and self.auto_blocked() is not None:
            self._update_status()  # スヌーズ・モード中: 自動の整理はしない(再開時にフルスキャン)
            return
        if self._scan_running:
            self._rescan = True
            self._manual_kick = self._manual_kick or manual  # 手動の要求は次の1回に持ち越す
            return
        self._scan_running = True
        if not self._submit(self.service.run_cycle, self._on_cycle):
            self._scan_running = False

    def _on_cycle(self, res: Any) -> None:
        self._scan_running = False
        if isinstance(res, Exception):
            self._update_status()
            return
        assert isinstance(res, CycleResult)
        self.last_cycle = res
        if res.skipped == "unresolved":
            self._stop_watcher()
            if not self._unresolved_notified:
                self._unresolved_notified = True
                self.notifier.push("ダウンロードフォルダが見つかりません",
                                   "場所を解決できないため整理を止めています。見つかりしだい再開します。", "error")
        elif res.ok or res.skipped == "locked":
            if self._unresolved_notified and res.downloads:
                self._unresolved_notified = False
            self._ensure_watcher()
        if res.baseline_created is not None:
            self.notifier.push("DropSort を開始しました",
                               f"今あるファイル {res.baseline_created} 件は基準線として記録し、自動では動かしません。", "info")
        if res.rule_problems_changed:
            probs = [(r.name, m) for r, m in self.service.validate_rules(self.service.downloads)
                     if not self.service.rule_checks.get(r.index) or not self.service.rule_checks[r.index].unavailable]
            if probs:
                self.notifier.push("使えないルールがあります", "\n".join(f"{n}: {m}" for n, m in probs[:4]), "warn")
        self._notify_events(res.events)
        if res.next_check_s is not None and self._running:
            self._timers["follow"].start(int(min(res.next_check_s, self.cfg.full_scan_interval_s) * 1000) + 300)
        if self._rescan:
            self._rescan = False
            self.request_scan(200)
        self._update_status()
        self._fire_listeners()

    def _notify_events(self, events: list[dict[str, Any]]) -> None:
        by: dict[str, list[dict[str, Any]]] = {}
        for e in events:
            by.setdefault(str(e.get("op")), []).append(e)
        moved = by.get("move", []) + by.get("archive", [])
        if moved:
            lines = [f"{e['name']} → {ntpath.basename(ntpath.dirname(str(e.get('dst') or ''))) or e.get('dst')}"
                     for e in moved[:3]]
            if len(moved) > 3:
                lines.append(f"ほか {len(moved) - 3} 件")
            self.notifier.push(f"{len(moved)} 件を整理しました", "\n".join(lines), "ok")
        bad = by.get("refused", []) + by.get("failed", [])
        if bad:
            self.notifier.push(f"{len(bad)} 件の移動を見送りました",
                               "\n".join(f"{e['name']}: {reason_text(e.get('reason'))}" for e in bad[:3]), "warn")
        flagged = by.get("flagged", [])
        if flagged:
            self.notifier.push(f"要確認のファイルが {len(flagged)} 件あります",
                               "\n".join(f"{e['name']}: {reason_text(e.get('reason'))}" for e in flagged[:3])
                               + "\nダウンロードフォルダに残しています。", "warn", lambda: self.open_page("flagged"))
        dry = by.get("would_move", []) + by.get("would_archive", []) + by.get("would_refuse", [])
        if dry:
            self.notifier.push(f"試運転: {len(dry)} 件の予定を記録しました",
                               "\n".join(f"{e['name']}({reason_text(e.get('reason')) or 'OK'})" for e in dry[:3]),
                               "info", self.show_dryrun_dialog)

    # ------------------------------------------------------------ 状態・トレイ
    def status_line(self) -> str:
        if self.service.downloads is None:
            return "ダウンロードフォルダを解決できません"
        if self.cfg.paused:
            return "一時停止中"
        blocked = self.auto_blocked()
        if blocked == "mode":
            return f"モード「{self.mode_label(self._current_mode)}」中は自動の整理を止めています"
        if blocked == "snooze":
            return "スヌーズ中は自動の整理を止めています"
        snap = self.service.get_snapshot()
        parts = ["監視中" if self.cfg.watch_mode == "rdcw" and not self._watch_dead else "定期スキャン中"]
        try:
            n = self.service.today_counts()
            parts.append(f"今日 {n['move'] + n['archive']} 件移動")
        except OSError:
            pass
        if snap.get("pending"):
            parts.append(f"保留 {snap['pending']}")
        if snap.get("flagged"):
            parts.append(f"要確認 {snap['flagged']}")
        return " · ".join(parts)

    def _update_status(self) -> None:
        try:
            self.ctx.set_tray_status(self.status_line())
        except Exception as e:  # noqa: BLE001
            self.ctx.log.debug("トレイの状態を更新できません: %s", e)
        item = self._tray.get("pause")
        if item is not None:
            item.set_checked(self.cfg.paused)

    def _build_tray(self) -> None:
        self._tray["pause"] = self.ctx.add_tray_action("一時停止", lambda: self.set_paused(not self.cfg.paused),
                                                       checkable=True, checked=self.cfg.paused)
        self._tray["undo"] = self.ctx.add_tray_action("直前の1件を元に戻す", lambda: self.undo(1))
        self._tray["dry"] = self.ctx.add_tray_action("試運転の結果を見る…", self.show_dryrun_dialog)
        self.ctx.add_tray_separator()
        self._tray["scan"] = self.ctx.add_tray_action("今すぐスキャン", lambda: self.request_scan(0, manual=True))

    def _build_quick_actions(self) -> None:
        """トレイに無い操作だけをクイックアクションに足す(トレイ項目は host が自動で候補に入れる)。"""
        add = getattr(self.ctx, "add_quick_action", None)
        if add is None:
            return
        from deskkit.ui.theme import G

        add("既存ファイルを整理(試運転)", lambda: self.open_page("existing", "existing_dry"),
            keywords="dropsort sort-existing 既存 整理 基準線 ダウンロード", glyph=G.SEARCH)
        add("アーカイブを今すぐ評価(試運転)", self.show_archive_preview,
            keywords="dropsort archive-now アーカイブ 古い ダウンロード", glyph=G.ARCHIVE)
        add("要確認のファイルを見る", lambda: self.open_page("flagged"),
            keywords="dropsort flagged 要確認 危険 二重拡張子", glyph=G.SHIELD)
        add("操作履歴を見る(元に戻す)", lambda: self.open_page("history"),
            keywords="dropsort oplog undo 履歴 元に戻す", glyph=G.LOG)
        add("振り分けルールを編集", lambda: self.open_page("rules"),
            keywords="dropsort rules ルール 振り分け", glyph=G.FILTER)

    def open_page(self, section: str, action: str | None = None) -> None:
        """Control Center の DropSort 画面を開き、指定のセクションを表示する(画面側が要求を受け取る)。"""
        self._page_request = (section, action)
        show = getattr(self.ctx, "show_page", None)
        if show is not None:
            show()
        self._fire_listeners()

    def take_page_request(self) -> tuple[str, str | None] | None:
        req, self._page_request = self._page_request, None
        return req

    def show_archive_preview(self) -> None:
        """アーカイブの試運転結果をダイアログで出す(ファイルは動かさない)。"""
        def done(r: Any) -> None:
            if isinstance(r, Exception):
                self.ctx.notify(TITLE, f"アーカイブを評価できませんでした: {r}", level="warn")
                return
            from deskkit.modules.dropsort.page import RecordsDialog

            RecordsDialog(self.ctx.window_parent(), "アーカイブの評価(試運転)", r.items, with_time=False,
                          note=f"{self.cfg.archive.idle_days} 日以上触られていないファイルです。ファイルはまだ動いていません。",
                          on_open_page=lambda: self.open_page("archive")).exec()

        self.archive_now(False, done)

    def show_dryrun_dialog(self) -> None:
        from deskkit.modules.dropsort.page import DryRunDialog

        DryRunDialog(self, self.ctx.window_parent()).exec()

    # ------------------------------------------------------------ 画面・リスナー
    def add_listener(self, cb: Callable[[], None]) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[], None]) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    def _fire_listeners(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception as e:  # noqa: BLE001 - 画面側の不具合で処理を止めない
                self.ctx.log.warning("画面の更新で例外: %s", e)

    def create_page(self) -> Any:
        from deskkit.modules.dropsort.page import DropSortPage

        return DropSortPage(self)

    # ------------------------------------------------------------ 設定
    def update_settings(self, section: dict[str, Any], *, restart: bool = False) -> None:
        """画面からの設定変更。検査してから保存し、その場で反映する。不正なら ConfigError。"""
        sec, _ = fill_defaults(section)
        cfg = load_config(sec)
        self.ctx.write_settings(sec, restart=restart)
        if restart:
            return
        old = self.cfg
        self.cfg = cfg
        self.service.set_config(cfg)
        self._stats_cache = None
        if cfg.full_scan_interval_s != old.full_scan_interval_s and "full" in self._timers:
            self._timers["full"].setInterval(int(cfg.full_scan_interval_s * 1000))
        self._update_status()
        self.request_scan(300)
        self._fire_listeners()

    def settings_dict(self) -> dict[str, Any]:
        sec, _ = fill_defaults(self.ctx.settings_dict())
        return sec

    def set_paused(self, paused: bool) -> None:
        sec = self.settings_dict()
        sec["paused"] = bool(paused)
        try:
            self.ctx.write_settings(sec)
        except Exception as e:  # noqa: BLE001
            self.ctx.log.warning("一時停止の状態を保存できません: %s", e)
        self.cfg = dataclasses.replace(self.cfg, paused=bool(paused))
        self.service.set_config(self.cfg)
        self._update_status()
        if not paused:
            self.request_scan(0)
        self._fire_listeners()

    # ------------------------------------------------------------ スヌーズ・モード連携(契約 §1・§2)
    def _subscribe(self) -> None:
        on = getattr(self.ctx, "on", None)
        if on is None:
            return
        on("modeshift.switched", self._on_mode_switched)
        on("modeshift.reverted", self._on_mode_reverted)
        on("host.snooze_changed", self._on_snooze_changed)

    def snoozed(self) -> bool:
        fn = getattr(self.ctx, "is_snoozed", None)
        if fn is None:
            return False
        try:
            return bool(fn())
        except Exception:  # noqa: BLE001 - 判定できなければ止めない側に倒す(利用者の一時停止は別にある)
            return False

    def list_modes(self) -> list[tuple[str, str]]:
        fn = getattr(self.ctx, "list_modes", None)
        if fn is None:
            return []
        try:
            return [(str(n), str(lb)) for n, lb in fn()]
        except Exception:  # noqa: BLE001
            return []

    def mode_label(self, name: str | None) -> str:
        if not name:
            return ""
        return next((lb for n, lb in self.list_modes() if n == name and lb), name)

    @property
    def mode_paused(self) -> str | None:
        """今のモードが pause_in_modes に入っていればそのモード名。"""
        m = self._current_mode
        return m if m is not None and m in self.cfg.pause_in_modes else None

    def auto_blocked(self) -> str | None:
        """自動の整理を止めている理由コード: user(一時停止) / mode / snooze。動いていれば None。"""
        if self.cfg.paused:
            return "user"
        if self.mode_paused is not None:
            return "mode"
        if self.snoozed():
            return "snooze"
        return None

    def _resumed(self, was: str | None) -> None:
        """止まっていたのが動けるようになったらフルスキャンする(判断は実状態から行うので取りこぼさない)。通知はしない。"""
        if was is not None and self.auto_blocked() is None:
            self.request_scan(0)
        self._update_status()
        self._fire_listeners()

    def _on_mode_switched(self, payload: Any) -> None:
        mode = payload.get("mode") if hasattr(payload, "get") else None
        if not isinstance(mode, str):
            return
        was = self.auto_blocked()
        self._current_mode = mode
        self._resumed(was)

    def _on_mode_reverted(self, payload: Any) -> None:
        was = self.auto_blocked()
        self._current_mode = None
        self._resumed(was)

    def _on_snooze_changed(self, payload: Any) -> None:
        snoozed = payload.get("snoozed") if hasattr(payload, "get") else None
        if snoozed is False:
            self._resumed("snooze")
        else:
            self._update_status()
            self._fire_listeners()

    # ------------------------------------------------------------ ルールの実績(D3)
    def _stats_key(self) -> tuple[Any, ...]:
        key: list[Any] = [int(self.service.clock() // 3600)]
        for p in (self.service.oplog.path, self.service.dryrun.path):
            try:
                st = p.stat()
                key += [st.st_size, st.st_mtime_ns]
            except OSError:
                key += [0, 0]
        return tuple(key)

    def rule_stats(self, done: Callable[[dict[str, RuleStats]], None]) -> None:
        """ルールごとの実績。ログが変わっていなければキャッシュをすぐ返し、変わっていれば作業スレッドで数える。"""
        key = self._stats_key()
        c = self._stats_cache
        if c is not None and c[0] == key:
            done(c[1])
            return
        if self._stats_waiters is not None:
            self._stats_waiters.append(done)
            return
        self._stats_waiters = [done]
        from deskkit.modules.dropsort import stats

        now = self.service.clock()

        def after(r: Any) -> None:
            waiters, self._stats_waiters = self._stats_waiters or [], None
            if isinstance(r, Exception):
                return
            self._stats_cache = (key, r)
            for w in waiters:
                w(r)

        if not self._submit(lambda: stats.compute(self.service.oplog.path, self.service.dryrun.path, now), after):
            self._stats_waiters = None

    # ------------------------------------------------------------ 診断(契約 §1)
    def diagnostics(self) -> dict[str, str | int | bool]:
        """診断レポート用の要約。件数・モード・真偽・理由コードだけ(パス・ファイル名・URL・モード名は入れない)。"""
        rules = self.cfg.rules
        n_apply = sum(1 for r in rules if r.apply and r.error is None)
        checks = self.service.rule_checks
        n_invalid = sum(1 for r in rules if r.error is not None
                        or (r.index in checks and not checks[r.index].ok and not checks[r.index].unavailable))
        snap = self.service.get_snapshot()
        if not rules:
            mode = "none"
        elif n_apply == 0:
            mode = "dry-run"
        elif n_apply == len(rules):
            mode = "apply"
        else:
            mode = "mixed"
        watch = "poll" if self.cfg.watch_mode == "poll" else ("poll_fallback" if self._watch_dead else "rdcw")
        try:
            today = self.service.today_counts()
            moved_today = today["move"] + today["archive"]
        except OSError:
            moved_today = -1
        return {
            "running": self._running,
            "downloads_resolved": self.service.downloads is not None,
            "rules_mode": mode,
            "rules_total": len(rules),
            "rules_apply": n_apply,
            "rules_dry_run": sum(1 for r in rules if not r.apply and r.error is None),
            "rules_invalid": n_invalid,
            "rules_template": sum(1 for r in rules if r.is_template),
            "watch": watch,
            "paused": self.auto_blocked() is not None,
            "paused_reason": self.auto_blocked() or "none",
            "pause_in_modes": len(self.cfg.pause_in_modes),
            "pending": int(snap.get("pending") or 0),
            "flagged": int(snap.get("flagged") or 0),
            "dry_run_present": int(snap.get("dryrun_present") or 0),
            "moved_today": moved_today,
            "archive_enabled": self.cfg.archive.enabled,
            "archive_mode": self.cfg.archive.mode,
        }

    # ------------------------------------------------------------ 明示操作(作業スレッドで実行)
    def undo(self, count: int, done: Callable[[UndoResult | Exception], None] | None = None) -> None:
        def after(r: Any) -> None:
            if isinstance(r, LockBusyError):
                self.notifier.push("元に戻せませんでした", "他の DropSort の処理が実行中です。少し待ってからやり直してください。", "warn")
            elif isinstance(r, UndoResult):
                if r.nothing:
                    self.ctx.notify(TITLE, "元に戻せる操作はありません", level="info")
                else:
                    if r.restored:
                        self.ctx.notify(f"{len(r.restored)} 件を元に戻しました",
                                        "\n".join(f"{i['name']}" for i in r.restored[:3]), level="ok")
                    if r.problems:
                        self.ctx.notify(f"{len(r.problems)} 件は戻せませんでした",
                                        "\n".join(f"{i['name']}: {reason_text(i.get('reason'))}" for i in r.problems[:3]),
                                        level="warn")
            if done is not None:
                done(r)
            self._update_status()
            self._fire_listeners()

        self._submit(lambda: self.service.undo(count, timeout=5.0), after)

    def sort_existing(self, apply: bool, done: Callable[[BatchResult | Exception], None]) -> None:
        def after(r: Any) -> None:
            done(r)
            self._update_status()
            self._fire_listeners()

        self._submit(lambda: self.service.sort_existing(apply, timeout=5.0), after)

    def archive_now(self, apply: bool, done: Callable[[BatchResult | Exception], None]) -> None:
        def after(r: Any) -> None:
            done(r)
            self._fire_listeners()

        self._submit(lambda: self.service.archive_now(apply, timeout=5.0), after)

    def acknowledge(self, name: str, done: Callable[[Any], None] | None = None) -> None:
        def after(r: Any) -> None:
            if done is not None:
                done(r)
            self._update_status()
            self._fire_listeners()

        self._submit(lambda: self.service.acknowledge(name), after)

    def create_folder(self, path: str) -> int:
        """画面で利用者が「フォルダを作成」を押したときだけ呼ぶ(ルールの移動先)。0 なら成功。"""
        from deskkit.modules.dropsort._win32 import ERROR_ALREADY_EXISTS, ERROR_SUCCESS

        p = ntpath.normpath(path)
        missing: list[str] = []
        cur = p
        while cur and not self.api.is_dir(cur):
            missing.append(cur)
            parent = ntpath.dirname(cur)
            if parent == cur:
                break
            cur = parent
        for d in reversed(missing):
            e = self.api.create_directory(d)
            if e not in (ERROR_SUCCESS, ERROR_ALREADY_EXISTS):
                return int(e)
        return ERROR_SUCCESS

    # ------------------------------------------------------------ 利用状況(§7)
    def usage(self, days: int) -> list[Any]:
        """日ごとの件数だけを返す(パス・ファイル名は入れない)。R-3 の判定材料として undo 率を hint に書く。"""
        import datetime as dt

        from deskkit.modules.dropsort.oplog import tail_jsonl
        from deskkit.usage import UsageSeries, count_jsonl, day_index

        idx = day_index(days)
        auto = [0] * days
        undo = [0] * days
        flag = [0] * days
        auto_ids: set[str] = set()
        undo_of: list[str] = []
        for r in tail_jsonl(self.service.oplog.path, 0, max_bytes=64 * 1024 * 1024):
            try:
                d = dt.datetime.fromisoformat(str(r.get("ts", ""))).date()
            except ValueError:
                continue
            i = idx.get(d)
            if i is None:
                continue
            op = r.get("op")
            if op in ("move", "archive") and str(r.get("source") or "auto").startswith("auto"):
                auto[i] += 1
                auto_ids.add(str(r.get("id")))
            elif op == "undo":
                undo[i] += 1
                undo_of.append(str(r.get("undo_of")))
            elif op in ("flagged", "refused"):
                flag[i] += 1
        moved = sum(auto)
        undone_auto = sum(1 for x in undo_of if x in auto_ids)
        ratio = f"{undone_auto}/{moved} 件 = {undone_auto * 100 / moved:.1f}%" if moved else "自動移動なし"
        would = count_jsonl(self.service.dryrun.path, days, lambda r: r.get("op") == "would_move")
        return [
            UsageSeries("auto_moves", "自動で整理した件数", auto, "件", primary=True),
            UsageSeries("undos", "元に戻した件数", undo, "件", good_when="low",
                        hint=f"R-3: 自動移動の1割を超えたら自動振り分けを止める(現在 {ratio})",
                        extra={"undone_auto": undone_auto, "auto_moves": moved}),
            UsageSeries("flagged_refused", "要確認・移動拒否", flag, "件", good_when="neutral"),
            UsageSeries("would_move", "試運転の移動予定", would, "件", good_when="neutral"),
        ]

    # ------------------------------------------------------------ CLI(host 経由)
    def handle_cli(self, args: list[str]) -> tuple[int, str]:
        """移動・コピー・ロック待ちを含むので作業スレッドで実行し、終わってから結果を返す(IPC の応答は完了後)。"""
        argv = list(args)
        res = self.run_blocking(lambda: run_command(self.service, argv, lock_timeout=CLI_LOCK_TIMEOUT_S))
        if isinstance(res, Exception):
            raise res
        code, text = res
        self._update_status()
        self._fire_listeners()
        return int(code), str(text)


__all__ = ["DropSortModule", "ConfigError"]
