# host に渡す Module 本体: 設定の既定値補完、トレイ「モード」サブメニュー(FR-2)、ホットキー(FR-3)、CLI(FR-4)、
# プレビュー窓の表示と進捗の受け渡し、FR-14 の確認ダイアログ、自動切替の開始/停止、Control Center 画面の生成。
# 実処理は service.ModeShiftService に集約し、ここは Qt と host の窓口だけを持つ。
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from deskkit.modules.modeshift import cli
from deskkit.modules.modeshift.autoswitch import AutoSwitcher
from deskkit.modules.modeshift.config import AutoRule, fill_defaults
from deskkit.modules.modeshift.model import Plan, Step
from deskkit.modules.modeshift.service import ModeShiftService
from deskkit.modules.modeshift.system import Backends, real_backends

if TYPE_CHECKING:
    from deskkit.usage import UsageSeries

log = logging.getLogger("deskkit.modeshift")
SUBMENU = "モード"


class ModeShiftModule:
    def __init__(self, ctx: Any, backends: Backends | None = None) -> None:
        self.ctx = ctx
        self._backends = backends
        sec, changed = fill_defaults(ctx.settings_dict())
        if changed:
            try:
                ctx.write_settings(sec)
            except Exception:  # noqa: BLE001 - 書けなくても既定値で動く
                log.exception("既定値を書き戻せません")
        self.service: ModeShiftService | None = None
        self._tray_modes: dict[str, Any] = {}
        self._undo_item: Any = None
        self._hotkeys: list[str] = []
        self._auto: AutoSwitcher | None = None
        self._preview: Any = None
        self._picker: Any = None

    # ---------------------------------------------------------------- ライフサイクル
    def start(self) -> None:
        self.service = ModeShiftService(self.ctx, self._backends or real_backends(), ui=self)
        self.service.add_listener(self._refresh_tray)
        self._apply_config(initial=True)
        cfg = self.service.config
        bad = cfg.invalid_modes()
        if bad or cfg.issues:
            lines = [f"「{m.label}」: {m.reason()}" for m in bad] + cfg.issues
            self.ctx.notify("ModeShift: 無効な設定があります", "\n".join(lines[:6]), level="warn")

    def stop(self) -> None:
        if self._auto is not None:
            self._auto.stop()
            self._auto = None
        if self.service is not None:
            self.service.shutdown()
        for w in (self._preview, self._picker):
            try:
                if w is not None:
                    w.close()
            except RuntimeError:
                pass
        self._preview = self._picker = None

    def handle_cli(self, args: list[str]) -> tuple[int, str]:
        return cli.handle(self.service, args)

    def usage(self, days: int) -> list[UsageSeries]:
        """利用状況(§7): 日ごとのモード切替回数(代表)・元に戻す回数・うまくいかなかった切替の回数。"""
        from deskkit.modules.modeshift.stats import usage_series

        return usage_series(self.ctx.data_dir / "ops.jsonl", days)

    def create_page(self) -> Any:
        from deskkit.modules.modeshift.page import ModeShiftPage

        return ModeShiftPage(self)

    # ---------------------------------------------------------------- 入口
    def switch(self, name: str, *, dry_run: bool = False, source: str = "gui") -> tuple[int, str]:
        if self.service is None:
            return 1, "停止中"
        return self.service.switch_mode(name, dry_run=dry_run, source=source)

    def undo(self, *, dry_run: bool = False, source: str = "gui") -> tuple[int, str]:
        if self.service is None:
            return 1, "停止中"
        code, text = self.service.request_undo(dry_run=dry_run, source=source)
        if code == 7:
            self.ctx.notify("ModeShift", text, level="info")
        return code, text

    # ---------------------------------------------------------------- 設定
    def save_section(self, section: dict[str, Any]) -> str | None:
        """設定を保存して反映する。戻り値はエラー文(成功なら None)。定義が変わったモードは自動で未確認に戻る。"""
        try:
            self.ctx.write_settings(section)
        except Exception as e:  # noqa: BLE001
            log.exception("設定を保存できません")
            return f"保存できませんでした: {e}"
        if self.service is not None:
            self.service.reload()
        self._apply_config()
        return None

    def _apply_config(self, *, initial: bool = False) -> None:
        svc = self.service
        if svc is None:
            return
        cfg = svc.config
        for n in self._hotkeys:
            try:
                self.ctx.hotkeys.unregister(n)
            except Exception:  # noqa: BLE001
                log.exception("ホットキーを解除できません %s", n)
        self._hotkeys = []
        failed: list[str] = []
        for m in cfg.valid_modes():
            if not m.hotkey:
                continue
            name = f"mode.{m.name}"
            if self.ctx.hotkeys.register_text(name, m.hotkey):
                self.ctx.hotkeys.triggered(name).connect(lambda n=m.name: self.switch(n, source="hotkey"))
                self._hotkeys.append(name)
            else:
                failed.append(f"{m.label}: {m.hotkey}")
        if cfg.undo_hotkey:
            if self.ctx.hotkeys.register_text("undo", cfg.undo_hotkey):
                self.ctx.hotkeys.triggered("undo").connect(lambda: self.undo(source="hotkey"))
                self._hotkeys.append("undo")
            else:
                failed.append(f"元に戻す: {cfg.undo_hotkey}")
        if failed and not initial:   # 起動時の失敗は host がまとめて通知する(FR-9)。別の組み合わせへは振り替えない
            self.ctx.notify("ModeShift: ホットキーを登録できません", "\n".join(failed), level="warn")
        self._build_tray()
        self._build_quick()
        self._restart_auto()

    # ---------------------------------------------------------------- トレイ(FR-2)
    def _build_tray(self) -> None:
        svc = self.service
        if svc is None:
            return
        self.ctx.clear_tray_actions(SUBMENU)
        self._tray_modes = {}
        cur = svc.state.data.get("current_mode")
        for m in svc.config.valid_modes():
            label = m.label + ("" if m.confirmed else "(未確認)")
            self._tray_modes[m.name] = self.ctx.add_tray_action(
                label, lambda n=m.name: self.switch(n, source="tray"), checkable=True, checked=(cur == m.name), submenu=SUBMENU)
        self.ctx.add_tray_separator(SUBMENU)
        self.ctx.add_tray_action("プレビュー…", self.open_picker, submenu=SUBMENU)
        self._undo_item = self.ctx.add_tray_action("元に戻す", lambda: self.undo(source="tray"), submenu=SUBMENU)
        self._refresh_tray()

    # ---------------------------------------------------------------- クイックアクション(トレイに無いものだけ)
    def _build_quick(self) -> None:
        from deskkit.ui.theme import G

        svc = self.service
        if svc is None:
            return
        self.ctx.clear_quick_actions()

        def idle() -> bool:
            return not svc.busy

        for m in svc.config.valid_modes():
            self.ctx.add_quick_action(
                f"{m.label} をプレビュー", lambda n=m.name: self.switch(n, dry_run=True, source="gui"),
                keywords=f"{m.name} {m.label} モード プレビュー 確認 dry-run modeshift", glyph=G.EYE, enabled=idle)

        def can_undo() -> bool:
            return not svc.busy and svc.undo_info() is not None

        self.ctx.add_quick_action("元に戻す内容を確かめる", lambda: self.undo(dry_run=True, source="gui"),
                                  keywords="元に戻す undo プレビュー 電源 音量 modeshift", glyph=G.UNDO, enabled=can_undo)

    def _refresh_tray(self) -> None:
        svc = self.service
        if svc is None:
            return
        cur = svc.state.data.get("current_mode")
        for name, item in self._tray_modes.items():
            item.set_checked(name == cur)
            item.set_enabled(not svc.busy)
        snap = svc.undo_info()
        if self._undo_item is not None:
            self._undo_item.set_text(f"元に戻す(「{snap.get('label_to')}」適用前へ)" if snap else "元に戻す(記録なし)")
            self._undo_item.set_enabled(snap is not None and not svc.busy)
        m = svc.current_mode()
        status = f"現在: {m.label}" if m else "モード未適用"
        if svc.busy:
            status += "(切替中…)"
        self.ctx.set_tray_status(status)

    def open_picker(self) -> None:
        from deskkit.modules.modeshift.preview import ModePicker

        if self.service is not None and self.service.in_game():
            self.ctx.notify("ModeShift", "ゲーム中のためプレビューを表示しませんでした", level="warn")
            return
        if self._picker is not None:
            try:
                self._picker.close()
            except RuntimeError:
                pass
        self._picker = ModePicker(self)
        self._picker.destroyed.connect(lambda *_: setattr(self, "_picker", None))
        self._picker.show_animated()

    # ---------------------------------------------------------------- UiHooks(service から呼ばれる)
    def show_preview(self, plan: Plan) -> None:
        from deskkit.modules.modeshift.preview import PreviewWindow
        from deskkit.modules.modeshift.visuals import mode_accent

        svc = self.service
        if svc is None:
            return
        if self._preview is not None:
            try:
                self._preview.close()
            except RuntimeError:
                pass
        m = svc.config.mode(plan.mode)
        accent = mode_accent(m, m.index) if m else mode_accent(None, 0)

        def closed(w: Any) -> None:
            if self._preview is w:
                self._preview = None

        self._preview = PreviewWindow(plan, accent=accent, on_execute=svc.execute_from_preview, on_closed=closed)
        self._preview.show_animated()

    def plan_progress(self, plan: Plan, step: Step) -> None:
        w = self._preview
        if w is not None and w.plan is plan:
            try:
                w.step_updated(step)
            except RuntimeError:
                self._preview = None

    def plan_finished(self, plan: Plan) -> None:
        w = self._preview
        if w is not None and w.plan is plan:
            try:
                w.finished()
            except RuntimeError:
                self._preview = None

    def confirm_force(self, exe: str, pid: int) -> bool:
        """FR-14: 作業スレッドから呼ばれる。メインスレッドで確認ダイアログを出し、承認されたときだけ True。"""
        done = threading.Event()
        box = {"ok": False}

        def ask() -> None:
            try:
                from deskkit.ui import widgets as W

                ok, _ = W.confirm(
                    self.ctx.window_parent(), "強制終了しますか?",
                    f"{exe}(PID {pid})が時間内に終了しませんでした。\n\n強制終了すると、このアプリの未保存のデータは失われます。"
                    "\n保存の確認が出ていないか、先にアプリを見てください。",
                    ok_text="強制終了する", cancel_text="終了させない", danger=True)
                box["ok"] = bool(ok)
            finally:
                done.set()

        self.ctx.call_soon(ask)
        svc = self.service
        for _ in range(3000):   # 最長 10 分待つ。停止要求が来たら「しない」
            if done.wait(0.2):
                return box["ok"]
            if svc is None or svc.aborting:
                return False
        return False

    # ---------------------------------------------------------------- 自動切替(FR-23)
    def _restart_auto(self) -> None:
        if self._auto is not None:
            self._auto.stop()
            self._auto = None
        svc = self.service
        if svc is None or not svc.config.auto_active:
            return
        procs = svc.backends.processes

        def names() -> set[str]:
            return {p.exe for p in procs.list_processes()}

        def appear(rule: AutoRule) -> None:
            svc.switch_mode(rule.mode, dry_run=False, source="auto")

        def vanish(rule: AutoRule) -> None:
            snap = svc.undo_info()
            if snap is not None and snap.get("mode_to") == rule.mode:
                svc.request_undo(dry_run=False, source="auto")
            else:
                log.info("自動切替: 直前の切替が %s ではないため元に戻さない", rule.mode)

        self._auto = AutoSwitcher(names, float(svc.config.poll_interval_s or 0), svc.config.rules, appear, vanish,
                                  self.ctx.call_soon)
        self._auto.start()
