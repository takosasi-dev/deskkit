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
from deskkit.foreground import query_foreground, query_quns
from deskkit.hotkeys import HotkeyHub
from deskkit.ipc import IpcServer
from deskkit.loader import Loader
from deskkit.nativewin import NativeWindow
from deskkit.notify_hold import Note, NotificationHold, summary_note
from deskkit.settings import SettingsStore
from deskkit.snapshots import DIR_NAME as SNAPSHOT_DIR
from deskkit.snapshots import Snapshot, SnapshotStore
from deskkit.snooze import SnoozeManager
from deskkit.tray import Tray
from deskkit.ui.toast import ToastManager

log = logging.getLogger("deskkit.host")

CLI_ALIASES = {"mode": "modeshift"}
ACTIVITY_CLICK_TTL = _dt.timedelta(minutes=10)  # 「最近の通知」の行クリックで元の on_click を呼ぶのはこの時間内だけ
SNAPSHOT_DEBOUNCE_MS = 2000
# 前面がこの通知状態(QUNS)なら、矩形で全画面と判定できなくても通知を保留する
_QUNS_HOLD = frozenset({"running_d3d_full_screen", "presentation_mode"})


@dataclass
class Activity:
    ts: _dt.datetime
    source: str
    title: str
    level: str
    on_click: Callable[[], Any] | None = None
    alive: Callable[[], bool] | None = None  # モジュールの通知なら、その ctx がまだ動いているか


