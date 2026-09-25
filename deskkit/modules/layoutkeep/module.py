# LayoutKeep の入口。ModuleContext に沿ってトレイ項目・ホットキー・システムイベント・layout.apply を配線し、
# 保存(名前付きプリセット)・計画・適用・取り消し・提案・自動スナップショット・新規ウィンドウの配置を1か所で行う。
# foreground がゲーム/全画面等なら何もしない(D-8 / FR-10)。スヌーズ中は自動の処理だけ止める(手で押した操作は止めない)。
# アプリの起動・終了・ウィンドウを閉じる操作は持たない(INV-1)。ログ・oplog にタイトルを書かない(INV-8)。
from __future__ import annotations

import re
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QObject, Signal

from deskkit.usage import UsageSeries

from . import config, matcher, monitors, planner, windows
from ._win32 import Win32Api
from .applier import Applier, ApplyResult
from .config import Config
from .model import (
    AUTO_BEFORE,
    AUTO_LATEST,
    DEFAULT_FIELDS,
    Layout,
    RawWindow,
    SavedEntry,
    SigResult,
    WindowInfo,
    same_windows,
    validate_preset_name,
)
from .placer import NewWindowWatcher, PlaceEvent, event_for
from .planner import Plan
from .store import BrokenLayoutError, Store, StoreWriteError, is_signature, now_iso
from .sysevents import DisplayWatcher

WAIT_INTERVAL_MS = 1000
SNAPSHOT_TICK_MS = 60_000  # 自動スナップショットの時刻確認(ここでは列挙しない。書くのは条件を満たしたときだけ)
_SNAPSHOT_RETRY_CODES = ("snoozed", "game", "fullscreen", "fullscreen_unknown", "sig_changed", "no_signature", "disabled")


class LayoutKeepSignals(QObject):
    changed = Signal()  # 状態(構成・提案・レイアウト・設定)が変わった → 画面を更新


@dataclass
class _WaitState:
    request_id: str
    sig: str
    deadline: float
    pending: set[int]
    outcome: dict[int, str]  # 添字 → "moved" / スキップ理由
    errors: list[dict[str, Any]] = field(default_factory=list)
    retries: int = 0
    timer: Any = None
    preset: Any = None  # layout.apply で受けた preset(そのまま返す)


def _real_api() -> Win32Api:
    from ._win32 import RealWin32

    return RealWin32()


def preset_label(preset: str | None) -> str:
    """通知・画面用のプリセット表記。"""
    if preset is None:
        return "既定のプリセット"
    if preset == AUTO_BEFORE:
        return "抜く前の配置(自動保存)"
    if preset == AUTO_LATEST:
        return "自動保存の最新"
    return f"プリセット「{preset}」"


