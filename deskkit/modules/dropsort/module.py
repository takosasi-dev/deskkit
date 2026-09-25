# host との結合: 設定の読み込みと既定値の補完、監視スレッド・定期フルスキャン・作業スレッドの管理、
# トレイ項目・通知(保留つき)・ホットキー・CLI 転送・Control Center の画面生成。
# 重い処理(スキャン・移動・undo)は作業スレッド1本で順に行い、結果は ctx.call_soon でメインスレッドへ渡す。
from __future__ import annotations

import concurrent.futures as cf
import dataclasses
import ntpath
import time
from collections.abc import Callable
from typing import Any

from deskkit.modules.dropsort.cli import run_command
from deskkit.modules.dropsort.config import Config, ConfigError, fill_defaults, load_config
from deskkit.modules.dropsort.notifier import Notifier
from deskkit.modules.dropsort.oplog import LockBusyError
from deskkit.modules.dropsort.service import BatchResult, CycleResult, DropSortService, UndoResult, reason_text

TITLE = "DropSort"


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

    # ------------------------------------------------------------ 起動・停止
    def start(self) -> None:
        self._running = True
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
        self.service.resolve()
        self._ensure_watcher()
        self._update_status()
        self.request_scan(0)

    def stop(self) -> None:
        self._running = False
        self._stop_watcher()
        ex, self._executor = self._executor, None
        if ex is not None:
            ex.shutdown(wait=True, cancel_futures=True)

    # ------------------------------------------------------------ 作業スレッド
    def _submit(self, fn: Callable[[], Any], done: Callable[[Any], None] | None = None) -> bool:
        ex = self._executor
        if ex is None:
            return False

        def job() -> Any:
            try:
                return fn()
            except Exception as e:  # noqa: BLE001 - 結果としてメインスレッドへ渡す
                self.ctx.log.exception("作業スレッドで例外")
                return e

        fut = ex.submit(job)
        if done is not None:
            fut.add_done_callback(lambda f: self.ctx.call_soon(lambda: done(f.result()) if not f.cancelled() else None))
        return True

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
    def request_scan(self, delay_ms: int = 600) -> None:
        if not self._running:
            return
        t = self._timers.get("debounce")
        if t is None:
            return
        t.start(max(0, int(delay_ms)))

    def _kick(self) -> None:
        if not self._running:
            return
        if self.cfg.paused:
            self._update_status()
            return
        if self._scan_running:
            self._rescan = True
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
        self._tray["scan"] = self.ctx.add_tray_action("今すぐスキャン", lambda: self.request_scan(0))

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
        code, text = run_command(self.service, list(args), lock_timeout=10.0)
        self._update_status()
        self._fire_listeners()
        return code, text


__all__ = ["DropSortModule", "ConfigError"]
