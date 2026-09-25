# ClipShelf モジュール本体。設定の読み込み・DB・監視(WM_CLIPBOARDUPDATE を ctx.on_native で受ける)・ホットキー・
# トレイ・パレット・書き込み・自動貼り付けを束ね、Control Center の画面とパレットに操作窓口を提供する。
# 本文を扱うのはメモリ上とパレットの表示だけ。ログ・通知・ops.jsonl には理由コード・exe 名・ID・件数しか出さない。
from __future__ import annotations

import logging
from collections import Counter, deque
from collections.abc import Callable
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, QTimer, Signal

from deskkit.catalog import info
from deskkit.modules.clipshelf import config as cfgmod
from deskkit.modules.clipshelf import policy, search, snippets
from deskkit.modules.clipshelf._win32 import WM_CLIPBOARDUPDATE, RealWin32, Win32Api
from deskkit.modules.clipshelf.crypto import Cipher, CryptoError, DpapiCipher
from deskkit.modules.clipshelf.monitor import ClipMonitor, Decision
from deskkit.modules.clipshelf.ops import OpsLog
from deskkit.modules.clipshelf.paster import Paster
from deskkit.modules.clipshelf.store import KIND_SNIPPET, Item, Store, StoreError
from deskkit.modules.clipshelf.writer import ClipWriter, Marks, PlainResult
from deskkit.ui.theme import G
from deskkit.usage import UsageSeries, count_dates, count_jsonl

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

    from deskkit.context import ModuleContextImpl
    from deskkit.modules.clipshelf.palette import Palette

CLIPBOARD_MARKER = "〔クリップボードの内容〕"
REASON_LABELS: dict[str, str] = {
    policy.RECORDED: "記録した",
    policy.OBSERVE_ONLY: "観察のみ(記録対象)",
    policy.SELF_ORIGIN: "ClipShelf の書き込み",
    policy.DUPLICATE: "直前と同じ",
    policy.EXCLUDE_FORMAT: "除外形式あり",
    policy.HISTORY_DISALLOWED: "履歴不可の印",
    policy.EXCLUDED_APP: "除外アプリ",
    policy.OWNER_UNKNOWN: "コピー元が不明",
    policy.NOT_TEXT: "テキストなし",
    policy.EMPTY: "空・空白のみ",
    policy.TOO_LARGE: "大きすぎる",
    policy.PAUSED: "一時停止中",
    policy.CLIPBOARD_BUSY: "クリップボード使用中",
}


class _Notifier(QObject):
    changed = Signal()      # 件数・状態が変わった
    decided = Signal()      # 判定が1件増えた


def _is_clip_reason(reason: str) -> Callable[[dict[str, Any]], bool]:
    def pick(r: dict[str, Any]) -> bool:
        return r.get("op") == "clip" and r.get("reason") == reason

    return pick


def _now() -> datetime:
    return datetime.now().astimezone()


