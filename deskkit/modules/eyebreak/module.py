# EyeBreak モジュール本体。5 秒ごとのタイマーで入力なしの秒数を読み、prompts.Engine の判定を通知・記録・トレイに移す。
# ModeShift のイベント・一時停止・スリープからの復帰を受け、「10分あとで」「今日はもう出さない」「再開する」を
# 窓・トレイ・クイックアクション・CLI から受ける。ログ・daily.json・state.json・usage・diagnostics には回数と理由コードだけ(INV-4)。
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Signal

from deskkit.modules.eyebreak import _win32
from deskkit.modules.eyebreak.config import Config, parse
from deskkit.modules.eyebreak.prompts import BODY, EYE, Action, Engine, Prompt, fmt_minutes
from deskkit.modules.eyebreak.store import Daily, State
from deskkit.usage import UsageSeries

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

TICK_MS = 5000          # E-1: 5 秒ごとに読む
STATUS_MS = 60_000      # FR-17: トレイの状態は 1 分ごと
WM_POWERBROADCAST = 0x0218
REPLACE_KEY = "eyebreak.prompt"
QUICK_MUTE = "休憩の声かけ: 今日はもう出さない"
QUICK_RESUME = "休憩の声かけ: 再開する"


class Notifier(QObject):
    changed = Signal()


def _default_api() -> _win32.Api:
    return _win32.RealApi()


def _hour_text(h: int) -> str:
    return f"朝{h}時" if 4 <= h < 12 else f"{h}時"