class HostSignals(QObject):
    module_changed = Signal(str)
    activity = Signal(object)
    settings_reloaded = Signal()
    snooze_changed = Signal()
    settings_replaced = Signal()  # 設定をファイルから読み直した(再読み込み・世代の復元・バックアップの読み込み)
    snapshot_request = Signal()  # settings.json が書かれた(どのスレッドからでも emit してよい)
    snapshots_changed = Signal()  # 設定の世代が増えた・減った


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
            on_quick=self.open_quick_actions, on_snooze=self.snooze_for, on_resume=self.resume,
        )
        # 一時停止(H2)。状態はメモリだけ(再起動で解除)
        self.snooze = SnoozeManager(query_quns, lambda: bool(self.settings.host().get("snooze_follow_quns", False)))
        self.snooze.changed.connect(self._snooze_changed)
        # 通知の保留(H1)
        self.hold = NotificationHold(lambda: self._foreground_busy(), lambda: bool(self.settings.host().get("hold_notifications", True)),
                                     lambda n: self._show_note(n), self._summary_note)
        # 設定の自動世代保存(H3)。連続した書き込みは 2 秒の間まとめて1世代にする
        self.snapshots = SnapshotStore(paths.local_dir() / SNAPSHOT_DIR,
                                       lambda: int(self.settings.host().get("settings_history_keep", 20)))
        self._snap_timer = QTimer()
        self._snap_timer.setSingleShot(True)
        self._snap_timer.setInterval(SNAPSHOT_DEBOUNCE_MS)
        self._snap_timer.timeout.connect(self.snapshot_now)
        self.signals.snapshot_request.connect(lambda: self._snap_timer.start())
        self.settings.on_written = self.signals.snapshot_request.emit
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
        if res.ok:
            self.snapshot_now()  # 起動時点の設定を1世代目にする(同じ内容なら増えない)
        self.snooze.sync_settings()
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
        self.tray.set_quick_hotkey(None)
        if not text:
            return
        try:
            mods, vk = parse_hotkey(text)
            self.hotkeys.register("host.quick", mods, vk)
        except (ValueError, HotkeyError):
            self.hotkeys.note_conflict("host.quick", text)
            return
        self.hotkeys.add_callback("host.quick", self._quick_hotkey)
        self.tray.set_quick_hotkey(self.hotkeys.combo_text("host.quick"))

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

    def notify(self, source: str, title: str, text: str, on_click: Callable[[], Any] | None, *, level: str = "info",
               alive: Callable[[], bool] | None = None) -> None:
        """通知を記録してトーストで出す。ゲーム・全画面中は error 以外を保留する(H1。記録はすぐに行う)。"""
        act = Activity(_dt.datetime.now(), source, title, level, on_click, alive)
        self.activities.insert(0, act)
        del self.activities[60:]
        self.signals.activity.emit(act)
        note = Note(source, title, text, on_click, level, act.ts)
        if self.hold.offer(note):
            log.info("notify held source=%s level=%s", source, level)  # 種類だけ(INV-7)
            return
        log.info("notify source=%s level=%s", source, level)
        self._show_note(note)

    def _show_note(self, n: Note) -> None:
        style = str(self.settings.host().get("notification_style", "toast"))
        accent = "#7C8CFF" if n.source == "host" else catalog.info(n.source).accent
        glyph = None if n.source == "host" else catalog.info(n.source).glyph
        if style == "balloon":
            self._last_balloon_cb = n.on_click
            self.tray.show_balloon(f"{self.title_of(n.source)}: {n.title}", n.text)
        else:
            self.toasts.show(self.title_of(n.source), accent, n.title, n.text, n.on_click, n.level,
                             glyph if n.level == "info" else None)

    def _summary_note(self, held: list[Note], total: int) -> Note:
        log.info("notify held summary count=%d", total)
        return summary_note(held, total, self.title_of, lambda: self.show_window("home"))

    def _foreground_busy(self) -> bool:
        """通知を保留すべき前面か(ゲーム・全画面)。全画面か判定できないときは Windows の通知状態で決める。"""
        fg = query_foreground(self.settings.game_processes(), str(self.settings.host().get("fullscreen_detection", "rect")))
        if fg.exe == "deskkit":
            return False
        if fg.is_game or fg.is_fullscreen is True or fg.quns in _QUNS_HOLD:
            return True
        return fg.is_fullscreen is None and fg.quns == "busy"

    def open_activity(self, a: Activity) -> None:
        """「最近の通知」の行クリック。10 分以内で送り主が動いていれば元の動作、それ以外は送り主の画面を開く。"""
        fresh = (a.on_click is not None and _dt.datetime.now() - a.ts <= ACTIVITY_CLICK_TTL
                 and (a.alive is None or a.alive()))
        if fresh and a.on_click is not None:
            try:
                a.on_click()
                return
            except Exception:  # noqa: BLE001
                log.exception("通知クリックの処理で例外")
        self.show_window(a.source if a.source in catalog.MODULE_NAMES else "home")

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
        snooze = self.snooze.status_text() if hasattr(self, "snooze") else ""
        suffix = f"\n{snooze}" if snooze else ""
        if stopped:
            tip = f"{APP_NAME} — 停止中: " + ", ".join(self.title_of(n) for n in stopped)
            self.tray.set_badge("error", tip + suffix)
        else:
            tip = f"{APP_NAME} — " + (", ".join(self.title_of(n) for n in running) + " 動作中" if running else "モジュール未使用")
            self.tray.set_badge("paused" if snooze else None, tip + suffix)

    # ---- 一時停止(H2)
    def snooze_for(self, minutes: int | None) -> None:
        """minutes 分(None は再開するまで)一時停止する。自動で動く処理だけが止まる。"""
        self.snooze.snooze(minutes)
        self.notify("host", "一時停止しました", self.snooze.status_text() + "\n手で行う操作はそのまま使えます", None, level="ok")

    def resume(self) -> None:
        was = self.snooze.manual_active()
        self.snooze.resume()
        if was:
            self.notify("host", "一時停止を終わりました", "自動の処理を再開します", None, level="ok")

    def _snooze_changed(self) -> None:
        payload = self.snooze.payload()
        log.info("snooze changed snoozed=%s timed=%s", payload["snoozed"], payload["until"] is not None)
        self.events.emit("host", "host.snooze_changed", payload)
        self.tray.set_snooze(self.snooze.manual_active(), self.snooze.status_text())
        self.refresh_badge()
        self.signals.snooze_changed.emit()
        if self.snooze.auto_resumed:
            self.snooze.auto_resumed = False
            self.notify("host", "一時停止を終わりました", "予定の時間になったので、自動の処理を再開します", None, level="ok")

    # ---- 設定の世代(H3)
    def snapshot_now(self) -> Snapshot | None:
        self._snap_timer.stop()
        try:
            snap = self.snapshots.save_file(self.settings.path)
        except OSError:
            log.exception("設定の世代を保存できません")
            return None
        if snap is not None:
            log.info("settings snapshot saved")
            self.signals.snapshots_changed.emit()
        return snap

    def restore_snapshot(self, snap: Snapshot) -> None:
        """今の設定を1世代残してから snap の内容で置き換え、再読み込みする。失敗は SettingsError / OSError。"""
        data = self.snapshots.read(snap)
        self.snapshot_now()
        self.settings.replace_all(data)
        self.snapshot_now()
        self.reload()

    # ---- 診断(H4)
    def diagnostics_report(self) -> str:
        from deskkit.diagnostics import build_report

        return build_report(self)

    def copy_diagnostics(self) -> None:
        from PySide6.QtGui import QGuiApplication

        try:
            text = self.diagnostics_report()
        except Exception as e:  # noqa: BLE001
            log.exception("診断レポートを作れません")
            self.notify("host", "診断レポートを作れませんでした", type(e).__name__, None, level="error")
            return
        QGuiApplication.clipboard().setText(text)
        self.notify("host", "診断レポートをコピーしました", "パス・ユーザー名・本文は含みません。貼り付けて共有できます", None, level="ok")

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
        self.snooze.sync_settings()
        self.loader.load_all()
        self.register_host_hotkeys()
        self.after_hotkey_registration()
        self.signals.settings_reloaded.emit()
        self.signals.settings_replaced.emit()
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
            if len(args) >= 2 and args[1] == "open" and name in catalog.MODULE_NAMES:
                # 「送る」から渡されたのに受け取れない: 画面から起動していないので、黙って捨てずに知らせる(H-B)
                title = catalog.info(name).title
                target = name

                def open_page() -> None:
                    self.show_window(target)

                self.notify("host", f"{title} が無効なので、ファイルを受け取れませんでした",
                            f"Control Center で {title} を有効にしてから、もう一度送ってください", open_page, level="warn")
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

        cmd = paths.launch_command() + ["--post-update", str(os.getpid())]
        cwd = None if paths.is_frozen() else str(paths.source_root())
        subprocess.Popen(cmd, cwd=cwd, creationflags=0x00000008 | 0x00000200, close_fds=True,  # DETACHED | NEW_GROUP
                         env=paths.child_env())
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
        if self._snap_timer.isActive():
            self.snapshot_now()
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
