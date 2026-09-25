# host 本体。設定・ログ・隠しウィンドウ・ホットキー・トレイ・通知・イベント・IPC・ローダーを配線する。
# QOL 機能の知識は持たない(INV-1)。CLI 引数の振り分けと、Control Center の表示もここから行う。
from __future__ import annotations

import datetime as _dt
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QWidget

from deskkit import APP_NAME, __version__, autostart, catalog, paths, win32
from deskkit.events import EventBus
from deskkit.hotkeys import HotkeyHub
from deskkit.ipc import IpcServer
from deskkit.loader import Loader
from deskkit.nativewin import NativeWindow
from deskkit.settings import SettingsStore
from deskkit.tray import Tray
from deskkit.ui.toast import ToastManager

log = logging.getLogger("deskkit.host")

CLI_ALIASES = {"mode": "modeshift"}


@dataclass
class Activity:
    ts: _dt.datetime
    source: str
    title: str
    level: str


class HostSignals(QObject):
    module_changed = Signal(str)
    activity = Signal(object)
    settings_reloaded = Signal()


class Host:
    def __init__(self, app: QApplication, dpi_awareness: str, *, start_ipc: bool = True, show_tray: bool = True) -> None:
        self.app = app
        self.dpi_awareness = dpi_awareness
        self.signals = HostSignals()
        self.settings = SettingsStore(paths.settings_path())
        self.native = NativeWindow()
        self.hotkeys = HotkeyHub(self.native.hwnd)
        self.native.hotkey_sink = self.hotkeys.registry.handle_wm_hotkey
        self.events = EventBus()
        self.toasts = ToastManager()
        self.activities: list[Activity] = []
        self._last_balloon_cb: Callable[[], Any] | None = None
        self.tray = Tray(
            on_open=self.show_window, on_open_settings_file=self.open_settings_file, on_reload=self.reload,
            on_open_logs=self.open_logs, on_toggle_autostart=self.toggle_autostart,
            autostart_state=autostart.state, on_quit=self.quit,
        )
        self.tray.icon.messageClicked.connect(self._balloon_clicked)
        self.native.taskbar_created_sink = self.tray.reshow
        self.loader = Loader(self)
        self.loader.changed.connect(self.signals.module_changed.emit)
        self._window: QWidget | None = None
        self._quick: Any = None
        self.ipc = IpcServer(self.handle_cli) if start_ipc else None
        self._show_tray = show_tray
        self._quitting = False
        from deskkit.update_manager import UpdateManager

        self.updates = UpdateManager(self)

    # ---- 起動
    def start(self) -> bool:
        res = self.settings.load()
        from deskkit import logging_setup

        logging_setup.setup(paths.log_dir(), int(self.settings.host().get("log_retention_days", 14)))
        log.info("%s %s 起動 dpi_awareness=%s", APP_NAME, __version__, self.dpi_awareness)
        if self._show_tray:
            self.tray.show()
        if self.ipc is not None:
            self.ipc.listen()
        if not res.ok:
            log.error("設定を読めません: %s", res.error)
            self.notify("host", "設定ファイルを読めません", f"{res.error}" + (f"({res.line} 行目)" if res.line else ""),
                        self.open_settings_file, level="error")
        elif res.created:
            log.info("既定の settings.json を作成しました")
        self.loader.load_all()
        self.register_host_hotkeys()
        self.after_hotkey_registration()
        self.app.aboutToQuit.connect(self._shutdown)
        if self.ipc is not None:  # selftest では通信しない
            self.updates.start()
        return res.created

    def register_host_hotkeys(self) -> None:
        """host 自身のホットキー(クイックアクション)を登録し直す。競合は FR-9 の一括通知に載せる。"""
        from deskkit.hotkeys import parse_hotkey
        from overlaykit import HotkeyError

        self.hotkeys.drop("host.quick")
        text = str(self.settings.host().get("quick_action_hotkey") or "")
        if not text:
            return
        try:
            mods, vk = parse_hotkey(text)
            self.hotkeys.registry.register("host.quick", mods, vk)
        except (ValueError, HotkeyError):
            self.hotkeys.conflicts.append(("host.quick", text))
            return
        self.hotkeys.add_callback("host.quick", self._quick_hotkey)

    def _quick_hotkey(self) -> None:
        try:
            from deskkit.foreground import query_foreground

            fg = query_foreground(self.settings.game_processes(), str(self.settings.host().get("fullscreen_detection", "rect")))
            if fg.is_game or fg.is_fullscreen is not False:
                log.info("quick actions blocked reason=%s", "game" if fg.is_game else "fullscreen")
                return
            self.open_quick_actions()
        except Exception:  # noqa: BLE001 - ホットキーから例外を漏らさない
            log.exception("クイックアクションを開けません")

    def open_quick_actions(self) -> None:
        if self._quick is None:
            from deskkit.ui.quick_actions import QuickActions

            self._quick = QuickActions(self)
        self._quick.open()

    def show_onboarding(self) -> None:
        from deskkit.ui.onboarding import Onboarding

        self.show_window()
        Onboarding(self, self._window).exec()

    def after_hotkey_registration(self) -> None:
        """起動・再読み込み直後に、ホットキー競合をまとめて1回だけ通知する(FR-9)。"""
        if self.hotkeys.conflicts:
            items = ", ".join(f"{n}({k})" for n, k in self.hotkeys.conflicts)
            log.warning("ホットキー登録失敗: %s", items)
            self.notify("host", "登録できなかったホットキーがあります", items, self.show_window, level="warn")
            self.hotkeys.conflicts.clear()

    # ---- 通知
    def title_of(self, name: str) -> str:
        return APP_NAME if name == "host" else catalog.info(name).title

    def notify(self, source: str, title: str, text: str, on_click: Callable[[], Any] | None, *, level: str = "info") -> None:
        log.info("notify source=%s level=%s", source, level)  # 種類だけ(INV-7)
        act = Activity(_dt.datetime.now(), source, title, level)
        self.activities.insert(0, act)
        del self.activities[60:]
        self.signals.activity.emit(act)
        style = str(self.settings.host().get("notification_style", "toast"))
        accent = "#7C8CFF" if source == "host" else catalog.info(source).accent
        glyph = None if source == "host" else catalog.info(source).glyph
        if style == "balloon":
            self._last_balloon_cb = on_click
            self.tray.show_balloon(f"{self.title_of(source)}: {title}", text)
        else:
            self.toasts.show(self.title_of(source), accent, title, text, on_click, level, glyph if level == "info" else None)

    def _balloon_clicked(self) -> None:
        cb, self._last_balloon_cb = self._last_balloon_cb, None
        if cb is not None:
            try:
                cb()
            except Exception:  # noqa: BLE001
                log.exception("通知クリックの処理で例外")

    # ---- 状態
    def fault(self, name: str, reason: str) -> None:
        self.loader.fault(name, reason)

    def module_status_changed(self, name: str) -> None:
        self.signals.module_changed.emit(name)

    def refresh_badge(self) -> None:
        stopped = [s.name for s in self.loader.slots.values() if s.state == "stopped"]
        running = [s.name for s in self.loader.slots.values() if s.state == "running"]
        if stopped:
            tip = f"{APP_NAME} — 停止中: " + ", ".join(self.title_of(n) for n in stopped)
            self.tray.set_badge("error", tip)
        else:
            tip = f"{APP_NAME} — " + (", ".join(self.title_of(n) for n in running) + " 動作中" if running else "モジュール未使用")
            self.tray.set_badge(None, tip)

    # ---- トレイの共通項目
    def open_settings_file(self) -> None:
        os.startfile(str(paths.settings_path()))  # noqa: S606 - 利用者の明示操作

    def open_logs(self) -> None:
        os.startfile(str(paths.log_dir()))  # noqa: S606

    def toggle_autostart(self) -> None:
        try:
            st = autostart.toggle()
        except OSError as e:
            self.notify("host", "自動起動を切り替えられません", str(e), None, level="error")
            return
        self.tray.refresh_autostart()
        self.notify("host", "自動起動をオンにしました" if st == "on" else "自動起動をオフにしました", "", None, level="ok")
        self.signals.settings_reloaded.emit()

    def reload(self) -> None:
        """全モジュール stop → 設定検証 → 有効なものを start。ホットキーは全解除して登録し直す(FR-5)。"""
        res = self.settings.load()
        if not res.ok:
            self.notify("host", "設定を再読み込みできません", f"{res.error}" + (f"({res.line} 行目)" if res.line else "")
                        + "\n前回の設定で動作を続けます", self.open_settings_file, level="error")
            return
        self.loader.stop_all()
        self.hotkeys.unregister_all()
        self.loader.load_all()
        self.register_host_hotkeys()
        self.after_hotkey_registration()
        self.signals.settings_reloaded.emit()
        self.notify("host", "設定を再読み込みしました", "", None, level="ok")

    def set_module_enabled(self, name: str, enabled: bool) -> None:
        self.settings.set_enabled(name, enabled)
        if enabled:
            self.loader.start_one(name)
            self.after_hotkey_registration()
        else:
            self.loader.stop_one(name)

    # ---- Control Center
    def show_window(self, page: str | None = None) -> None:
        if self._window is None:
            from deskkit.ui.main_window import ControlCenter

            self._window = ControlCenter(self)
        w = self._window
        if page is not None:
            w.open_page(page)  # type: ignore[attr-defined]
        w.show()
        if w.isMinimized():
            w.showNormal()
        w.raise_()
        w.activateWindow()

    def main_window_if_visible(self) -> QWidget | None:
        w = self._window
        return w if w is not None and w.isVisible() else None

    # ---- CLI(FR-13)
    def handle_cli(self, args: list[str]) -> tuple[int, str]:
        if not args or args[0] in ("--show", "--autostart"):
            if not args or args[0] == "--show":
                QTimer.singleShot(0, self.show_window)
            return 0, ""
        if args[0] == "--quit":
            QTimer.singleShot(100, self.quit)
            return 0, "DeskKit を終了します"
        if args[0] == "--reload":
            QTimer.singleShot(0, self.reload)
            return 0, "再読み込みします"
        name = CLI_ALIASES.get(args[0], args[0])
        mod = self.loader.module(name)
        if mod is None:
            return 11, f"{name} は無効または停止中です"
        ctx = self.loader.slots[name].ctx
        assert ctx is not None
        result = ctx.safe(mod.handle_cli, "handle_cli")(list(args[1:]))
        if result is None:
            return 1, "モジュールの処理で例外が発生しました(ログを確認してください)"
        code, out = result
        return int(code), str(out)

    # ---- 終了
    def restart(self) -> None:
        """同じ DeskKit を起動し直す(テーマ変更の反映など)。新しい方はこのプロセスの終了を待ってから始まる。"""
        import subprocess
        from pathlib import Path

        import deskkit

        cmd = paths.launch_command() + ["--post-update", str(os.getpid())]
        cwd = None if paths.is_frozen() else str(Path(deskkit.__file__).resolve().parents[1])
        subprocess.Popen(cmd, cwd=cwd, creationflags=0x00000008 | 0x00000200, close_fds=True)  # DETACHED | NEW_GROUP
        QTimer.singleShot(150, self.quit)

    @property
    def quitting(self) -> bool:
        return self._quitting

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        if self._window is not None:
            self._window.close()  # Qt 6 の quit() は窓を閉じようとするので、先に閉じておく
        self.app.exit(0)

    def _shutdown(self) -> None:
        log.info("終了処理")
        self.loader.stop_all()
        self.hotkeys.unregister_all()
        self.native.close_native()
        if self.ipc is not None:
            self.ipc.close()
        self.tray.icon.hide()


def dpi_setup() -> str:
    """QApplication 生成前に Per-Monitor V2 を確定させる(D-6)。"""
    import ctypes

    try:
        win32.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(win32.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
    except (AttributeError, OSError):
        pass
    return win32.dpi_awareness_text()