class LayoutKeepModule:
    def __init__(self, ctx: Any, api: Win32Api | None = None) -> None:
        self.ctx = ctx
        self.log = ctx.log
        section, filled = config.fill_defaults(ctx.settings_dict())
        section.pop("enabled", None)
        self.cfg: Config = config.parse(section)  # mode / id_source が不正なら ValueError(host が停止中にする)
        if filled:
            try:
                ctx.write_settings(section)
            except Exception as e:  # noqa: BLE001 - 既定値の書き戻しに失敗しても動作は続ける
                self.log.warning("既定値を settings.json に書き戻せません: %s", e)
        self.api: Win32Api = api if api is not None else _real_api()
        self.store = Store(ctx.data_dir)
        self.applier = Applier(self.api, self.store)
        self.signals = LayoutKeepSignals()
        self.disabled_reason: str | None = None
        self.proposal_sig: str | None = None
        self.last_plan: Plan | None = None
        self.last_plan_preset: str | None = None
        self.last_sig: SigResult | None = None
        self.last_result: str = ""
        self.hotkey_ok: dict[str, bool] = {}
        # 診断(diagnostics)用の最後の結果コード。タイトル・exe 名・パスは入れない
        self.codes: dict[str, str] = {"apply": "", "snapshot": "", "place": ""}
        self._wait: _WaitState | None = None
        self._tray: dict[str, Any] = {}
        self._running = False
        # L2: 自動スナップショット
        self._stable_sig: str | None = None
        self._stable_since = 0.0
        self._last_snap: dict[str, float] = {}
        self.last_snapshot_at: str = ""
        self._snap_timer: Any = None
        # L3: 新規ウィンドウの配置
        self.placer = NewWindowWatcher(self.api, self._could_be_target)
        self.place_events: deque[PlaceEvent] = deque(maxlen=50)
        self._place_timer: Any = None
        self._place_on = False
        self.watcher = DisplayWatcher(ctx, lambda: self.cfg.debounce_ms, lambda: self.cfg.settle_checks,
                                      self._sig_for_watch, self._on_settled, self._on_kick)

    # ================================================================ ライフサイクル
    def start(self) -> None:
        aw = self.ctx.dpi_awareness()
        if aw != "per_monitor_aware_v2":
            # FR-1 / D-4: PMv2 でなければ有効化しない(awareness は host の責務なので変更しない)
            self.disabled_reason = f"DPI awareness が {aw} です(Per-Monitor V2 が必要)"
            self.log.warning("LayoutKeep を無効のままにします: %s", self.disabled_reason)
            self.ctx.notify("LayoutKeep は無効です", f"{self.disabled_reason}。座標がずれるため配置の保存・復元を行いません。",
                            level="warn")
            self.ctx.set_tray_status("無効: DPI awareness が PMv2 でない")
            return
        self._running = True
        if self.cfg.invalid_targets:
            for msg in self.cfg.invalid_targets:
                self.log.warning("無効にした対象: %s", msg)
            self.ctx.notify("LayoutKeep: 対象の一部を無効にしました",
                            f"{len(self.cfg.invalid_targets)} 件の targets が不正です。設定画面で確認してください。", level="warn")
        self._build_tray()
        self._register_hotkeys()
        self.watcher.attach()
        self.ctx.on("layout.apply", self._on_apply_event)
        self._stable_sig = self.current_signature().signature
        self._stable_since = time.monotonic()
        self._sync_timers()
        self.refresh_status()

    def stop(self) -> None:
        self._running = False
        self.watcher.cancel()
        for t in (self._snap_timer, self._place_timer):
            if t is not None:
                t.stop()
        self._snap_timer = self._place_timer = None
        self._place_on = False
        if self._wait is not None and self._wait.timer is not None:
            self._wait.timer.stop()
        self._wait = None

    def _sync_timers(self) -> None:
        """設定に合わせて自動スナップショットの確認タイマと新規ウィンドウのポーリングを動かす/止める。
        ポーリングは有効な間だけ動かす(無効ならタイマごと止めて EnumWindows も呼ばない)。"""
        if not self._running:
            return
        if self.cfg.snapshot_enabled:
            if self._snap_timer is None:
                self._snap_timer = self.ctx.start_timer(SNAPSHOT_TICK_MS, self._snapshot_tick)
            else:
                self._snap_timer.start()
        elif self._snap_timer is not None:
            self._snap_timer.stop()
        if self.cfg.place_enabled:
            if not self._place_on:
                self.placer.reset()  # 有効にした時点で開いている窓は動かさない
            if self._place_timer is None:
                self._place_timer = self.ctx.start_timer(self.cfg.place_poll_ms, self._place_tick)
            else:
                self._place_timer.setInterval(self.cfg.place_poll_ms)
                if not self._place_on:
                    self._place_timer.start()
            self._place_on = True
        else:
            if self._place_timer is not None:
                self._place_timer.stop()
            self._place_on = False

    def snoozed(self) -> bool:
        """本体の一時停止(スヌーズ)中か。本体が API を持たない版でも動くよう、無ければ False。"""
        fn = getattr(self.ctx, "is_snoozed", None)
        if fn is None:
            return False
        try:
            return bool(fn())
        except Exception:  # noqa: BLE001 - 判定できなければ止めない側(従来どおり)に倒す
            return False

    def handle_cli(self, args: list[str]) -> tuple[int, str]:
        cmd = args[0] if args else ""
        preset = " ".join(args[1:]).strip() or None
        if self.disabled_reason:
            return 11, self.disabled_reason
        if cmd == "status":
            sr = self.current_signature()
            name = self.display_name(sr.signature) if sr.signature else "不明"
            n = len(self.preset_names(sr.signature)) if sr.signature else 0
            return 0, (f"mode={self.cfg.mode} sig={sr.signature} name={name} layout={self.store.has_layout(sr.signature)} "
                       f"presets={n} snoozed={self.snoozed()}")
        if cmd == "presets":
            sr = self.current_signature()
            names = self.preset_names(sr.signature) if sr.signature else []
            return 0, "\n".join(names) if names else "(プリセットなし)"
        if cmd == "save":
            return (0, self.last_result) if self.save("cli", preset) else (1, self.last_result)
        if cmd in ("apply", "plan"):
            r = self.apply_current("cli", preset=preset, force_dry=(cmd == "plan"))
            return (0 if r in ("applied", "dry_run", "nothing") else 1), self.last_result
        if cmd == "undo":
            r = self.undo("cli")
            return (0 if r == "applied" else 1), self.last_result
        if cmd == "snapshot":
            code = self.snapshot_now("cli")
            return (0 if code in ("saved", "unchanged") else 1), code
        return 2, "unsupported"

    def usage(self, days: int) -> list[UsageSeries]:
        """利用状況(MODULE_GUIDE §7)。oplog から日ごとの件数だけを返す。"""
        from .usage import usage_series

        return usage_series(self.store.oplog_path, days)

    def diagnostics(self) -> dict[str, str | int | bool]:
        """診断レポート用の要約(件数・モード・真偽・理由コードだけ。タイトル・exe 名・パス・シグネチャは入れない)。"""
        sums = self.store.list_layouts()
        cur = self.last_sig.signature if self.last_sig else None
        cur_sum = next((s for s in sums if s.signature == cur), None)
        return {
            "disabled": bool(self.disabled_reason),
            "mode": self.cfg.mode,
            "auto_apply": self.cfg.auto_apply,
            "targets": len(self.cfg.targets),
            "invalid_targets": len(self.cfg.invalid_targets),
            "signatures": sum(1 for s in sums if s.preset_count or s.broken),
            "presets": sum(s.preset_count for s in sums),
            "broken_layouts": sum(1 for s in sums if s.broken),
            "v1_layouts": sum(1 for s in sums if s.v1),
            "auto_snapshots": sum(len(s.auto_slots) for s in sums),
            "current_signature_known": cur is not None,
            "current_presets": cur_sum.preset_count if cur_sum else 0,
            "proposal_pending": bool(self.proposal_sig and self.proposal_sig == cur),
            "snoozed": self.snoozed(),
            "auto_snapshot": self.cfg.snapshot_enabled,
            "place_new": self.cfg.place_enabled,
            "place_new_mode": "live" if self.cfg.place_live else "dry_run",
            "place_pending": self.placer.pending_count,
            "last_apply": self.codes["apply"] or "none",
            "last_snapshot": self.codes["snapshot"] or "none",
            "last_place": self.codes["place"] or "none",
        }

    def create_page(self) -> Any:
        from .page import LayoutKeepPage

        return LayoutKeepPage(self)

    # ================================================================ トレイ・ホットキー
    def _build_tray(self) -> None:
        c = self.ctx
        self._tray["save"] = c.add_tray_action("現在の配置を保存", lambda: self.save("tray"))
        self._tray["apply"] = c.add_tray_action("配置を戻す", lambda: self.apply_current("tray"))
        self._tray["before"] = c.add_tray_action("抜く前の配置に戻す(自動保存)", lambda: self.restore_auto("tray"))
        self._tray["undo"] = c.add_tray_action("直前の適用を元に戻す", lambda: self.undo("tray"))
        c.add_tray_separator()
        self._tray["name"] = c.add_tray_action("この構成に名前を付ける", self.name_current_interactive)

    def _register_hotkeys(self) -> None:
        actions: dict[str, Callable[[], None]] = {
            "save": lambda: self._hotkey(lambda: self.save("hotkey")),
            "apply": lambda: self._hotkey(lambda: self.apply_current("hotkey")),
        }
        for name, fn in actions.items():
            text = self.cfg.hotkeys.get(name, "")
            if not text:
                continue
            ok = bool(self.ctx.hotkeys.register_text(name, text))
            self.hotkey_ok[name] = ok
            if ok:
                self.ctx.hotkeys.triggered(name).connect(fn)

    @staticmethod
    def _hotkey(fn: Callable[[], Any]) -> None:
        fn()

    # ================================================================ 状態
    def current_signature(self) -> SigResult:
        r = monitors.current(self.api, self.cfg.id_source, self.cfg.fields)
        self.last_sig = r
        return r

    def display_name(self, sig: str | None) -> str:
        if not sig:
            return "構成不明"
        return self.cfg.names.get(sig) or f"構成-{sig[:6]}"

    def preset_names(self, sig: str | None) -> list[str]:
        """その構成の利用者プリセット名(既定を先頭に)。壊れていれば空。"""
        if not sig:
            return []
        try:
            ps = self.store.load_set(sig)
        except BrokenLayoutError:
            return []
        if ps is None:
            return []
        d = ps.default()
        return [p.name for p in sorted(ps.presets, key=lambda p: p is not d)]

    def auto_restore_name(self, sig: str | None) -> str | None:
        """「抜く前の配置に戻す」で使う自動スナップショット(抜く前 → 無ければ最新)。無ければ None。"""
        if not sig:
            return None
        a = self.store.load_auto_quiet(sig)
        if a is None:
            return None
        if "before_change" in a.slots:
            return AUTO_BEFORE
        return AUTO_LATEST if "latest" in a.slots else None

    def refresh_status(self) -> None:
        if self.disabled_reason:
            return
        sr = self.current_signature()
        mode = "試運転" if not self.cfg.live else "本番"
        parts = [mode, self.display_name(sr.signature)]
        if sr.signature and not self.store.has_layout(sr.signature):
            parts.append("未保存")
        if self.proposal_sig and self.proposal_sig == sr.signature:
            parts.append("提案中")
        if self.snoozed():
            parts.append("一時停止中")
        self.ctx.set_tray_status(" · ".join(parts))
        if "apply" in self._tray:
            label = "配置を戻す"
            if self.proposal_sig and self.proposal_sig == sr.signature:
                label += "(提案中)"
            if not self.cfg.live:
                label += "  [試運転]"
            self._tray["apply"].set_text(label)
        if "before" in self._tray:
            self._tray["before"].set_enabled(self.auto_restore_name(sr.signature) is not None)
        if "undo" in self._tray:
            self._tray["undo"].set_enabled(self.store.read_undo() is not None)
        self._build_quick_actions(sr.signature)
        self.signals.changed.emit()

    def _build_quick_actions(self, sig: str | None) -> None:
        """トレイに無い操作だけをクイックアクションに出す(構成名・プリセットが変わるので毎回作り直す)。"""
        add = getattr(self.ctx, "add_quick_action", None)
        clear = getattr(self.ctx, "clear_quick_actions", None)
        if add is None or clear is None:
            return
        clear()
        names = self.preset_names(sig)
        if sig and names:
            add(f"{self.display_name(sig)} の計画を表示(試運転)", self.quick_plan,
                keywords="layoutkeep レイアウト 配置 計画 dry-run プレビュー", glyph=None)
            suffix = "" if self.cfg.live else "(試運転)"
            for n in names:
                add(f"{self.display_name(sig)}: プリセット「{n}」を適用{suffix}", _bind_apply(self, n),
                    keywords=f"layoutkeep レイアウト 配置 プリセット preset {n}")
        if self.cfg.live:
            add("LayoutKeep を試運転に切り替える", lambda: self.set_mode("dry_run"),
                keywords="layoutkeep 試運転 dry_run 安全")

    def quick_plan(self) -> None:
        """現在の構成の計画を作って通知で見せる(動かさない)。"""
        plan, text = self.preview_plan()
        self.ctx.notify("LayoutKeep(計画のみ)" if plan is not None else "LayoutKeep", text,
                        level="info" if plan is not None else "warn")

    def snapshot_windows(self) -> list[WindowInfo]:
        return windows.snapshot(self.api, self.cfg.targets, self.ctx.game_processes(), self.cfg.exclude_classes)

    def _suppressed(self, source: str, action: str) -> bool:
        """foreground がゲーム・全画面・昇格(不明含む)なら True(何もしないで oplog に残す)。"""
        fg = self.ctx.foreground()
        if not fg.unsafe_for_input():
            return False
        reason = fg.reason() or "unknown"
        sig = self.last_sig.signature if self.last_sig else None
        self.store.append_oplog({"action": "suppressed", "source": source, "sig": sig, "reason": reason, "result": action})
        self.log.info("抑止: %s(source=%s, 理由=%s)", action, source, reason)
        self.last_result = f"foreground が {reason} のため何もしませんでした"
        if source in ("tray", "gui", "cli", "quick"):
            self.ctx.notify("LayoutKeep: 何もしませんでした", self.last_result, level="warn")
        return True

    def _game_in_front(self) -> str | None:
        """読むだけの自動処理(スナップショット)を控える理由。ゲーム・全画面(不明含む)なら理由コード。"""
        fg = self.ctx.foreground()
        if fg.is_game:
            return "game"
        if fg.is_fullscreen is None:
            return "fullscreen_unknown"
        if fg.is_fullscreen:
            return "fullscreen"
        return None

    # ================================================================ 保存
    def _capture(self, sr: SigResult, prev: Layout | None) -> list[SavedEntry]:
        """今の対象ウィンドウ(除外を除く)の配置を保存エントリにする。"""
        wins = [w for w in self.snapshot_windows() if w.target_index is not None and w.excluded is None]
        entries: list[SavedEntry] = []
        for w in wins:
            p = w.raw.placement
            assert p is not None and w.exe
            t = self.cfg.targets[w.target_index] if w.target_index is not None else None
            rx = _inherit_regex(prev, w) or (t.title_regex if t else None)
            rect, snapped = windows.effective_normal_rect(w.raw, sr.monitors)
            assert rect is not None
            entries.append(SavedEntry(exe=w.exe, cls=w.cls, title_regex=rx, title_at_save=w.title, hwnd=w.hwnd, pid=w.pid,
                                      proc_start=w.raw.proc_start, show="maximized" if p.show == "maximized" else "normal",
                                      normal_rect=rect, screen_rect=w.raw.screen_rect,
                                      normal_rect_raw=p.normal_rect if snapped else None, snapped=snapped))
        return entries

    def _load_quiet(self, sig: str, preset: str | None = None) -> Layout | None:
        try:
            return self.store.load_layout(sig, preset)
        except BrokenLayoutError:
            return None

    def save(self, source: str, preset: str | None = None) -> bool:
        """現在の配置を保存する。preset=None なら既定のプリセット(無ければ「基本」を作る)、名前なら上書き or 新規。"""
        if self.disabled_reason:
            self.last_result = self.disabled_reason
            return False
        if preset is not None:
            err = validate_preset_name(preset)
            if err:
                self.last_result = err
                self.ctx.notify("LayoutKeep: 保存できません", err, level="error")
                return False
            preset = preset.strip()
        sr = self.current_signature()
        if sr.signature is None:
            self.last_result = f"構成シグネチャを計算できないため保存しません({sr.reason})"
            self.log.warning(self.last_result)
            self.ctx.notify("LayoutKeep: 保存できません", self.last_result, level="error")
            return False
        sig = sr.signature
        prev = (self._load_quiet(sig, preset) if preset else None) or self._load_quiet(sig)
        entries = self._capture(sr, prev)
        if not entries:
            self.last_result = "対象のウィンドウがありません(targets を確認)"
            self.ctx.notify("LayoutKeep: 保存しませんでした", self.last_result, level="warn")
            return False
        full, _ = monitors.normalize(sr.monitors, self.cfg.id_source, DEFAULT_FIELDS)
        layout = Layout(signature=sig, monitors=full or [], saved_at=now_iso(), windows=entries)
        try:
            broken_to = self.store.save_layout(layout, preset)
        except StoreWriteError as e:
            self.last_result = str(e)
            self.ctx.notify("LayoutKeep: 保存できません", self.last_result, level="error")
            return False
        if broken_to is not None:
            self.log.warning("壊れたレイアウトを %s に退避しました", broken_to.name)
            self.ctx.notify("LayoutKeep", f"壊れていた {sig}.json を {broken_to.name} に改名して残しました", level="warn")
        if sig not in self.cfg.names:  # FR-4: 名前が無ければ既定名
            self.update_settings(lambda s: s.setdefault("names", {}).__setitem__(sig, f"構成-{sig[:6]}"))
        names = self.preset_names(sig)
        saved_name = preset or (names[0] if names else "")
        self.store.append_oplog({"action": "save", "source": source, "sig": sig, "count": len(entries),
                                 "preset": saved_name})
        self.log.info("保存 sig=%s 件数=%d (%s)", sig, len(entries), ", ".join(sorted({e.exe_name for e in entries})))
        self.last_result = f"{len(entries)} 件のウィンドウ配置を保存しました({self.display_name(sig)} / {saved_name})"
        self.ctx.notify("LayoutKeep: 保存しました", self.last_result, level="ok")
        if self.proposal_sig == sig:
            self.proposal_sig = None
        self.refresh_status()
        return True

    # ================================================================ プリセットの管理(画面から)
    def rename_preset(self, sig: str, preset_id: str, name: str) -> str | None:
        err = validate_preset_name(name)
        if err:
            return err
        name = name.strip()
        try:
            ps = self.store.load_set(sig)
        except BrokenLayoutError as e:
            return str(e)
        p = ps.by_id(preset_id) if ps else None
        if ps is None or p is None:
            return "プリセットが見つかりません"
        if any(x.name == name and x.id != preset_id for x in ps.presets):
            return "同じ名前のプリセットが既にあります"
        p.name = name
        try:
            self.store.write_set(ps, keep_prev=False)
        except StoreWriteError as e:
            return str(e)
        self.store.append_oplog({"action": "preset", "source": "gui", "sig": sig, "result": "rename", "preset": name})
        self.refresh_status()
        return None

    def set_default_preset(self, sig: str, preset_id: str) -> str | None:
        try:
            ps = self.store.load_set(sig)
        except BrokenLayoutError as e:
            return str(e)
        p = ps.by_id(preset_id) if ps else None
        if ps is None or p is None:
            return "プリセットが見つかりません"
        ps.default_id = preset_id
        try:
            self.store.write_set(ps, keep_prev=False)
        except StoreWriteError as e:
            return str(e)
        self.store.append_oplog({"action": "preset", "source": "gui", "sig": sig, "result": "default", "preset": p.name})
        self.refresh_status()
        return None

    def delete_preset(self, sig: str, preset_id: str) -> str | None:
        """プリセットを外す(DeskKit 自身のデータ。ファイルは .deleted-<日時> として残す)。"""
        try:
            ps = self.store.load_set(sig)
            p = ps.by_id(preset_id) if ps else None
            if p is None:
                return "プリセットが見つかりません"
            dst = self.store.delete_preset(sig, preset_id)
        except (BrokenLayoutError, StoreWriteError, ValueError) as e:
            return str(e)
        self.log.info("プリセット %s/%s を外し、%s として残しました", sig, preset_id, dst.name)
        self.store.append_oplog({"action": "preset", "source": "gui", "sig": sig, "result": "delete", "preset": p.name})
        if self.proposal_sig == sig and not self.store.has_layout(sig):
            self.proposal_sig = None
        self.refresh_status()
        return None

    # ================================================================ 計画・適用
    def plan_for(self, sig: str, preset: str | None = None) -> tuple[Plan | None, str | None]:
        """(計画, エラー種別)。エラー種別は no_layout / broken:<説明>。preset=None なら既定のプリセット。"""
        try:
            layout = self.store.load_layout(sig, preset)
        except BrokenLayoutError as e:
            return None, f"broken:{e}"
        if layout is None:
            return None, "no_layout"
        cands = windows.target_windows(self.snapshot_windows())
        ms = matcher.match(layout.windows, cands)
        mons = self.last_sig.monitors if self.last_sig and self.last_sig.signature == sig else self.api.enum_monitors()
        plan = planner.make_plan(sig, ms, mons)
        self.last_plan = plan
        self.last_plan_preset = preset
        return plan, None

    def preview_plan(self, preset: str | None = None) -> tuple[Plan | None, str]:
        """画面の「計画を見る」。動かさずに計画だけ作る(oplog に plan 行を残す)。"""
        sr = self.current_signature()
        if sr.signature is None:
            return None, f"構成シグネチャを計算できません({sr.reason})"
        plan, err = self.plan_for(sr.signature, preset)
        if plan is None:
            return None, (f"この構成の{preset_label(preset)}はありません" if err == "no_layout"
                          else f"レイアウトが壊れています: {err}")
        self._log_plan(plan, "gui", preset)
        self.signals.changed.emit()
        return plan, planner.summary_text(plan, dry_run=True)

    def _log_plan(self, plan: Plan, source: str, preset: str | None = None) -> None:
        rec: dict[str, Any] = {"action": "plan", "source": source, "sig": plan.signature, "planned": len(plan.moves),
                               "skipped": plan.skipped(), "items": [i.log_dict() for i in plan.items]}
        if preset is not None:
            rec["preset"] = preset
        self.store.append_oplog(rec)
        for i in plan.items:
            self.log.info("plan %s %s/%s hwnd=%s %s %s → %s", i.action, i.exe_name, i.cls, i.hwnd, i.key,
                          i.before_rect, i.after_rect)

    def apply_current(self, source: str, *, preset: str | None = None, force_dry: bool = False) -> str:
        """トレイ・ホットキー・通知クリック・画面・CLI・クイックアクションの「配置を戻す」。戻り値は結果種別。
        手で押した操作なのでスヌーズ中でも動く(ゲーム・全画面の抑止は効く)。"""
        if self.disabled_reason:
            self.last_result = self.disabled_reason
            return "error"
        if self._suppressed(source, "apply"):
            self.codes["apply"] = "suppressed"
            return "suppressed"
        sr = self.current_signature()
        if sr.signature is None:
            self.last_result = f"構成シグネチャを計算できません({sr.reason})"
            self.ctx.notify("LayoutKeep", self.last_result, level="error")
            self.codes["apply"] = "no_signature"
            return "error"
        plan, err = self.plan_for(sr.signature, preset)
        if plan is None:
            if err == "no_layout":
                self.last_result = "この構成のレイアウトはありません" if preset is None else f"この構成の{preset_label(preset)}はありません"
                self.ctx.notify("LayoutKeep", self.last_result, level="info")
                self.codes["apply"] = "no_layout"
                return "no_layout"
            self.last_result = f"レイアウトを読めません: {err}"
            self.ctx.notify("LayoutKeep: レイアウトが壊れています", self.last_result, level="error")
            self.codes["apply"] = "broken"
            return "error"
        return self._execute(plan, source, dry=(force_dry or not self.cfg.live), preset=preset)

    def restore_auto(self, source: str) -> str:
        """「抜く前の配置に戻す」: 自動スナップショット(抜く前 → 無ければ最新)を適用する。"""
        if self.disabled_reason:
            self.last_result = self.disabled_reason
            return "error"
        sr = self.current_signature()
        name = self.auto_restore_name(sr.signature)
        if name is None:
            self.last_result = "この構成の自動保存はまだありません(構成が落ち着いてしばらくすると保存します)"
            self.ctx.notify("LayoutKeep", self.last_result, level="info")
            return "no_layout"
        return self.apply_current(source, preset=name)

    def _execute(self, plan: Plan, source: str, *, dry: bool, preset: str | None = None) -> str:
        if self.proposal_sig == plan.signature:
            self.proposal_sig = None
        if dry:
            self._log_plan(plan, source, preset)
            self.last_result = planner.summary_text(plan, dry_run=True)
            self.ctx.notify("LayoutKeep(試運転)", self.last_result, level="info")
            self.codes["apply"] = "dry_run"
            self.refresh_status()
            return "dry_run"
        res = self.applier.apply(plan, dry_run=False)
        return self._finish_apply(plan, res, source, preset=preset)

    def _finish_apply(self, plan: Plan, res: ApplyResult, source: str, request_id: str | None = None,
                      preset: str | None = None) -> str:
        rec: dict[str, Any] = {"action": "apply", "source": source, "sig": plan.signature, "moved": res.moved,
                               "skipped": res.skipped, "errors": res.errors, "focus_kept": res.focus_kept}
        if request_id is not None:
            rec["request_id"] = request_id
        if preset is not None:
            rec["preset"] = preset
        if res.status == "undo_failed":
            rec["result"] = "undo_failed"
            self.store.append_oplog(rec)
            self.last_result = f"undo.json を書けないため適用を中止しました(1件も動かしていません): {res.message}"
            self.log.error(self.last_result)
            self.ctx.notify("LayoutKeep: 適用を中止しました", self.last_result, level="error")
            self.codes["apply"] = "undo_failed"
            self.refresh_status()
            return "error"
        self.store.append_oplog(rec)
        for e in res.errors:
            self.log.warning("SetWindowPlacement 失敗 %s/%s hwnd=%s err=%s", e["exe"], e["class"], e["hwnd"], e["win32_error"])
        if res.focus_kept is False:
            self.log.warning("適用の前後で foreground ウィンドウが変わりました(FR-16)")
        self.last_result = planner.summary_text(plan, dry_run=False, moved=res.moved)
        if res.errors:
            self.last_result += f"\n{len(res.errors)} 件は動かせませんでした(権限など)"
        if res.status == "nothing" and not plan.moves:
            self.last_result = "動かすウィンドウはありません\n" + planner.summary_text(plan, dry_run=False, moved=0)
        self.ctx.notify("LayoutKeep", self.last_result, level="ok" if not res.errors else "warn")
        self.codes["apply"] = "applied" if res.status == "applied" else "nothing"
        self.refresh_status()
        return "applied" if res.status == "applied" else "nothing"

    # ================================================================ 取り消し
    def undo(self, source: str) -> str:
        if self.disabled_reason:
            self.last_result = self.disabled_reason
            return "error"
        if self._suppressed(source, "undo"):
            return "suppressed"
        sr = self.current_signature()
        res = self.applier.undo(sr.signature, dry_run=not self.cfg.live)
        rec: dict[str, Any] = {"action": "undo", "source": source, "sig": sr.signature, "result": res.status,
                               "moved": res.moved, "skipped": res.skipped, "errors": res.errors}
        if res.status == "nothing":
            self.last_result = res.message
            self.ctx.notify("LayoutKeep", self.last_result, level="info")
            return "nothing"
        self.store.append_oplog(rec)
        if res.status == "sig_mismatch":
            self.last_result = res.message
            self.ctx.notify("LayoutKeep: 元に戻せません", self.last_result, level="warn")
        elif res.status == "dry_run":
            self.last_result = res.message
            self.ctx.notify("LayoutKeep(試運転)", self.last_result, level="info")
        else:
            self.last_result = f"{res.moved} 件を元に戻しました"
            if res.skipped:
                self.last_result += f" / {sum(res.skipped.values())} 件は戻していません"
            if res.message:
                self.last_result += f"\n{res.message}"
            self.ctx.notify("LayoutKeep", self.last_result, level="ok" if not res.errors else "warn")
        self.refresh_status()
        return res.status

    # ================================================================ 構成変化(sysevents)
    def _sig_for_watch(self) -> str | None:
        return self.current_signature().signature

    def _on_kick(self, _why: str) -> None:
        # L2: 構成が動き出す前の最後の自動スナップショットを「抜く前」に写す(以後の自動保存で上書きされないように)
        if self._stable_sig is not None:
            try:
                if self.store.promote_auto(self._stable_sig):
                    self.log.info("自動スナップショットを「抜く前」に写しました sig=%s", self._stable_sig)
            except StoreWriteError as e:
                self.log.warning("自動スナップショットを「抜く前」に写せません: %s", e)
            self._stable_sig = None
        self.placer.reset()  # 構成変化で動いた窓・現れた窓を「新しい窓」と誤認しない
        # 構成が動き出したら古い提案は捨てる
        if self.proposal_sig is not None:
            self.proposal_sig = None
            self.refresh_status()

    def _on_settled(self, sig: str | None) -> None:
        if not self._running:
            return
        self._stable_sig = sig
        self._stable_since = time.monotonic()
        if sig is None:
            self.log.info("構成シグネチャを計算できないため提案しません(%s)", self.last_sig.reason if self.last_sig else "")
            self.refresh_status()
            return
        if not self.store.has_layout(sig):
            self.refresh_status()  # レイアウトの無い構成: 何も出さない
            return
        if self.snoozed():
            # スヌーズ中は自動の提案・自動適用をしない(手で「配置を戻す」は使える)
            self.store.append_oplog({"action": "suppressed", "source": "auto", "sig": sig, "reason": "snoozed",
                                     "result": "propose"})
            self.log.info("一時停止中のため構成変化後の提案をしません")
            self.refresh_status()
            return
        if self._suppressed("auto", "propose"):
            return  # 提案も保留せず捨てる(§10)
        plan, err = self.plan_for(sig)
        if plan is None:
            self.log.warning("構成変化後の計画を作れません: %s", err)
            if err and err.startswith("broken:"):
                self.ctx.notify("LayoutKeep: レイアウトが壊れています", err[7:], level="error")
            return
        if not plan.moves:
            self.refresh_status()  # FR-12: 動かすものが無ければ何も出さない
            return
        if self.cfg.auto_apply and self.cfg.live:
            res = self.applier.apply(plan, dry_run=False)
            self._finish_apply(plan, res, "auto")
            return
        self.proposal_sig = sig
        self.store.append_oplog({"action": "propose", "source": "auto", "sig": sig, "planned": len(plan.moves),
                                 "skipped": plan.skipped()})
        text = planner.summary_text(plan, dry_run=not self.cfg.live)
        self.ctx.notify(f"配置を戻しますか?({self.display_name(sig)})", text + "\nこの通知をクリックすると戻します",
                        on_click=lambda: self.apply_current("tray"), level="info")
        self.refresh_status()

    # ================================================================ L2: 自動スナップショット
    def _snapshot_tick(self) -> None:
        """1分ごとの時刻確認。構成が stable_min 分落ち着いたら1回、その後は interval_min 分ごとに保存する。"""
        if not self._running or self.disabled_reason or not self.cfg.snapshot_enabled:
            return
        sig = self._stable_sig
        if sig is None or self.watcher.pending:
            return
        now = time.monotonic()
        if now - self._stable_since < self.cfg.snapshot_stable_min * 60:
            return
        last = self._last_snap.get(sig)
        if last is not None and last >= self._stable_since and now - last < self.cfg.snapshot_interval_min * 60:
            return
        self.snapshot_now("auto")

    def snapshot_now(self, source: str) -> str:
        """自動スナップショットを1回取る。結果コード(saved / unchanged / no_windows / snoozed / game ...)を返す。
        書くのは <sig>.auto.json だけで、利用者のプリセットには触らない。"""
        code = self._snapshot(source)
        self.codes["snapshot"] = code
        sig = self.last_sig.signature if self.last_sig else None
        if code not in _SNAPSHOT_RETRY_CODES and sig:
            self._last_snap[sig] = time.monotonic()  # 控えた理由が一時的なものなら、次の確認でまた試す
        if code == "saved":
            self.refresh_status()
        return code

    def _snapshot(self, source: str) -> str:
        if self.disabled_reason:
            return "disabled"
        if source == "auto" and self.snoozed():
            return "snoozed"
        why = self._game_in_front()
        if why:
            return why
        sr = self.current_signature()
        if sr.signature is None:
            return "no_signature"
        if source == "auto" and sr.signature != self._stable_sig:
            return "sig_changed"
        sig = sr.signature
        entries = self._capture(sr, self._load_quiet(sig))
        if not entries:
            return "no_windows"
        prev = self.store.load_auto_slot(sig, "latest")
        if prev is not None and same_windows(prev.windows, entries):
            return "unchanged"  # 書き込みを減らす: 同じ配置なら書かない
        full, _ = monitors.normalize(sr.monitors, self.cfg.id_source, DEFAULT_FIELDS)
        saved_at = now_iso()
        try:
            self.store.write_auto_latest(Layout(signature=sig, monitors=full or [], saved_at=saved_at, windows=entries))
        except StoreWriteError as e:
            self.log.warning("自動スナップショットを書けません: %s", e)
            return "write_failed"
        self.last_snapshot_at = saved_at
        self.store.append_oplog({"action": "snapshot", "source": source, "sig": sig, "count": len(entries),
                                 "slot": "latest"})
        self.log.info("自動スナップショット sig=%s 件数=%d", sig, len(entries))
        return "saved"

    # ================================================================ L3: 新しいウィンドウの配置
    def _could_be_target(self, raw: RawWindow) -> bool:
        """exe とクラスだけで targets に当たり得るか(タイトルは後から変わるのでここでは見ない)。"""
        return any(t.matches_exe_class(raw.exe, raw.cls) for t in self.cfg.targets)

    def _place_tick(self) -> None:
        if not self._running or self.disabled_reason or not self.cfg.place_enabled:
            return
        if self.watcher.pending:
            self.placer.reset()  # 構成が変わっている最中は判定しない
            return
        if not self.placer.poll_new():
            return  # 新しい窓が無ければ EnumWindows 1回だけで終わる
        # 新しい窓がある: スヌーズ・ゲーム・全画面なら中身を読まずに扱い済みにする(後から動かさない)
        if self.snoozed():
            if self.placer.absorb():
                self.codes["place"] = "snoozed"
            return
        fg = self.ctx.foreground()
        if fg.is_game or fg.is_fullscreen is not False:
            if self.placer.absorb():
                self.codes["place"] = fg.reason() or "unsafe"
            return
        ready = self.placer.ready()
        if not ready:
            return
        sr = self.current_signature()
        for raw in ready:
            self.placer.mark_handled(raw.hwnd)
            ev = self._place_one(raw, sr)
            self.place_events.appendleft(ev)
            self.codes["place"] = ev.key if ev.action == "skip" else ev.action
            self.log.info("新規ウィンドウ %s %s/%s hwnd=%s %s", ev.action, ev.exe_name, ev.cls, ev.hwnd, ev.key)
        self.signals.changed.emit()

    def _place_one(self, raw: RawWindow, sr: SigResult) -> PlaceEvent:
        """新しい窓1枚を判定し、本番なら1回だけ SetWindowPlacement で動かす。"""
        ts = now_iso()
        if sr.signature is None:
            return event_for(raw, ts, "skip", "no_signature")
        sig = sr.signature
        elevated = self.api.is_elevated(raw.pid)
        if elevated is not False:  # 昇格・判定できない窓は動かさない(C-5)
            return event_for(raw, ts, "skip", "elevated" if elevated else "elevated_unknown")
        layout = self._load_quiet(sig)  # 既定のプリセット
        names = self.preset_names(sig)
        pname = names[0] if names else ""
        if layout is None:
            return event_for(raw, ts, "skip", "no_layout")
        cands = windows.target_windows(self.snapshot_windows())
        mine = next((w for w in cands if w.hwnd == raw.hwnd), None)
        if mine is None:
            return event_for(raw, ts, "skip", "not_target", preset=pname)
        if mine.excluded is not None:
            return event_for(raw, ts, "skip", f"excluded:{mine.excluded}", preset=pname)
        matches = matcher.match(layout.windows, cands)
        ms = [m for m in matches if m.window is not None and m.window.hwnd == raw.hwnd and m.status == "matched"]
        if not ms:
            # 同じアプリの窓が区別できない(M-5)・対応する保存エントリが無い → 動かさない(INV-2)
            amb = any(m.status == "ambiguous" and m.entry.group == mine.group for m in matches)
            return event_for(raw, ts, "skip", "ambiguous" if amb else "not_matched", preset=pname)
        plan = planner.make_plan(sig, ms, sr.monitors)
        item = plan.items[0]
        kw: dict[str, Any] = {"rule": item.rule, "preset": pname, "before": item.before_rect, "after": item.after_rect}
        if item.action != "move":
            return event_for(raw, ts, "skip", item.key, **kw)
        if not self.cfg.place_live:
            self.store.append_oplog({"action": "place", "source": "auto", "sig": sig, "result": "dry_run", "planned": 1,
                                     "preset": pname, "items": [item.log_dict()]})
            return event_for(raw, ts, "would_move", "move", **kw)
        prev_undo = self.store.read_undo()
        append = bool(prev_undo and prev_undo.get("tag") == "place" and prev_undo.get("signature") == sig)
        res = self.applier.apply(plan, dry_run=False, only=[item.index], append_undo=append, undo_tag="place")
        rec: dict[str, Any] = {"action": "place", "source": "auto", "sig": sig, "moved": res.moved, "skipped": res.skipped,
                               "errors": res.errors, "preset": pname, "items": [item.log_dict()]}
        if res.status == "undo_failed":
            rec["result"] = "undo_failed"
            self.store.append_oplog(rec)
            self.log.error("undo.json を書けないため新規ウィンドウを動かしませんでした: %s", res.message)
            return event_for(raw, ts, "skip", "undo_failed", **kw)
        rec["result"] = "applied" if res.moved else "set_failed"
        self.store.append_oplog(rec)
        if res.moved:
            return event_for(raw, ts, "moved", "move", **kw)
        return event_for(raw, ts, "skip", "set_failed", **kw)

    # ================================================================ layout.apply イベント(§9.5・契約 §2)
    def _reply(self, request_id: str, sig: str | None, result: str, moved: int = 0,
               skipped: Mapping[str, int] | None = None, *, preset: Any = None, **extra: Any) -> None:
        payload: dict[str, Any] = {"request_id": request_id, "sig": sig, "result": result, "moved": moved,
                                   "skipped": dict(skipped or {})}
        if preset is not None:
            payload["preset"] = preset  # 受けた preset はそのまま返す(契約 §2)
        payload.update(extra)
        self.ctx.emit("layout.applied", payload)

    def resolve_layout(self, want: Any) -> str | None:
        """名前 or シグネチャ → シグネチャ。見つからなければ None。"""
        if not isinstance(want, str) or not want:
            return None
        for sig, name in self.cfg.names.items():
            if name == want:
                return sig
        return want if is_signature(want) else None

    def _on_apply_event(self, payload: Mapping[str, Any]) -> None:
        req = str(payload.get("request_id") or "")
        raw_preset = payload.get("preset")
        echo = raw_preset if raw_preset not in (None, "") else None
        if self.disabled_reason:
            self._reply(req, None, "error", preset=echo, reason="disabled")
            return
        if self._wait is not None:
            self._reply(req, None, "error", preset=echo, reason="busy")  # 処理中の要求があれば後の要求はキューしない(§10)
            return
        sr = self.current_signature()
        cur = sr.signature
        want = payload.get("layout")
        if cur is None:
            self._reply(req, None, "error", preset=echo, reason="no_signature")
            return
        if want not in (None, ""):
            target = self.resolve_layout(want)
            if target is None:
                self._reply(req, cur, "no_layout", preset=echo)
                return
            if target != cur:  # INV-3: 名前指定でも構成が違えば適用しない
                self.store.append_oplog({"action": "apply", "source": "event", "sig": cur, "result": "sig_mismatch",
                                         "request_id": req})
                self._reply(req, cur, "sig_mismatch", preset=echo)
                return
        preset: str | None = None
        if echo is not None:
            if not isinstance(echo, str) or not echo.strip():
                self._reply(req, cur, "no_layout", preset=echo)
                return
            preset = echo.strip()
        # 現在の構成シグネチャのプリセット名で探す。無ければ no_layout(契約 §2)
        try:
            found = self.store.load_layout(cur, preset)
        except BrokenLayoutError:
            self._reply(req, cur, "error", preset=echo, reason="broken")
            return
        if found is None:
            self._reply(req, cur, "no_layout", preset=echo)
            return
        if self._suppressed("event", "apply"):
            self._reply(req, cur, "suppressed", preset=echo)
            return
        plan, err = self.plan_for(cur, preset)
        if plan is None:
            self._reply(req, cur, "error", preset=echo, reason=err or "error")
            return
        if not self.cfg.live:
            self._log_plan(plan, "event", preset)
            self._reply(req, cur, "dry_run", 0, plan.skipped(), preset=echo, planned=len(plan.moves))
            return
        res = self.applier.apply(plan, dry_run=False)
        if res.status == "undo_failed":
            self._finish_apply(plan, res, "event", req, preset=preset)
            self._reply(req, cur, "error", 0, plan.skipped(), preset=echo, reason="undo_failed")
            return
        outcome: dict[int, str] = {}
        for it in plan.items:
            if it.action == "move":
                outcome[it.index] = "moved" if it.index in res.moved_indexes else "set_failed"
            else:
                outcome[it.index] = it.key
        pending = {i for i, k in outcome.items() if k == "not_running"}
        try:
            wait_s = float(payload.get("wait_s") or 0)
        except (TypeError, ValueError):
            wait_s = 0.0
        wait_s = max(0.0, min(wait_s, float(self.cfg.max_wait_s)))
        st = _WaitState(req, cur, time.monotonic() + wait_s, pending, outcome, list(res.errors), preset=echo)
        if wait_s > 0 and pending:
            # FR-15: 起動していないエントリだけ、wait_s の間(上限 max_wait_s)再列挙して再試行する
            self._wait = st
            st.timer = self.ctx.start_timer(WAIT_INTERVAL_MS, self._wait_tick)
            self.log.info("layout.apply: %d 件の起動を最大 %.0f 秒待ちます", len(pending), wait_s)
            return
        self._finish_wait(st, plan)

    @staticmethod
    def _wait_preset(st: _WaitState) -> str | None:
        return st.preset.strip() if isinstance(st.preset, str) and st.preset.strip() else None

    def _wait_tick(self) -> None:
        st = self._wait
        if st is None:
            return
        done = time.monotonic() >= st.deadline or not self._running
        cur = self.current_signature().signature
        if cur != st.sig:
            done = True
        plan: Plan | None = None
        if not done and not self.ctx.foreground().unsafe_for_input():
            plan, _err = self.plan_for(st.sig, self._wait_preset(st))
            if plan is not None:
                st.retries += 1
                ready = [it for it in plan.items if it.index in st.pending and it.key != "not_running"]
                moves = [it.index for it in ready if it.action == "move"]
                res = self.applier.apply(plan, dry_run=False, append_undo=True, only=moves) if moves else None
                if res is not None and res.status == "undo_failed":
                    self.log.error("wait_s 再試行中に undo.json を書けないため中止しました")
                    done = True
                else:
                    for it in ready:
                        if it.action == "move":
                            st.outcome[it.index] = "moved" if res and it.index in res.moved_indexes else "set_failed"
                        else:
                            st.outcome[it.index] = it.key
                        st.pending.discard(it.index)
                    if res is not None:
                        st.errors.extend(res.errors)
        if not st.pending:
            done = True
        if done:
            if st.timer is not None:
                st.timer.stop()
            self._wait = None
            self._finish_wait(st, plan or self.last_plan)

    def _finish_wait(self, st: _WaitState, plan: Plan | None) -> None:
        moved = sum(1 for k in st.outcome.values() if k == "moved")
        skipped = dict(sorted(Counter(k for k in st.outcome.values() if k not in ("moved", "move")).items()))
        rec: dict[str, Any] = {"action": "apply", "source": "event", "sig": st.sig, "moved": moved, "skipped": skipped,
                               "errors": st.errors, "request_id": st.request_id, "retry": st.retries}
        wp = self._wait_preset(st)
        if wp is not None:
            rec["preset"] = wp
        self.store.append_oplog(rec)
        text = f"{moved} 件戻しました"
        other = sum(v for k, v in skipped.items() if k != "unchanged")
        if other:
            text += f" / {other} 件は動かしていません"
        if plan is not None:
            for exe, _cls, k in plan.ambiguous_groups():
                text += f"\n{exe.rsplit('.', 1)[0]} のウィンドウ {k} 枚は区別できず動かしていません"
        self.last_result = text
        self.codes["apply"] = "applied"
        self.ctx.notify("LayoutKeep", text, level="ok" if not st.errors else "warn")
        self._reply(st.request_id, st.sig, "applied", moved, skipped, preset=st.preset)
        self.refresh_status()

    # ================================================================ 設定の変更(画面から)
    def update_settings(self, mutate: Callable[[dict[str, Any]], Any], *, restart: bool = False) -> str | None:
        """セクションを書き換えて保存する。失敗時はエラー文を返す。"""
        sec, _ = config.fill_defaults(self.ctx.settings_dict())
        sec.pop("enabled", None)
        mutate(sec)
        try:
            new_cfg = config.parse(sec)
        except ValueError as e:
            return str(e)
        try:
            self.ctx.write_settings(sec, restart=restart)
        except Exception as e:  # noqa: BLE001 - SettingsError など。画面に出す
            return str(e)
        self.cfg = new_cfg
        if not restart:
            self._sync_timers()
            self.refresh_status()
        return None

    def set_mode(self, mode: str) -> str | None:
        return self.update_settings(lambda s: s.__setitem__("mode", mode))

    def set_nested(self, section: str, key: str, value: Any) -> str | None:
        """auto_snapshot / place_new の1項目を変える。"""
        def mut(s: dict[str, Any]) -> None:
            d = s.get(section)
            if not isinstance(d, dict):
                d = dict(config.DEFAULTS[section])
                s[section] = d
            d[key] = value

        return self.update_settings(mut)

    def rename(self, sig: str, name: str) -> str | None:
        name = name.strip()
        if not name:
            return "名前が空です"
        if any(n == name and s != sig for s, n in self.cfg.names.items()):
            return "同じ名前の構成が既にあります"
        return self.update_settings(lambda s: s.setdefault("names", {}).__setitem__(sig, name))

    def name_current_interactive(self) -> None:
        from deskkit.ui import widgets

        sr = self.current_signature()
        if sr.signature is None:
            self.ctx.notify("LayoutKeep", f"構成シグネチャを計算できません({sr.reason})", level="error")
            return
        v = widgets.text_input(self.ctx.window_parent(), "この構成に名前を付ける",
                               f"現在の構成({sr.signature})の名前。layout.apply でもこの名前で指定できます。",
                               self.display_name(sr.signature))
        if v is None:
            return
        err = self.rename(sr.signature, v)
        if err:
            self.ctx.notify("LayoutKeep", err, level="error")

    def delete_layout(self, sig: str) -> str | None:
        try:
            dst = self.store.delete_layout(sig)
        except (StoreWriteError, ValueError) as e:
            return str(e)
        self.log.info("レイアウト %s を %s に改名しました(削除操作)", sig, dst.name)
        if self.proposal_sig == sig:
            self.proposal_sig = None
        self.refresh_status()
        return None

    def set_entry_regex(self, sig: str, index: int, regex: str | None, preset: str | None = None) -> str | None:
        """保存済みエントリの title_regex を手直しする(M-4 で区別できるようにする)。preset=None なら既定。"""
        if regex:
            try:
                re.compile(regex)
            except re.error as e:
                return f"正規表現を解釈できません({e.msg})"
        try:
            lay = self.store.load_layout(sig, preset)
        except BrokenLayoutError as e:
            return str(e)
        if lay is None or not (0 <= index < len(lay.windows)):
            return "レイアウトが見つかりません"
        lay.windows[index].title_regex = regex or None
        try:
            self.store.write_layout_inplace(lay, preset)
        except StoreWriteError as e:
            return str(e)
        self.signals.changed.emit()
        return None


def _bind_apply(mod: LayoutKeepModule, preset: str) -> Callable[[], Any]:
    return lambda: mod.apply_current("quick", preset=preset)


def _inherit_regex(prev: Layout | None, w: WindowInfo) -> str | None:
    """既存レイアウトの同じ exe+クラスのエントリの title_regex を、今のタイトルに1つだけ当たるなら引き継ぐ(§9.2)。"""
    if prev is None:
        return None
    hits: list[str] = []
    for e in prev.windows:
        if e.group != w.group or not e.title_regex:
            continue
        try:
            if re.search(e.title_regex, w.title):
                hits.append(e.title_regex)
        except re.error:
            continue
    uniq = sorted(set(hits))
    return uniq[0] if len(uniq) == 1 else None
