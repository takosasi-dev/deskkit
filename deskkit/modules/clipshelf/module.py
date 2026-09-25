# ClipShelf モジュール本体。設定の読み込み・DB・監視(WM_CLIPBOARDUPDATE を ctx.on_native で受ける)・ホットキー・
# トレイ・パレット・書き込み・自動貼り付けを束ね、Control Center の画面とパレットに操作窓口を提供する。
# 本文を扱うのはメモリ上とパレットの表示だけ。ログ・通知・ops.jsonl には理由コード・exe 名・ID・件数しか出さない。
from __future__ import annotations

import json
import logging
from collections import Counter, deque
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, QTimer, Signal

from deskkit.catalog import info
from deskkit.modules.clipshelf import config as cfgmod
from deskkit.modules.clipshelf import policy, search, snippets, transforms
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
# 記録を止めている理由(診断・画面用の理由コード。§9.4 の判定理由とは別。判定理由はどれも paused になる)
PAUSE_MANUAL = "manual"     # 利用者の一時停止(トレイ・ホットキー・画面)
PAUSE_SNOOZE = "snoozed"    # 本体のスヌーズ(ctx.is_snoozed)
PAUSE_MODE = "mode"         # ModeShift の「このモード中は止める」(M3)
PAUSE_LABELS: dict[str, str] = {
    PAUSE_MANUAL: "一時停止中",
    PAUSE_SNOOZE: "スヌーズ中(記録しない)",
    PAUSE_MODE: "モード中のため停止(記録しない)",
}
MODE_STATE_FILE = "mode_state.json"  # 最後に受けた ModeShift のモード名(再起動をまたいで停止状態を保つ)
SWEEP_INTERVAL_MS = 60_000            # 短命記録の期限切れ掃除と状態表示の見直し(低頻度)
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
        self._sweep_timer: QTimer | None = None
        self._pause_item: Any = None
        self._last_status = ""
        self._active_mode: str | None = self._load_mode_state()
        self.writer = ClipWriter(self.api, ctx.hidden_hwnd, lambda: self.config.open_retry)
        self.paster = Paster(self.api, ctx.foreground, self.ops, self.log)
        self.monitor = ClipMonitor(
            self.api, owner_hwnd=ctx.hidden_hwnd, config=lambda: self.config, store=lambda: self.store,
            paused=lambda: bool(self.pause_reasons()), log=self.log, ops=self.ops, now=now, on_decision=self._on_decision,
        )

    # ================================================================ ライフサイクル
    def start(self) -> None:
        self._open_store()
        if self.store is not None:
            self.monitor.trim(self.config)  # 起動時の保持上限(FR-8)
            self.sweep_expired()            # 起動時の短命記録の掃除(C3)
        self.monitor.mark_startup()  # D-15: 起動時点の内容は記録しない
        self._debounce = self.ctx.start_timer(self.config.debounce_ms, self._on_debounced, single_shot=True)
        self._debounce.stop()
        self._paste_timer = self.ctx.start_timer(150, self._on_paste_timer, single_shot=True)
        self._paste_timer.stop()
        self._sweep_timer = self.ctx.start_timer(SWEEP_INTERVAL_MS, self._periodic)
        self.ctx.on_native(WM_CLIPBOARDUPDATE, self._on_clip_update)
        # M3: ModeShift のモード切替・復帰を受けて記録を止める/再開する(通知はしない。状態表示だけ)
        self.ctx.on("modeshift.switched", self._on_mode_switched)
        self.ctx.on("modeshift.reverted", self._on_mode_reverted)
        self.ctx.on("host.snooze_changed", lambda _p: self._update_status())
        self._register_hotkeys()
        self._build_tray()
        self._build_quick_actions()
        self._update_status()
        self.log.info("clipshelf started mode=%s history=%d", self.config.mode, self.counts()["history"])
        if self.store is not None and self.store.undecryptable:
            bad = self.store.undecryptable_counts()
            if bad["history"]:
                self.ctx.notify("ClipShelf", f"復号できない履歴が {bad['history']} 件あります(表示しません)。"
                                "別ユーザー・再作成したプロファイルのデータの可能性があります。"
                                "全消去で削除できます。", level="warn")
            if bad["snippets"]:
                self.ctx.notify("ClipShelf", f"復号できない定型文が {bad['snippets']} 件あります(表示しません)。"
                                "別ユーザー・再作成したプロファイルのデータの可能性があります。", level="warn")

    def stop(self) -> None:
        for t in (self._debounce, self._paste_timer, self._sweep_timer):
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
                       f"pinned={c['pinned']} snippets={c['snippets']} store={'ok' if self.store else 'error'} "
                       f"stopped_by={','.join(self.pause_reasons()) or 'none'}")
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
        reasons = self.pause_reasons()
        if reasons:
            return PAUSE_LABELS[reasons[0]]
        return "観察モード(記録しない)" if self.config.mode == "observe" else "記録中"

    def _update_status(self) -> None:
        self._last_status = self.status_text()
        self.ctx.set_tray_status(self._last_status)
        if self._pause_item is not None:
            self._pause_item.set_checked(self.paused)
        self.notifier.changed.emit()

    # ---------------------------------------------------------------- 記録を止める理由(手動・スヌーズ・モード)
    def is_snoozed(self) -> bool:
        """本体のスヌーズ中か。古い本体(is_snoozed が無い)では常に False。"""
        fn = getattr(self.ctx, "is_snoozed", None)
        if not callable(fn):
            return False
        try:
            return bool(fn())
        except Exception:  # noqa: BLE001 - 本体側の不具合で記録処理を止めない(スヌーズではない扱い)
            self.log.warning("is_snoozed failed")
            return False

    def mode_paused(self) -> bool:
        return self._active_mode is not None and self._active_mode in self.config.pause_in_modes

    def pause_reasons(self) -> list[str]:
        """記録を止めている理由コード(優先順)。空なら止めていない。"""
        out: list[str] = []
        if self.paused:
            out.append(PAUSE_MANUAL)
        if self.is_snoozed():
            out.append(PAUSE_SNOOZE)
        if self.mode_paused():
            out.append(PAUSE_MODE)
        return out

    def active_mode(self) -> str | None:
        """最後に受けた ModeShift のモード名(画面表示用。ログ・診断には出さない)。"""
        return self._active_mode

    def mode_choices(self) -> list[tuple[str, str]]:
        """「このモード中は止める」の選択肢(ModeShift の設定にあるモード)。古い本体では空。"""
        fn = getattr(self.ctx, "list_modes", None)
        if not callable(fn):
            return []
        try:
            got = fn()
        except Exception:  # noqa: BLE001
            self.log.warning("list_modes failed")
            return []
        out: list[tuple[str, str]] = []
        for pair in got or []:
            if isinstance(pair, (tuple, list)) and len(pair) == 2 and isinstance(pair[0], str) and pair[0]:
                out.append((pair[0], str(pair[1] or pair[0])))
        return out

    def _on_mode_switched(self, payload: Mapping[str, Any]) -> None:
        mode = payload.get("mode")
        if isinstance(mode, str) and mode:
            self._set_active_mode(mode)

    def _on_mode_reverted(self, _payload: Mapping[str, Any]) -> None:
        self._set_active_mode(None)

    def clear_mode_pause(self) -> None:
        """「モード中のため停止」を利用者が手で解除する(ModeShift の復帰を取りこぼした場合の逃げ道)。"""
        self._set_active_mode(None)

    def _set_active_mode(self, mode: str | None) -> None:
        before = self.mode_paused()
        self._active_mode = mode
        self._save_mode_state()
        after = self.mode_paused()
        if before != after:
            self.log.info("recording %s by mode", "paused" if after else "resumed")  # モード名は出さない
        self._update_status()

    def _load_mode_state(self) -> str | None:
        try:
            obj = json.loads((self.ctx.data_dir / MODE_STATE_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        mode = obj.get("mode") if isinstance(obj, dict) else None
        return mode if isinstance(mode, str) and mode else None

    def _save_mode_state(self) -> None:
        path = self.ctx.data_dir / MODE_STATE_FILE
        try:
            if self._active_mode is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"mode": self._active_mode}, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            self.log.warning("mode state write failed: %s", type(e).__name__)

    # ---------------------------------------------------------------- 定期処理(短命記録の掃除・状態表示の見直し)
    def _periodic(self) -> None:
        self.sweep_expired()
        if self.status_text() != self._last_status:  # スヌーズの開始・終了をイベント無しでも拾う
            self._update_status()

    def sweep_expired(self) -> int:
        """期限を過ぎた短命記録(C3)を消す。操作ログには件数だけ。"""
        if self.store is None or not self.store.is_open:
            return 0
        try:
            n = self.store.expire()
        except StoreError as e:
            self.log.error("expire failed: %s", e)
            return 0
        if n:
            self.ops.write("expired", deleted=n)
            self.log.info("expired deleted=%d", n)
            self.notifier.changed.emit()
        return n

    # ---------------------------------------------------------------- 診断(本文・exe 名・モード名を入れない)
    def diagnostics(self) -> dict[str, str | int | bool]:
        c = self.counts()
        reasons = self.pause_reasons()
        return {
            "mode": self.config.mode,
            "store_ok": self.store is not None,
            "history": c["history"],
            "pinned": c["pinned"],
            "snippets": c["snippets"],
            "undecryptable": c["undecryptable"],
            "short_lived_items": self.store.short_lived_count() if self.store is not None else 0,
            "short_lived_apps": len(self.config.short_lived_exes),
            "recording_paused": bool(reasons),
            "paused_reasons": ",".join(reasons) if reasons else "none",
            "pause_in_modes": len(self.config.pause_in_modes),
            "exclude_apps": len(self.config.exclude_exes),
            "unknown_owner_policy": self.config.unknown_owner_policy,
            "auto_paste": self.config.auto_paste,
            "hotkeys_failed": sum(1 for ok in self.hotkey_ok.values() if not ok),
        }

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

    def retention_preview(self, max_items: int, max_days: int) -> int:
        """その保持上限にしたら今ある履歴が何件消えるか(消さない)。"""
        if self.store is None or not self.store.is_open:
            return 0
        return self.store.trim_preview(max_items, max_days)

    # ================================================================ パレット用の窓口
    def search_history(self, query: str) -> list[Item]:
        return search.search(self.store.history(), query, 500) if self.store else []

    def search_snippets(self, query: str) -> list[Item]:
        return search.search(self.store.snippets(), query, 500) if self.store else []

    def snippet_preview(self, text: str, values: Mapping[str, str] | None = None) -> snippets.Expansion:
        """プレビュー用の展開。{clipboard} は本文を読まず目印に置き換え、テキストが無いときだけ警告する。
        values が None なら入力欄・選択欄は〔ラベル〕の目印、辞書ならその値(パレットの入力フォームのライブ表示)。"""
        exp = snippets.expand(text, self._now(), None, date_format=self.config.date_format,
                              time_format=self.config.time_format, clipboard_marker=CLIPBOARD_MARKER, values=values)
        if exp.used_clipboard and not self.writer.has_text():
            exp.warnings.append("クリップボードにテキストが無いため {clipboard} は空になります")
        return exp

    @staticmethod
    def snippet_fields(item: Item) -> list[snippets.Field]:
        """貼り付け前にパレットで聞く欄(定型文の {input:…} / {select:…})。履歴は常に空。"""
        return snippets.fields(item.text) if item.kind == KIND_SNIPPET else []

    def choose(self, item: Item, values: Mapping[str, str] | None = None, transform: str | None = None) -> None:
        """パレットで選んだ項目をクリップボードに置き、元の foreground へ戻す(FR-12)。
        values は定型文の入力欄・選択欄の値(無い欄は既定値)。transform は利用者が明示的に選んだ変換の ID(C2)。
        値と変換結果はクリップボードに書くだけで、ログ・DB には残さない(INV-1/2)。"""
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
                                  time_format=self.config.time_format, values=dict(values or {}))
            text = exp.text
        else:
            text = item.text
        if transform:
            try:
                text = transforms.apply(transform, text)
            except ValueError:
                self.log.warning("unknown transform=%s", transform)
                return
            if not text:
                self.ctx.notify("ClipShelf", "変換した結果が空になったため、クリップボードに置きませんでした", level="info")
                return
        if not self.writer.write(text, item.id, marks):
            self.ctx.notify("ClipShelf", "クリップボードに書き込めませんでした(他のアプリが使用中)", level="warn")
            return
        if self.store is not None:
            try:
                self.store.touch(item.id)
            except StoreError as e:
                self.log.error("touch failed: %s", e)
        self.log.info("item chosen id=%d kind=%s transform=%s", item.id, item.kind, transform or "none")
        if transform:
            self.ops.write("transform_paste", transform=transform)  # 変換の種類だけ(本文・文字数は書かない)
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
        bad = self.store.undecryptable_counts()["history"]
        extra = f"復号できない履歴 {bad} 件も削除します。" if bad else ""
        ok, checks = widgets.confirm(
            parent, "履歴を全消去",
            f"履歴 {c['history']} 件(うちピン {c['pinned']} 件)を完全に削除します。{extra}元に戻せません。定型文は消えません。",
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
        self.sweep_expired()  # 期限切れの短命記録を出さない(定期掃除の間隔の隙間を埋める)
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