class ClipShelfModule:
    def __init__(self, ctx: ModuleContextImpl, *, api: Win32Api | None = None, cipher: Cipher | None = None,
                 now: Callable[[], datetime] = _now) -> None:
        self.ctx = ctx
        self.log: logging.Logger = ctx.log
        self.accent = info("clipshelf").accent
        section = dict(ctx.settings_dict())
        section.pop("enabled", None)
        merged, changed = cfgmod.merge_defaults(section)
        self.config = cfgmod.parse(merged)  # 不正なら ValueError → host が停止中にする
        if changed:
            try:
                ctx.write_settings(merged)
            except Exception as e:  # noqa: BLE001 - 書き戻せなくても既定値で動く
                self.log.warning("既定値の書き戻しに失敗: %s", type(e).__name__)
        self.api: Win32Api = api or RealWin32()
        self.cipher: Cipher = cipher or DpapiCipher(self.api)
        self._now = now
        self.ops = OpsLog(ctx.data_dir / "ops.jsonl", now)
        self.notifier = _Notifier()
        self.store: Store | None = None
        self.store_error: str | None = None
        self.paused = False
        self.hotkey_ok: dict[str, bool] = {}
        self._decisions: deque[Decision] = deque(maxlen=60)
        self._today: date = now().date()
        self._today_counts: Counter[str] = Counter()
        self._palette: Palette | None = None
        self._target_hwnd = 0
        self._pending_paste_target = 0
        self._debounce: QTimer | None = None
        self._paste_timer: QTimer | None = None
        self._pause_item: Any = None
        self.writer = ClipWriter(self.api, ctx.hidden_hwnd, lambda: self.config.open_retry)
        self.paster = Paster(self.api, ctx.foreground, self.ops, self.log)
        self.monitor = ClipMonitor(
            self.api, owner_hwnd=ctx.hidden_hwnd, config=lambda: self.config, store=lambda: self.store,
            paused=lambda: self.paused, log=self.log, ops=self.ops, now=now, on_decision=self._on_decision,
        )

    # ================================================================ ライフサイクル
    def start(self) -> None:
        self._open_store()
        if self.store is not None:
            self.monitor.trim(self.config)  # 起動時の保持上限(FR-8)
        self.monitor.mark_startup()  # D-15: 起動時点の内容は記録しない
        self._debounce = self.ctx.start_timer(self.config.debounce_ms, self._on_debounced, single_shot=True)
        self._debounce.stop()
        self._paste_timer = self.ctx.start_timer(150, self._on_paste_timer, single_shot=True)
        self._paste_timer.stop()
        self.ctx.on_native(WM_CLIPBOARDUPDATE, self._on_clip_update)
        self._register_hotkeys()
        self._build_tray()
        self._build_quick_actions()
        self._update_status()
        self.log.info("clipshelf started mode=%s history=%d", self.config.mode, self.counts()["history"])
        if self.store is not None and self.store.undecryptable:
            self.ctx.notify("ClipShelf", f"復号できない項目が {self.store.undecryptable} 件あります(表示しません)。"
                            "別ユーザー・再作成したプロファイルのデータの可能性があります。全消去を検討してください。", level="warn")

    def stop(self) -> None:
        for t in (self._debounce, self._paste_timer):
            if t is not None:
                t.stop()
        if self._palette is not None:
            try:
                self._palette.hide()
                self._palette.deleteLater()
            except RuntimeError:
                pass
            self._palette = None
        if self.store is not None:
            self.store.close()
        self.log.info("clipshelf stopped")

    def handle_cli(self, args: list[str]) -> tuple[int, str]:
        cmd = args[0] if args else ""
        if cmd == "open":
            self.open_palette()
            return 0, "ok"
        if cmd in ("pause", "resume", "toggle-pause"):
            self.set_paused(not self.paused if cmd == "toggle-pause" else cmd == "pause")
            return 0, "paused" if self.paused else "recording"
        if cmd == "plain-text":
            return (0, "ok") if self.plain_text() == PlainResult.OK else (1, "failed")
        if cmd == "status":
            c = self.counts()
            return 0, (f"mode={self.config.mode} paused={int(self.paused)} history={c['history']} "
                       f"pinned={c['pinned']} snippets={c['snippets']} store={'ok' if self.store else 'error'}")
        return 2, "unsupported"

    def create_page(self) -> QWidget:
        from deskkit.modules.clipshelf.page import ClipShelfPage

        return ClipShelfPage(self)

    # ================================================================ 内部: 起動
    def _open_store(self) -> None:
        st = Store(self.ctx.data_dir / "clipshelf.db", self.cipher, self._now)
        try:
            st.open()
        except StoreError as e:
            self.store = None
            self.store_error = str(e)
            self.log.error("store open failed: %s", e)
            self.ctx.notify("ClipShelf", f"履歴 DB を開けないため記録を止めています。{e}", level="error")
            return
        self.store = st
        self.store_error = None
        self.log.info("store loaded rows=%d undecryptable=%d in %.1f ms", len(st.items()), st.undecryptable, st.load_ms)

    def _register_hotkeys(self) -> None:
        handlers = {"open_palette": self.open_palette, "plain_text": self.plain_text_hotkey, "toggle_pause": self.toggle_pause}
        for name, cb in handlers.items():
            text = self.config.hotkeys.get(name, "")
            if not text:
                continue
            ok = self.ctx.hotkeys.register_text(name, text)
            self.hotkey_ok[name] = ok
            if ok:
                self.ctx.hotkeys.triggered(name).connect(cb)
            else:
                self.log.warning("hotkey not registered name=%s", name)  # 通知は host がまとめて出す(FR-22)

    def _build_tray(self) -> None:
        self.ctx.add_tray_action("パレットを開く", self.open_palette)
        self._pause_item = self.ctx.add_tray_action("記録を一時停止", self._tray_toggle_pause, checkable=True, checked=self.paused)
        self.ctx.add_tray_action("書式なしにする", self.plain_text_hotkey)
        self.ctx.add_tray_separator()
        self.ctx.add_tray_action("履歴を全消去…", lambda: self.clear_all_interactive(self.ctx.window_parent()))

    def _build_quick_actions(self) -> None:
        """トレイに無い操作だけをクイックアクションに足す(トレイ項目は host が自動で候補に入れる)。
        コールバックは検索窓が閉じて元の foreground に戻った後(約150ms後)に呼ばれるので、
        パレットを開く操作はコールバックの中で foreground を記録し直す(D-13 の判定もその時点で行う)。"""
        add = getattr(self.ctx, "add_quick_action", None)
        if add is None:
            return
        add("定型文を開く", lambda: self.open_palette(snippets_tab=True), keywords="snippet 定型文 テンプレート clipshelf",
            glyph=G.SPARKLE)
        add("記録を一時停止する", lambda: self.set_paused(True), keywords="pause 停止 clipshelf クリップボード",
            glyph=G.PAUSE, enabled=lambda: not self.paused)
        add("記録を再開する", lambda: self.set_paused(False), keywords="resume 再開 clipshelf クリップボード",
            glyph=G.PLAY, enabled=lambda: self.paused)
        add("ClipShelf: 記録を始める(記録モード)", lambda: self.set_mode("record"), keywords="record 記録 モード clipshelf",
            glyph=G.CLIPBOARD, enabled=lambda: self.config.mode != "record")
        add("ClipShelf: 観察モードに戻す(記録しない)", lambda: self.set_mode("observe"), keywords="observe 観察 モード clipshelf",
            glyph=G.EYE, enabled=lambda: self.config.mode != "observe")

    def set_mode(self, mode: str) -> None:
        def mut(s: dict[str, Any]) -> None:
            s["mode"] = mode

        err = self.update_settings(mut)
        if err:
            self.ctx.notify("ClipShelf", f"切り替えられませんでした。{err}", level="error")
        else:
            self.ctx.notify("ClipShelf", "記録を始めました" if mode == "record" else "観察モードにしました(記録しません)", level="ok")

    # ================================================================ 利用状況(MODULE_GUIDE §7)
    def usage(self, days: int) -> list[UsageSeries]:
        """日ごとの件数だけを返す。履歴の件数は平文の created_at 列から数え、復号しない。"""
        today = self._now().date()

        def ops_count(pick: Callable[[dict[str, Any]], bool]) -> list[int]:
            a = count_jsonl(self.ops.path, days, pick, today=today)
            b = count_jsonl(self.ops.path.with_name(self.ops.path.name + ".1"), days, pick, today=today)
            return [x + y for x, y in zip(a, b, strict=True)]

        opens = ops_count(lambda r: r.get("op") == "palette_open")
        recorded = [0] * days
        if self.store is not None and self.store.is_open:
            try:
                recorded = count_dates(self.store.history_created_dates(), days, today)
            except StoreError:
                pass
        # 除外は理由ごとに数える(期間内の分だけ)
        by_reason: dict[str, int] = {}
        excluded = [0] * days
        for reason in sorted(policy.EXCLUSION_REASONS):
            per = ops_count(_is_clip_reason(reason))
            if any(per):
                by_reason[REASON_LABELS.get(reason, reason)] = sum(per)
                excluded = [x + y for x, y in zip(excluded, per, strict=True)]
        observed = ops_count(lambda r: r.get("op") == "clip" and r.get("reason") == policy.OBSERVE_ONLY)
        sent = ops_count(lambda r: r.get("op") == "auto_paste" and r.get("result") == "sent")
        skipped = ops_count(lambda r: r.get("op") == "auto_paste" and (
            str(r.get("result", "")).startswith("skipped_") or str(r.get("would", "")).startswith("skipped_")))
        dry = ops_count(lambda r: r.get("op") == "auto_paste" and r.get("result") == "dry_run" and r.get("would") == "sent")
        series = [
            UsageSeries("palette_open", "パレットを開いた回数", opens, primary=True,
                        hint="週5回未満なら撤退を検討(R-4)"),
            UsageSeries("recorded", "記録した履歴(現存分)", recorded, unit="件", good_when="neutral",
                        hint="保持上限で消えた分は数えません"),
            UsageSeries("excluded", "記録しなかったコピー", excluded, unit="件", good_when="neutral",
                        extra={"by_reason": by_reason}),
        ]
        if any(observed) or self.config.mode == "observe":
            series.append(UsageSeries("observe_only", "記録対象だったコピー(観察モード)", observed, unit="件", good_when="neutral"))
        if any(sent) or any(skipped) or any(dry) or self.config.auto_paste != "off":
            series.append(UsageSeries("auto_paste_sent", "自動貼り付け(送信)", sent, good_when="neutral"))
            series.append(UsageSeries("auto_paste_skipped", "自動貼り付け(見送り)", skipped, good_when="neutral",
                                      extra={"dry_run_would_send": sum(dry)}))
        return series

    # ================================================================ 監視
    def _on_clip_update(self, _wparam: int, _lparam: int) -> None:
        if self._debounce is None:
            return
        self._debounce.setInterval(max(0, self.config.debounce_ms))
        self._debounce.start()  # 連続通知はまとめて1回だけ処理(FR-1)

    def _on_debounced(self) -> None:
        self.monitor.process()

    def _on_decision(self, dec: Decision) -> None:
        if not dec.reason:
            return
        d = dec.ts.date()
        if d != self._today:
            self._today = d
            self._today_counts.clear()
        self._today_counts[dec.reason] += 1
        self._decisions.appendleft(dec)
        self.notifier.decided.emit()
        self.notifier.changed.emit()

    # ================================================================ 状態
    def counts(self) -> dict[str, int]:
        if self.store is None:
            return {"history": 0, "pinned": 0, "snippets": 0, "undecryptable": 0}
        return self.store.counts()

    def decisions(self) -> list[Decision]:
        return list(self._decisions)

    def today_exclusions(self) -> dict[str, int]:
        if self._now().date() != self._today:
            return {}
        return {k: v for k, v in self._today_counts.items() if k in policy.EXCLUSION_REASONS}

    def status_text(self) -> str:
        if self.store is None:
            return "停止: DB を開けません"
        if self.paused:
            return "一時停止中"
        return "観察モード(記録しない)" if self.config.mode == "observe" else "記録中"

    def _update_status(self) -> None:
        self.ctx.set_tray_status(self.status_text())
        if self._pause_item is not None:
            self._pause_item.set_checked(self.paused)
        self.notifier.changed.emit()

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self.log.info("paused=%s", paused)
        self._update_status()

    def toggle_pause(self) -> None:
        self.set_paused(not self.paused)
        self.ctx.notify("ClipShelf", "記録を一時停止しました" if self.paused else "記録を再開しました", level="info")

    def _tray_toggle_pause(self) -> None:
        self.set_paused(not self.paused)

    def safe(self, fn: Callable[..., Any], label: str) -> Callable[..., Any]:
        return self.ctx.safe(fn, label)

    # ================================================================ 設定
    def update_settings(self, mutate: Callable[[dict[str, Any]], None], *, restart: bool = False) -> str | None:
        """設定を書き換えて保存する。成功なら None、失敗なら利用者向けの短い理由。"""
        sec = dict(self.ctx.settings_dict())
        merged, _ = cfgmod.merge_defaults({k: v for k, v in sec.items() if k != "enabled"})
        mutate(merged)
        try:
            new_cfg = cfgmod.parse(merged)
        except ValueError as e:
            return str(e)
        try:
            self.ctx.write_settings(merged, restart=restart)
        except Exception as e:  # noqa: BLE001 - SettingsError 等を画面に返す
            self.log.warning("settings write failed: %s", type(e).__name__)
            return str(e) or type(e).__name__
        self.config = new_cfg
        self._update_status()
        return None

    # ================================================================ パレット用の窓口
    def search_history(self, query: str) -> list[Item]:
        return search.search(self.store.history(), query, 500) if self.store else []

    def search_snippets(self, query: str) -> list[Item]:
        return search.search(self.store.snippets(), query, 500) if self.store else []

    def snippet_preview(self, text: str) -> snippets.Expansion:
        """プレビュー用の展開。{clipboard} は本文を読まず目印に置き換え、テキストが無いときだけ警告する。"""
        exp = snippets.expand(text, self._now(), None, date_format=self.config.date_format,
                              time_format=self.config.time_format, clipboard_marker=CLIPBOARD_MARKER)
        if exp.used_clipboard and not self.writer.has_text():
            exp.warnings.append("クリップボードにテキストが無いため {clipboard} は空になります")
        return exp

    def choose(self, item: Item) -> None:
        """パレットで選んだ項目をクリップボードに置き、元の foreground へ戻す(FR-12)。"""
        marks: Marks = {}
        if item.kind == KIND_SNIPPET:
            clip_text: str | None = None
            if snippets.uses_clipboard(item.text):
                got = self.writer.read_for_expansion()
                if got is None:
                    self.ctx.notify("ClipShelf", "クリップボードが使用中のため展開できませんでした", level="warn")
                    return
                clip_text, marks = got  # 除外形式は展開結果にも引き継ぐ(FR-17)
            exp = snippets.expand(item.text, self._now(), clip_text, date_format=self.config.date_format,
                                  time_format=self.config.time_format)
            text = exp.text
        else:
            text = item.text
        if not self.writer.write(text, item.id, marks):
            self.ctx.notify("ClipShelf", "クリップボードに書き込めませんでした(他のアプリが使用中)", level="warn")
            return
        if self.store is not None:
            try:
                self.store.touch(item.id)
            except StoreError as e:
                self.log.error("touch failed: %s", e)
        self.log.info("item chosen id=%d kind=%s", item.id, item.kind)
        target = self._target_hwnd
        restored = self.paster.restore(target)
        if self._palette is not None:
            self._palette.dismiss(animated=False)
        if not restored:
            self.ctx.notify("ClipShelf", "クリップボードに置きました", level="ok")
        elif self.config.auto_paste != "off" and self._paste_timer is not None:
            self._pending_paste_target = target
            self._paste_timer.start()
        self.notifier.changed.emit()

    def _on_paste_timer(self) -> None:
        target, self._pending_paste_target = self._pending_paste_target, 0
        self.paster.auto_paste(self.config.auto_paste, target, self.config.auto_paste_deny_exes)

    def toggle_pin(self, item: Item) -> None:
        if self.store is None:
            return
        self.store.set_pinned(item.id, not item.pinned)
        self.log.info("pin id=%d pinned=%s", item.id, item.pinned)
        self.notifier.changed.emit()

    def delete_item(self, item: Item) -> None:
        if self.store is None:
            return
        if self.store.delete(item.id):
            self.ops.write("item_delete", id=item.id, kind=item.kind)
            self.log.info("item deleted id=%d kind=%s", item.id, item.kind)
        self.notifier.changed.emit()

    def save_snippet(self, item_id: int | None, name: str, text: str) -> Item | None:
        if self.store is None:
            return None
        name = name.strip() or "無題の定型文"
        try:
            if item_id is None:
                item: Item | None = self.store.add_snippet(name, text)
            else:
                item = self.store.get(item_id) if self.store.update_snippet(item_id, name, text) else None
        except (CryptoError, StoreError) as e:
            self.log.error("snippet save failed: %s", type(e).__name__)
            return None
        if item is not None:
            self.log.info("snippet saved id=%d", item.id)
        self.notifier.changed.emit()
        return item

    def clear_all_interactive(self, parent: QWidget | None) -> None:
        from deskkit.ui import widgets

        if self.store is None:
            widgets.message(parent, "全消去できません", self.store_error or "DB が開かれていません", kind="error")
            return
        c = self.counts()
        ok, checks = widgets.confirm(
            parent, "履歴を全消去",
            f"履歴 {c['history']} 件(うちピン {c['pinned']} 件)を完全に削除します。元に戻せません。定型文は消えません。",
            ok_text="全消去", danger=True,
            checks=[("ピンも消す", False), ("現在のクリップボードも空にする", True)],
        )
        if not ok:
            return
        self.clear_all(include_pins=checks[0], clear_clipboard=checks[1])

    def clear_all(self, *, include_pins: bool, clear_clipboard: bool) -> int:
        if self.store is None:
            return 0
        try:
            n = self.store.clear_all(include_pins)
        except StoreError as e:
            self.log.error("clear_all failed: %s", e)
            self.ctx.notify("ClipShelf", f"全消去に失敗しました。{e}", level="error")
            return 0
        self.ops.write("clear_all", deleted=n, pins_deleted=include_pins)
        self.log.info("clear_all deleted=%d pins_deleted=%s", n, include_pins)
        if clear_clipboard and not self.writer.clear():
            self.ctx.notify("ClipShelf", "クリップボードを空にできませんでした(使用中)", level="warn")
        self.ctx.notify("ClipShelf", f"履歴を {n} 件削除しました", level="ok")
        if self._palette is not None:
            self._palette.refresh()
        self.notifier.changed.emit()
        return n

    # ================================================================ ホットキーの動作
    def open_palette(self, snippets_tab: bool = False) -> None:
        if self._palette is not None and self._palette.isVisible():
            self._palette.dismiss()
            return
        fg = self.ctx.foreground()
        blocked = "game" if fg.is_game else ("fullscreen" if fg.is_fullscreen else
                                             ("fullscreen_unknown" if fg.is_fullscreen is None else None))
        if blocked is not None:
            self.ops.write("palette_blocked", reason=blocked)
            self.log.info("palette_blocked reason=%s exe=%s", blocked, fg.exe or "(不明)")  # D-13: 開かない
            return
        self._target_hwnd = int(fg.hwnd or 0)
        self.ops.write("palette_open")
        self.log.info("palette_open")
        from deskkit.modules.clipshelf.palette import Palette

        if self._palette is None:
            self._palette = Palette(self)
        self._palette.open_for(self.api.window_rect(self._target_hwnd) if self._target_hwnd else None,
                               1 if snippets_tab else 0)

    def plain_text_hotkey(self) -> None:
        self.plain_text()

    def plain_text(self) -> PlainResult:
        r = self.writer.plain_text()
        self.ops.write("plain_text", result=r.value)
        self.log.info("plain_text result=%s", r.value)
        if r == PlainResult.NO_TEXT:
            self.ctx.notify("ClipShelf", "クリップボードにテキストがありません", level="info")
        elif r == PlainResult.BUSY:
            self.ctx.notify("ClipShelf", "クリップボードが使用中のため書式なしにできませんでした", level="warn")
        elif r == PlainResult.FAILED:
            self.ctx.notify("ClipShelf", "書式なしにできませんでした", level="warn")
        else:
            self.ctx.notify("ClipShelf", "書式なしにしました", level="ok")
        return r

    def running_exe_names(self) -> list[str]:
        try:
            return self.api.running_exe_names()
        except OSError:
            return []
