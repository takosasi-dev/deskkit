# モジュールに渡す唯一の窓口 ModuleContext(§9.1)の実装。モジュールが登録した資源(トレイ項目・
# ホットキー・購読・タイマー)を記録し、停止時に host がまとめて片付ける。Qt から呼ばれる経路は
# すべて safe() の例外ラッパーを通す(D-3 / INV-5)。
from __future__ import annotations

import copy
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction

from deskkit import paths
from deskkit.foreground import ForegroundInfo, query_foreground
from deskkit.hotkeys import ModuleHotkeys

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

    from deskkit.host import Host

T = TypeVar("T")


class Module(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def handle_cli(self, args: list[str]) -> tuple[int, str]: ...


class TrayItem:
    """モジュールが追加したトレイ項目の操作ハンドル。"""

    def __init__(self, action: QAction) -> None:
        self._a = action

    def set_text(self, text: str) -> None:
        self._a.setText(text)

    def set_checked(self, checked: bool) -> None:
        self._a.setCheckable(True)
        self._a.setChecked(checked)

    def set_enabled(self, enabled: bool) -> None:
        self._a.setEnabled(enabled)

    def set_visible(self, visible: bool) -> None:
        self._a.setVisible(visible)


@dataclass
class QuickAction:
    """クイックアクション(検索窓から実行する操作)の1件。トレイ項目は自動で候補になるので、それ以外の操作だけ登録する。"""

    label: str
    callback: Callable[[], None]
    keywords: str = ""
    glyph: str | None = None
    enabled: Callable[[], bool] | None = None


class _Invoker(QObject):
    call = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    def _run(self, fn: object) -> None:
        fn()  # type: ignore[operator]  # fn は safe 済み


def list_modes(section: Mapping[str, Any]) -> list[tuple[str, str]]:
    """modeshift セクションの modes[*] から (name, label)。name が空・文字列でない要素は飛ばし、label が無ければ name。"""
    modes = section.get("modes")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    if not isinstance(modes, list):
        return out
    for m in modes:
        if not isinstance(m, dict):
            continue
        name = m.get("name")
        if not isinstance(name, str) or not name.strip() or name in seen:
            continue
        label = m.get("label")
        seen.add(name)
        out.append((name, label if isinstance(label, str) and label.strip() else name))
    return out


class ModuleContextImpl:
    def __init__(self, host: Host, name: str) -> None:
        self._host = host
        self.name = name
        self.data_dir: Path = paths.data_dir(name)
        self.log: logging.Logger = logging.getLogger(f"deskkit.{name}")
        self.hotkeys = ModuleHotkeys(host.hotkeys, self)
        self._timers: list[QTimer] = []
        self._errors: dict[str, int] = {}
        self.error_totals: dict[str, int] = {}  # 起動からの例外の累計(診断レポート用。連続回数の _errors とは別)
        self._alive = True
        self._invoker = _Invoker()
        self._lock = threading.Lock()
        self.status_text = ""
        self.quick_actions: list[QuickAction] = []

    # ---- 生存管理(host 用)
    @property
    def alive(self) -> bool:
        return self._alive

    def teardown(self) -> None:
        self._alive = False
        self.quick_actions.clear()
        for t in self._timers:
            t.stop()
        self._timers.clear()
        self.hotkeys.release_all()
        self._host.native.unsubscribe_owner(self.name)
        self._host.events.drop_owner(self.name)

    # ---- 例外ラッパー(D-3)
    def safe(self, fn: Callable[..., T], label: str | None = None) -> Callable[..., T | None]:
        key: str = label or str(getattr(fn, "__qualname__", "handler"))

        def wrapper(*args: Any, **kwargs: Any) -> T | None:
            if not self._alive:
                return None
            try:
                result = fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 - モジュールの例外を host へ漏らさない
                n = self._errors.get(key, 0) + 1
                self._errors[key] = n
                self.error_totals[key] = self.error_totals.get(key, 0) + 1
                self.log.exception("ハンドラ %s で例外(連続 %d 回目)", key, n)
                limit = int(self._host.settings.host().get("handler_error_limit", 5))
                if n >= limit:
                    self._host.fault(self.name, f"{key} で例外が {n} 回連続: {type(e).__name__}")
                return None
            self._errors.pop(key, None)
            return result

        return wrapper

    # ---- 設定
    def settings(self) -> Mapping[str, Any]:
        return MappingProxyType(self._host.settings.module_section(self.name))

    def settings_dict(self) -> dict[str, Any]:
        """書き換え用の深いコピー(write_settings と組で使う)。"""
        return copy.deepcopy(self._host.settings.module_section(self.name))

    def write_settings(self, section: Mapping[str, Any], *, restart: bool = False) -> None:
        """自分のセクションを settings.json に書く。構文エラーのファイルは上書きせず SettingsError。"""
        sec = dict(section)
        sec["enabled"] = self._host.settings.module_section(self.name).get("enabled", True)
        self._host.settings.write_module(self.name, sec)
        if restart:
            QTimer.singleShot(0, lambda: self._host.loader.restart(self.name))

    def game_processes(self) -> frozenset[str]:
        return self._host.settings.game_processes()

    def list_modes(self) -> list[tuple[str, str]]:
        """ModeShift の設定にあるモードの (name, label) 一覧(ModeShift が無効でも返す。壊れた要素は飛ばす)。"""
        return list_modes(self._host.settings.module_section("modeshift"))

    # ---- 一時停止(H2)
    def is_snoozed(self) -> bool:
        """一時停止中なら True。自動で動く処理の入口で見る(手で押した操作は止めない)。どのスレッドからでも呼べる。"""
        return self._host.snooze.is_snoozed()

    # ---- トレイ
    def add_tray_action(self, label: str, callback: Callable[[], None], *, checkable: bool = False,
                        checked: bool = False, submenu: str | None = None) -> TrayItem:
        act = self._host.tray.add_module_action(self.name, label, self.safe(callback, f"tray:{label}"),
                                                checkable=checkable, checked=checked, submenu=submenu)
        return TrayItem(act)

    def add_tray_separator(self, submenu: str | None = None) -> None:
        self._host.tray.add_module_separator(self.name, submenu)

    def clear_tray_actions(self, submenu: str | None = None) -> None:
        self._host.tray.clear_module_actions(self.name, submenu)

    def add_quick_action(self, label: str, callback: Callable[[], None], *, keywords: str = "",
                         glyph: str | None = None, enabled: Callable[[], bool] | None = None) -> None:
        """クイックアクションに操作を足す(トレイ項目は自動で入るので不要)。keywords は検索用の別名(空白区切り)。"""
        self.quick_actions.append(QuickAction(label, self.safe(callback, f"quick:{label}"), keywords, glyph, enabled))

    def clear_quick_actions(self) -> None:
        self.quick_actions.clear()

    def set_tray_status(self, text: str) -> None:
        self.status_text = text
        self._host.tray.set_module_status(self.name, text)
        self._host.module_status_changed(self.name)

    def notify(self, title: str, text: str, on_click: Callable[[], None] | None = None, *,
               level: str = "info") -> None:
        cb = self.safe(on_click, "notify:on_click") if on_click is not None else None
        self._host.notify(self.name, title, text, cb, level=level, alive=lambda: self._alive)

    # ---- システムメッセージ
    def on_native(self, msg: int, handler: Callable[[int, int], None]) -> None:
        self._host.native.subscribe(self.name, msg, self.safe(handler, f"native:{msg:#06x}"))

    def hidden_hwnd(self) -> int:
        return self._host.native.hwnd

    # ---- foreground
    def foreground(self) -> ForegroundInfo:
        mode = str(self._host.settings.host().get("fullscreen_detection", "rect"))
        return query_foreground(self.game_processes(), mode)

    # ---- イベント
    def emit(self, event: str, payload: Mapping[str, Any]) -> None:
        self._host.events.emit(self.name, event, payload)

    def on(self, event: str, handler: Callable[[Mapping[str, Any]], None]) -> None:
        self._host.events.on(self.name, event, self.safe(handler, f"event:{event}"))

    # ---- スレッド・タイマー(拡張: どちらも safe を通す)
    def call_soon(self, fn: Callable[[], None]) -> None:
        """任意のスレッドから、メインスレッドで fn を実行する。"""
        if self._alive:
            self._invoker.call.emit(self.safe(fn, getattr(fn, "__qualname__", "call_soon")))

    def start_timer(self, interval_ms: int, callback: Callable[[], None], *, single_shot: bool = False) -> QTimer:
        t = QTimer()
        t.setSingleShot(single_shot)
        t.setInterval(max(0, int(interval_ms)))
        t.timeout.connect(self.safe(callback, f"timer:{getattr(callback, '__qualname__', 'cb')}"))
        t.start()
        self._timers.append(t)
        return t

    # ---- 拡張: GUI 連携
    def dpi_awareness(self) -> str:
        return self._host.dpi_awareness

    def window_parent(self) -> QWidget | None:
        return self._host.main_window_if_visible()

    def show_page(self) -> None:
        """Control Center を開き、このモジュールの画面を表示する。"""
        self._host.show_window(self.name)

    def request_restart(self) -> None:
        QTimer.singleShot(0, lambda: self._host.loader.restart(self.name))

    def refresh_page(self) -> None:
        """Control Center のモジュール画面の再生成を頼む(設定の外部変更後など)。"""
        self._host.module_status_changed(self.name)