class EyeBreakModule:
    def __init__(self, ctx: Any, *, api: _win32.Api | None = None, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], datetime] = datetime.now) -> None:
        self.ctx = ctx
        self.log: logging.Logger = ctx.log
        sec, cfg, filled = parse(dict(ctx.settings_dict()))  # 誤りは例外 → host がこのモジュールだけ停止中にする
        if filled:
            try:
                ctx.write_settings(sec)
            except Exception as e:  # noqa: BLE001 - 書き戻せなくても既定値で動く
                self.log.warning("settings write-back failed: %s", type(e).__name__)
        self.section = sec
        self.cfg: Config = cfg
        self.api: _win32.Api = api or _default_api()
        self._clock = clock
        self._wall = wall
        self.engine = Engine(cfg)
        self.daily = Daily(ctx.data_dir / "daily.json", self.log)
        self.state = State(ctx.data_dir / "state.json", self.log)
        self.daily.load(wall().date())
        self.engine.muted_until = self.state.load()
        self.notifier = Notifier()
        self.last_idle_s: float | None = None
        self._replace_ok = True        # 本体が replace_key を受け付けるか(TypeError を1回見たら False)
        self._logged_unavailable = False
        self._dialog: Any = None
        self._tray: dict[str, Any] = {}
        self._running = False
        self._mode: str | None = None  # E-9: 起動時は「静かにするモードではない」

    # ================================================================ ライフサイクル
    def start(self) -> None:
        self._running = True
        ctx = self.ctx
        on = getattr(ctx, "on", None)
        if on is not None:
            on("modeshift.switched", self._on_mode_switched)
            on("modeshift.reverted", self._on_mode_reverted)
            on("host.snooze_changed", self._on_snooze_changed)
        on_native = getattr(ctx, "on_native", None)
        if on_native is not None:
            on_native(WM_POWERBROADCAST, self._on_power)
        add = getattr(ctx, "add_tray_action", None)
        if add is not None:
            self._tray["later"] = add(f"{self.cfg.later_min}分あとで", lambda: self.later(None, None))
            self._tray["mute"] = add("今日はもう出さない", self.mute_today)
            self._tray["resume"] = add("再開する", self.resume)
        addq = getattr(ctx, "add_quick_action", None)
        if addq is not None:
            addq(QUICK_MUTE, self.mute_today, keywords="eyebreak 休憩 目 声かけ 止める ミュート",
                 enabled=lambda: not self.muted())
            addq(QUICK_RESUME, self.resume, keywords="eyebreak 休憩 目 声かけ 再開",
                 enabled=self.muted)
        try:
            self.engine.snoozed = bool(ctx.is_snoozed())
        except Exception:  # noqa: BLE001
            self.engine.snoozed = False
        self.engine.tracker.last = self._clock()
        ctx.start_timer(TICK_MS, self.tick)
        ctx.start_timer(STATUS_MS, self.update_status)
        self.update_status()
        self.log.info("eyebreak start mode=%s game=%s", self.cfg.mode, self.cfg.game)

    def stop(self) -> None:
        self._running = False
        self.daily.save()
        d = self._dialog
        self._dialog = None
        if d is not None:
            try:
                d.close()
            except RuntimeError:
                pass
        self.log.info("eyebreak stop")

    def handle_cli(self, args: list[str]) -> tuple[int, str]:
        cmd = args[0] if args else ""
        if cmd == "status" and len(args) == 1:
            if self.engine.unavailable:
                return 20, self.status_text()
            return 0, self.status_text()
        if cmd == "later" and len(args) in (1, 2):
            minutes = self.cfg.later_min
            if len(args) == 2:
                try:
                    minutes = int(args[1])
                except ValueError:
                    return 1, "分は 1〜60 の整数で指定してください"
                if not 1 <= minutes <= 60:
                    return 1, "分は 1〜60 の整数で指定してください"
            self.later(None, minutes)
            return 0, f"{minutes}分あとに、もう一度声をかけます"
        if cmd == "mute-today" and len(args) == 1:
            until = self.mute_today()
            return 0, f"{self.until_text(until)}まで出しません"
        if cmd == "resume" and len(args) == 1:
            self.resume()
            return 0, "再開しました。0 から数え直します"
        return 1, "使い方: eyebreak status | later [分] | mute-today | resume"

    def create_page(self) -> QWidget:
        from deskkit.modules.eyebreak.page import EyeBreakPage

        return EyeBreakPage(self)

    # ================================================================ 5 秒ごと
    def _read_idle(self) -> float | None:
        try:
            return _win32.read_idle_seconds(self.api)
        except OSError:
            return None

    def _fg(self) -> tuple[bool, bool | None]:
        info = self.ctx.foreground()
        return bool(info.is_game), info.is_fullscreen

    def snoozed_now(self) -> bool:
        try:
            return bool(self.ctx.is_snoozed())
        except Exception:  # noqa: BLE001
            return False

    def tick(self) -> None:
        if not self._running:
            return
        idle = self._read_idle()
        self.last_idle_s = idle
        acts = self.engine.tick(self._clock(), self._wall(), idle, self._fg, self.snoozed_now())
        for a in acts:
            self._handle(a)
        self.notifier.changed.emit()

    def _handle(self, a: Action) -> None:
        today = self._wall().date()
        if a.type == "show" and a.prompt is not None:
            self._notify(a.prompt)
            self.daily.add(today, a.kind)
            self.log.info("prompt kind=%s result=shown", a.kind)
        elif a.type == "dry":
            self.daily.add(today, "dry")
            self.log.info("prompt kind=%s result=dry", a.kind)
        elif a.type in ("held", "muted"):
            self.log.info("prompt kind=%s result=%s", a.kind, a.type)
        elif a.type == "rested":
            self.daily.add(today, "rested")
            self.log.info("rested kind=%s", a.kind)
        elif a.type == "break":
            self.log.info("break")
        elif a.type == "idle_unavailable":
            if not self._logged_unavailable:
                self._logged_unavailable = True
                self.log.warning("idle_unavailable")
            self.update_status()
        elif a.type == "unmuted":
            self.state.save(None)
            self.update_status()

    def _notify(self, p: Prompt) -> None:
        """声かけを出す。本体が replace_key を受け付けなければ、1回だけ付けずに呼び直し、以後は付けない(契約 §2.2)。"""
        on_click = self._click_handler(p.kind)
        if self._replace_ok:
            try:
                self.ctx.notify(p.title, p.text, on_click, level="info", replace_key=REPLACE_KEY)
                return
            except TypeError:
                self._replace_ok = False
                self.log.info("notify replace_key unsupported")
        self.ctx.notify(p.title, p.text, on_click, level="info")

    def _click_handler(self, kind: str) -> Callable[[], None]:
        return lambda: self.open_dialog(kind)

    # ================================================================ イベント
    def _quiet_names(self) -> set[str]:
        """quiet_modes のうち、今 ModeShift にあるモード名だけ(§9: 今は無いモード名は無視する)。"""
        try:
            names = {n for n, _label in self.ctx.list_modes()}
        except Exception:  # noqa: BLE001
            names = set()
        return {m for m in self.cfg.quiet_modes if m in names}

    def _on_mode_switched(self, payload: Mapping[str, Any]) -> None:
        mode = payload.get("mode")
        self._mode = mode if isinstance(mode, str) else None  # メモリにだけ持つ(INV-4: 書かない)
        self.engine.set_quiet(self._mode is not None and self._mode in self._quiet_names())
        self.update_status()

    def _on_mode_reverted(self, _payload: Mapping[str, Any]) -> None:
        self._mode = None
        self.engine.set_quiet(False)
        self.update_status()

    def _on_snooze_changed(self, _payload: Mapping[str, Any]) -> None:
        self.engine.set_snoozed(self.snoozed_now())
        self.update_status()
        self.notifier.changed.emit()

    def _on_power(self, _wparam: int, _lparam: int) -> None:
        self.engine.force_break()  # FR-14: wparam の値は見ない
        self.log.info("break")
        self.notifier.changed.emit()

    # ================================================================ 操作
    def muted(self) -> bool:
        return self.engine.is_muted(self._wall())

    def later(self, kind: str | None, minutes: int | None) -> None:
        m = int(minutes or self.cfg.later_min)
        self.engine.later(kind, m, self._clock())
        self.daily.add(self._wall().date(), "later")
        self.update_status()
        self.notifier.changed.emit()

    def mute_today(self) -> datetime:
        until = self.engine.mute_today(self._wall())
        self.state.save(until)
        self.daily.add(self._wall().date(), "mute_today")
        self.log.info("mute")
        self.update_status()
        self.notifier.changed.emit()
        return until

    def resume(self) -> None:
        self.engine.resume()
        self.state.save(None)
        self.log.info("resume")
        self.update_status()
        self.notifier.changed.emit()

    def until_text(self, until: datetime) -> str:
        day = "今日" if until.date() == self._wall().date() else "明日"
        return f"{day}の{_hour_text(until.hour)}"

    def open_dialog(self, kind: str) -> None:
        """通知のクリックでだけ開く(FR-5・INV-3)。"""
        from deskkit.modules.eyebreak.dialog import BreakDialog

        old = self._dialog
        if old is not None:
            try:
                old.close()
            except RuntimeError:
                pass
        parent = self.ctx.window_parent() if hasattr(self.ctx, "window_parent") else None
        self._dialog = BreakDialog(self, kind, parent)
        self._dialog.show()
        self._dialog.raise_()
        self._dialog.activateWindow()

    def open_page(self) -> None:
        show = getattr(self.ctx, "show_page", None)
        if show is not None:
            show()

    # ================================================================ 設定
    def update_settings(self, changes: Mapping[str, Any]) -> str | None:
        """画面から設定を変える。今の数えは保つ(§10)。誤りなら理由を返し、何も変えない。"""
        from deskkit.modules.eyebreak.config import EyeBreakSettingsError

        new = dict(self.section)
        for k, v in changes.items():
            if k in ("eye", "body") and isinstance(v, Mapping):
                new[k] = {**new.get(k, {}), **v}
            else:
                new[k] = v
        try:
            sec, cfg, _filled = parse(new)
        except EyeBreakSettingsError as e:
            return str(e)
        try:
            self.ctx.write_settings(sec)
        except Exception as e:  # noqa: BLE001
            self.log.warning("settings write failed: %s", type(e).__name__)
            return "設定を保存できませんでした"
        self.section = sec
        self.cfg = cfg
        self.engine.with_config(cfg)
        self.engine.set_quiet(self._mode is not None and self._mode in self._quiet_names())
        later = self._tray.get("later")
        if later is not None:
            later.set_text(f"{cfg.later_min}分あとで")
        self.update_status()
        self.notifier.changed.emit()
        return None

    # ================================================================ 表示
    def status_text(self) -> str:
        return self.engine.status(self._wall())

    def update_status(self) -> None:
        if not self._running:
            return
        setter = getattr(self.ctx, "set_tray_status", None)
        if setter is not None:
            setter(self.status_text())
        muted = self.muted()
        if "mute" in self._tray:
            self._tray["mute"].set_enabled(not muted)
            self._tray["resume"].set_enabled(muted or self.engine.later_ is not None)

    def today(self) -> date:
        return self._wall().date()

    def today_counts(self) -> dict[str, int]:
        t = self.today()
        return {f: self.daily.get(t, f) for f in ("eye", "body", "rested", "later", "mute_today", "dry")}

    def use_text(self) -> str:
        return fmt_minutes(int(self.engine.tracker.use_s // 60))

    # ================================================================ usage / diagnostics
    def usage(self, days: int) -> list[UsageSeries]:
        t = self.today()
        hint = "90 日で声かけが 0 回なら、README の紹介から外す(R-3)"
        return [UsageSeries("prompts", "声かけ", self.daily.series(days, t, (EYE, BODY)), primary=True, hint=hint,
                            good_when="neutral"),
                UsageSeries("rested", "休めた", self.daily.series(days, t, ("rested",)), good_when="high")]

    def diagnostics(self) -> dict[str, str | int | bool]:
        t = self.today()
        return {
            "mode": self.cfg.mode,
            "idle_readable": not self.engine.unavailable,
            "game": self.cfg.game,
            "quiet_modes": len(self.cfg.quiet_modes),
            "muted_today": self.muted(),
            "replace_key": self._replace_ok,
            "prompts_14d": sum(self.daily.series(14, t, (EYE, BODY))),
            "rested_14d": sum(self.daily.series(14, t, ("rested",))),
            "later_14d": sum(self.daily.series(14, t, ("later",))),
            "mute_today_14d": sum(self.daily.series(14, t, ("mute_today",))),
        }
