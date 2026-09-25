# host が1枚だけ持つ隠しトップレベルウィンドウ(message-only ではない)。システムメッセージを購読者へ配る。
# nativeEvent 内の例外は必ず握りつぶしてログに出す(2026-09-02 の Qt 仮想メソッド内例外でのクラッシュ対策)。
# WM_CLIPBOARDUPDATE の購読者がいるときだけ AddClipboardFormatListener を呼ぶ(D-4)。
from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from deskkit import win32

log = logging.getLogger("deskkit.host.nativewin")
NativeHandler = Callable[[int, int], None]


class NativeWindow(QWidget):
    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Tool)
        self.setWindowTitle("DeskKit hidden")
        self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        # winId() の生成中にも nativeEvent が呼ばれるので、属性は先に用意する
        self._subs: dict[int, list[tuple[str, NativeHandler]]] = {}
        self._clip_listening = False
        self.clipboard_listener_calls = 0  # selftest(AC-6)用の観測値
        self.hotkey_sink: Callable[[int], None] | None = None
        self.taskbar_created_sink: Callable[[], None] | None = None
        self._taskbar_created = win32.user32.RegisterWindowMessageW("TaskbarCreated")
        self._hwnd = 0
        self._hwnd = int(self.winId())  # ネイティブウィンドウを作る(表示はしない)

    @property
    def hwnd(self) -> int:
        return self._hwnd

    def subscribe(self, owner: str, msg: int, handler: NativeHandler) -> None:
        self._subs.setdefault(msg, []).append((owner, handler))
        if msg == win32.WM_CLIPBOARDUPDATE:
            self._update_clipboard_listener()

    def unsubscribe_owner(self, owner: str) -> None:
        for msg in list(self._subs):
            self._subs[msg] = [(o, h) for (o, h) in self._subs[msg] if o != owner]
            if not self._subs[msg]:
                del self._subs[msg]
        self._update_clipboard_listener()

    def subscriber_count(self, msg: int) -> int:
        return len(self._subs.get(msg, []))

    def _update_clipboard_listener(self) -> None:
        want = self.subscriber_count(win32.WM_CLIPBOARDUPDATE) > 0
        if want and not self._clip_listening:
            self.clipboard_listener_calls += 1
            ok = win32.user32.AddClipboardFormatListener(self._hwnd)
            self._clip_listening = bool(ok)
            log.info("AddClipboardFormatListener ok=%s", bool(ok))
        elif not want and self._clip_listening:
            win32.user32.RemoveClipboardFormatListener(self._hwnd)
            self._clip_listening = False
            log.info("RemoveClipboardFormatListener")

    def nativeEvent(self, event_type, message):  # type: ignore[no-untyped-def]  # noqa: N802
        try:
            msg = win32.MSG.from_address(int(message))
            m = msg.message
            if m == win32.WM_HOTKEY and self.hotkey_sink is not None:
                self.hotkey_sink(int(msg.wParam))
            elif m == self._taskbar_created and self.taskbar_created_sink is not None:
                self.taskbar_created_sink()
            subs = self._subs.get(m)
            if subs:
                wp, lp = int(msg.wParam or 0), int(msg.lParam or 0)
                for _owner, handler in list(subs):
                    handler(wp, lp)  # handler は ModuleContext.safe で包まれている
        except Exception:  # noqa: BLE001 - ここから例外を Qt へ漏らさない(INV-5)
            log.exception("nativeEvent で例外(握りつぶしました)")
        return False, 0

    def close_native(self) -> None:
        if self._clip_listening:
            win32.user32.RemoveClipboardFormatListener(self._hwnd)
            self._clip_listening = False
