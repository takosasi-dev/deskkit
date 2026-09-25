# QSystemTrayIcon を1つだけ持ち、モジュールごとのサブメニューと host の共通項目(FR-4)を組み立てる。
# モジュールの項目はモジュール自身が ctx 経由で足す。host は状態表示と共通項目だけを知っている。
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from deskkit import APP_NAME, catalog
from deskkit.ui import icons


class _ModuleMenu:
    def __init__(self, menu: QMenu) -> None:
        self.menu = menu
        self.status = QAction("…", self.menu)
        self.status.setEnabled(False)
        self.menu.addAction(self.status)
        self.sep = self.menu.addSeparator()
        self.subs: dict[str, QMenu] = {}

    def target(self, submenu: str | None) -> QMenu:
        if submenu is None:
            return self.menu
        if submenu not in self.subs:
            self.subs[submenu] = self.menu.addMenu(submenu)
        return self.subs[submenu]

    def clear(self, submenu: str | None) -> None:
        if submenu is not None:
            m = self.subs.get(submenu)
            if m is not None:
                m.clear()
            return
        for a in list(self.menu.actions()):
            if a is self.status or a is self.sep:
                continue
            self.menu.removeAction(a)
        self.subs.clear()


def _style_menu(m: QMenu) -> None:
    m.setWindowFlags(m.windowFlags() | Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint)
    m.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)


class Tray:
    def __init__(self, *, on_open: Callable[[], None], on_open_settings_file: Callable[[], None],
                 on_reload: Callable[[], None], on_open_logs: Callable[[], None],
                 on_toggle_autostart: Callable[[], None], autostart_state: Callable[[], str],
                 on_quit: Callable[[], None], on_quick: Callable[[], None] | None = None,
                 on_snooze: Callable[[int | None], None] | None = None, on_resume: Callable[[], None] | None = None) -> None:
        self.icon = QSystemTrayIcon(icons.app_icon())
        self.icon.setToolTip(APP_NAME)
        self.menu = QMenu()
        _style_menu(self.menu)
        self._autostart_state = autostart_state
        # 一時停止中だけ先頭に出す状態行
        self.snooze_status = self.menu.addAction("")
        self.snooze_status.setEnabled(False)
        self.snooze_status.setVisible(False)
        open_act = self.menu.addAction("DeskKit を開く")
        f = QFont(open_act.font())
        f.setBold(True)
        open_act.setFont(f)
        open_act.triggered.connect(lambda: on_open())
        # クイックアクション(右側にホットキーを出す: QMenu はタブ文字の後ろをショートカット欄に表示する)
        self.quick = self.menu.addAction("クイックアクション")
        if on_quick is not None:
            self.quick.triggered.connect(lambda: on_quick())
        else:
            self.quick.setVisible(False)
        # 一時停止(H2)
        self.snooze_menu = QMenu("一時停止", self.menu)
        _style_menu(self.snooze_menu)
        self.menu.addMenu(self.snooze_menu)
        self.snooze_actions: list[QAction] = []
        if on_snooze is not None:
            for label, minutes in (("30分", 30), ("1時間", 60), ("再開するまで", None)):
                a = self.snooze_menu.addAction(label)
                a.triggered.connect(lambda _c=False, m=minutes: on_snooze(m))
                self.snooze_actions.append(a)
        self.snooze_menu.addSeparator()
        self.resume_act = self.snooze_menu.addAction("再開")
        self.resume_act.setEnabled(False)
        if on_resume is not None:
            self.resume_act.triggered.connect(lambda: on_resume())
        self.snooze_menu.menuAction().setVisible(on_snooze is not None)
        self.menu.addSeparator()
        self._module_anchor = self.menu.addSeparator()
        self._modules: dict[str, _ModuleMenu] = {}
        self.menu.addAction("設定ファイルを開く").triggered.connect(lambda: on_open_settings_file())
        self.menu.addAction("設定を再読み込み").triggered.connect(lambda: on_reload())
        self.menu.addAction("ログフォルダを開く").triggered.connect(lambda: on_open_logs())
        self.autostart = self.menu.addAction("Windows 起動時に自動起動")
        self.autostart.setCheckable(True)
        self.autostart.triggered.connect(lambda _c=False: on_toggle_autostart())
        self.menu.addSeparator()
        self.menu.addAction("終了").triggered.connect(lambda: on_quit())
        self.menu.aboutToShow.connect(self.refresh_autostart)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        self._on_open = on_open
        self.refresh_autostart()

    def show(self) -> None:
        self.icon.show()

    def reshow(self) -> None:
        """エクスプローラー再起動(TaskbarCreated)後にアイコンを出し直す。"""
        self.icon.hide()
        self.icon.show()

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self._on_open()

    def refresh_autostart(self) -> None:
        st = self._autostart_state()
        self.autostart.setChecked(st == "on")
        self.autostart.setText("Windows 起動時に自動起動" + ("(パス不一致)" if st == "mismatch" else ""))

    def set_quick_hotkey(self, text: str | None) -> None:
        self.quick.setText("クイックアクション" + (f"\t{text}" if text else ""))

    def set_snooze(self, manual: bool, status: str) -> None:
        """一時停止の状態を反映する。manual は利用者が止めたか(「再開」を押せるか)。status は空なら非表示。"""
        self.snooze_status.setText(status)
        self.snooze_status.setVisible(bool(status))
        self.resume_act.setEnabled(manual)
        self.snooze_menu.setTitle("一時停止中" if manual else "一時停止")

    def set_badge(self, badge: str | None, tooltip: str) -> None:
        self.icon.setIcon(icons.app_icon(badge))
        self.icon.setToolTip(tooltip)

    # ---- モジュールのサブメニュー
    def ensure_module(self, name: str) -> _ModuleMenu:
        mm = self._modules.get(name)
        if mm is None:
            m = QMenu(catalog.info(name).title, self.menu)
            _style_menu(m)
            self.menu.insertMenu(self._module_anchor, m)
            mm = _ModuleMenu(m)
            self._modules[name] = mm
        return mm

    def remove_module(self, name: str) -> None:
        mm = self._modules.pop(name, None)
        if mm is not None:
            self.menu.removeAction(mm.menu.menuAction())
            mm.menu.deleteLater()

    def reset_module(self, name: str) -> None:
        mm = self._modules.get(name)
        if mm is not None:
            mm.clear(None)

    def set_module_status(self, name: str, text: str) -> None:
        self.ensure_module(name).status.setText(text or "…")

    def add_module_action(self, name: str, label: str, cb: Callable[[], Any], *, checkable: bool = False,
                          checked: bool = False, submenu: str | None = None) -> QAction:
        mm = self.ensure_module(name)
        target = mm.target(submenu)
        if submenu is not None:
            _style_menu(target)
        act = target.addAction(label)
        if checkable:
            act.setCheckable(True)
            act.setChecked(checked)
        act.triggered.connect(lambda _c=False: cb())
        return act

    def add_module_separator(self, name: str, submenu: str | None) -> None:
        self.ensure_module(name).target(submenu).addSeparator()

    def clear_module_actions(self, name: str, submenu: str | None) -> None:
        mm = self._modules.get(name)
        if mm is not None:
            mm.clear(submenu)

    def show_balloon(self, title: str, text: str) -> None:
        self.icon.showMessage(title, text, QSystemTrayIcon.MessageIcon.Information, 5000)
