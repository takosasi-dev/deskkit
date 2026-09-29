# 入力欄の窓(枠なし・最前面。J-2)と「書きました」の印(J-7)。窓の題は常に「JotDrop」で、本文を入れない(J-15)。
# MemoEdit は IME の確定前の Enter・確定直後 100ms 以内の Enter・押しっぱなしの Enter では送らない(J-6)。
# 前面の窓を変える制限の回避策(偽のキー入力など)は使わない。前面になれなければ案内を出すだけ(J-3・INV-4)。
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QEasingCurve, QEvent, QPoint, QPropertyAnimation, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QGuiApplication, QInputMethodEvent, QKeyEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from deskkit.ui import theme as T
from deskkit.ui import widgets as W
from deskkit.ui.theme import G

TITLE = "JotDrop"
PLACEHOLDER = "思いついたことを1行で。Enter で書きます"
COMMIT_GUARD_S = 0.1          # J-6: 確定の直後 100ms 以内の Enter は送らない
FOCUS_CHECK_MS = 200          # FR-4
DONE_MARK_MS = 1500           # J-7
PANEL_W = 620
SHADOW = 22


class MemoEdit(QLineEdit):
    """1行の入力。Enter の扱いだけを変える(J-6)。clock はテストで差し替える。"""

    submit = Signal()
    escape = Signal()
    undo_key = Signal()          # 空のときの Ctrl+Z(FR-15)

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self._clock = clock
        self.preedit = ""
        self.last_commit = -1e9

    def inputMethodEvent(self, e: QInputMethodEvent) -> None:  # noqa: N802
        try:
            self.preedit = e.preeditString()
            if e.commitString():
                self.last_commit = self._clock()
        except Exception:  # noqa: BLE001 - 判定の失敗で入力を止めない
            self.preedit = ""
        super().inputMethodEvent(e)

    def enter_allowed(self, e: QKeyEvent) -> bool:
        if self.preedit:
            return False           # 変換中
        if e.isAutoRepeat():
            return False           # 押しっぱなし
        return self._clock() - self.last_commit >= COMMIT_GUARD_S

    def keyPressEvent(self, e: QKeyEvent) -> None:  # noqa: N802
        try:
            key = e.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if self.enter_allowed(e):
                    self.submit.emit()
                e.accept()
                return
            if key == Qt.Key.Key_Escape:
                self.escape.emit()
                e.accept()
                return
            if (key == Qt.Key.Key_Z and e.modifiers() == Qt.KeyboardModifier.ControlModifier and not self.text()
                    and not self.preedit):
                self.undo_key.emit()
                e.accept()
                return
        except Exception:  # noqa: BLE001 - Qt の仮想メソッドから例外を漏らさない
            return
        super().keyPressEvent(e)


def pick_screen(rect: tuple[int, int, int, int] | None) -> Any:
    """前面の窓の中心があるモニタ。分からなければマウスのあるモニタ(J-2)。"""
    if rect is not None:
        cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
        for s in QGuiApplication.screens():
            g = s.geometry()
            dpr = s.devicePixelRatio() or 1.0
            native = QRect(g.x(), g.y(), int(g.width() * dpr), int(g.height() * dpr))
            if native.contains(cx, cy):
                return s
    return QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()


class MemoPopup(QWidget):
    """入力欄の窓。start() で作って隠しておき、open_at() で出す。閉じる理由は closed(reason) で知らせる。"""

    submitted = Signal(str)          # 本文(整える前)
    closed = Signal(str)             # "esc" / "outside"
    undo_requested = Signal()
    pending_clicked = Signal()

    def __init__(self, accent: str, *, foreground: Callable[[], int] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.accent = accent
        self._foreground = foreground
        self.setWindowTitle(TITLE)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self._was_active = False
        self._suppress_outside = False
        self.focus_ok: bool | None = None
        self._anim: QPropertyAnimation | None = None
        self._build(clock)

    def _build(self, clock: Callable[[], float]) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW, SHADOW - 6, SHADOW, SHADOW + 6)
        panel = QFrame()
        panel.setObjectName("JotPanel")
        panel.setStyleSheet(f"QFrame#JotPanel {{ background: {T.BG1}; border: 1px solid {T.BORDER_HI}; border-radius: 16px; }}")
        W.shadow(panel, 44, 70 if T.IS_LIGHT else 160, 12)
        outer.addWidget(panel)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(16, 14, 16, 10)
        lay.setSpacing(8)
        box = QFrame()
        box.setObjectName("JotBox")
        box.setStyleSheet(f"QFrame#JotBox {{ background: {T.SURFACE}; border: 1px solid {T.alpha(self.accent, 0.55)};"
                          f" border-radius: 12px; }}")
        row = QHBoxLayout(box)
        row.setContentsMargins(12, 4, 10, 4)
        row.setSpacing(10)
        row.addWidget(W.Glyph(G.EDIT, 18, self.accent))
        self.edit = MemoEdit(clock)
        self.edit.setPlaceholderText(PLACEHOLDER)
        self.edit.setStyleSheet("QLineEdit { background: transparent; border: none; font-size: 18px; padding: 8px 2px; }")
        row.addWidget(self.edit, 1)
        lay.addWidget(box)
        # 前面になれなかったときの案内(J-3)。クリックすれば前面になる
        self.warn = QLabel(f"{G.WARNING}  ここをクリックしてから書いてください")
        f = T.ui_font(15)
        f.setBold(True)
        f.setFamilies([*T.UI_FAMILIES[:2], T.icon_family(), *T.UI_FAMILIES[2:]])
        self.warn.setFont(f)
        self.warn.setStyleSheet(f"color: {T.WARN}; background: {T.alpha(T.WARN, 0.12)}; border: 1px solid {T.alpha(T.WARN, 0.4)};"
                                f" border-radius: 10px; padding: 8px 12px;")
        self.warn.setVisible(False)
        lay.addWidget(self.warn)
        # 確かめ・お知らせの行(J-17・FR-5・FR-7)
        self.msg = W.label("", "Dim", wrap=True)
        self.msg.setVisible(False)
        lay.addWidget(self.msg)
        # 小さな行: 書き込み先のファイル名・取り消し・預かり中(FR-3)
        info = QHBoxLayout()
        info.setSpacing(12)
        self.target = W.label("", "Mute")
        info.addWidget(self.target)
        info.addStretch(1)
        self.undo_link = QLabel()
        self.undo_link.setTextFormat(Qt.TextFormat.RichText)
        self.undo_link.linkActivated.connect(lambda _h: self._safe(self.undo_requested.emit))
        self.undo_link.setVisible(False)
        info.addWidget(self.undo_link)
        self.pending_link = QLabel()
        self.pending_link.setTextFormat(Qt.TextFormat.RichText)
        self.pending_link.linkActivated.connect(lambda _h: self._safe(self.pending_clicked.emit))
        self.pending_link.setVisible(False)
        info.addWidget(self.pending_link)
        lay.addLayout(info)
        self.edit.submit.connect(lambda: self._safe(lambda: self.submitted.emit(self.edit.text())))
        self.edit.escape.connect(lambda: self._safe(lambda: self.closed.emit("esc")))
        self.edit.undo_key.connect(lambda: self._safe(self.undo_requested.emit))
        self.resize(PANEL_W + SHADOW * 2, self.sizeHint().height())

    @staticmethod
    def _safe(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 - シグナルから例外を Qt へ漏らさない(本文を含みうるので文は書かない)
            import logging

            logging.getLogger("deskkit.jotdrop").warning("popup handler failed")

    # ------------------------------------------------------------ 表示
    def _link(self, text: str) -> str:
        return f'<a href="#" style="color:{self.accent}; text-decoration:none;">{text}</a>'

    def set_info(self, target_name: str, target_full: str, undo_label: str | None, pending: int) -> None:
        self.target.setText(target_name)
        self.target.setToolTip(target_full)
        self.undo_link.setVisible(bool(undo_label))
        if undo_label:
            self.undo_link.setText(self._link(f"{undo_label} の1行を取り消す(Ctrl+Z)"))
        self.pending_link.setVisible(pending > 0)
        if pending:
            self.pending_link.setText(self._link(f"書けていないメモが {pending} 件あります"))

    def show_message(self, text: str, level: str = "info") -> None:
        color = {"warn": T.WARN, "error": T.DANGER, "ok": T.SUCCESS}.get(level, T.TEXT_DIM)
        self.msg.setStyleSheet(f"color: {color};")
        self.msg.setText(text)
        self.msg.setVisible(bool(text))
        self.adjustSize()

    def open_at(self, rect: tuple[int, int, int, int] | None, text: str) -> None:
        self.show_message("")
        self.warn.setVisible(False)
        self.focus_ok = None
        self._was_active = False
        self._suppress_outside = False
        self.edit.preedit = ""
        self.edit.setText(text)
        self.adjustSize()
        screen = pick_screen(rect)
        ag = screen.availableGeometry() if screen is not None else QRect(0, 0, 1280, 720)
        w, h = self.width(), self.height()
        x = ag.x() + (ag.width() - w) // 2
        y = ag.y() + max(0, ag.height() // 4 - h // 2)   # 縦は上から 1/4(J-2)
        self.move(QPoint(x, y))
        self.setWindowOpacity(1.0)
        self.show()
        self.raise_()
        self.activateWindow()
        self.edit.setFocus()
        self.edit.end(False)
        QTimer.singleShot(FOCUS_CHECK_MS, lambda: self._safe(self.check_focus))

    def is_foreground(self) -> bool:
        fg = self._foreground() if self._foreground is not None else 0
        return bool(fg) and fg == int(self.winId())

    def check_focus(self) -> None:
        """FR-4: 200ms 後に、自分の窓が前面で入力にフォーカスがあるか。無ければ案内を出す(J-3)。"""
        if not self.isVisible():
            return
        ok = self.is_foreground() and self.edit.hasFocus()
        self.focus_ok = ok
        self.warn.setVisible(not ok)
        self.adjustSize()

    def hide_now(self) -> None:
        self._suppress_outside = True
        self.hide()

    # ------------------------------------------------------------ 窓の出来事
    def changeEvent(self, e: QEvent) -> None:  # noqa: N802
        try:
            if e.type() == QEvent.Type.ActivationChange:
                if self.isActiveWindow():
                    self._was_active = True
                    if self.warn.isVisible():
                        self.warn.setVisible(False)
                        self.edit.setFocus()
                        self.adjustSize()
                elif self._was_active and self.isVisible() and not self._suppress_outside:
                    self.closed.emit("outside")   # 窓の外をクリックした(J-4: 戻さない)
        except Exception:  # noqa: BLE001
            pass
        super().changeEvent(e)

    def mousePressEvent(self, e: Any) -> None:  # noqa: N802
        try:
            self.activateWindow()
            self.edit.setFocus()
        except Exception:  # noqa: BLE001
            pass
        super().mousePressEvent(e)


class DoneMark(QWidget):
    """「書きました」の小さな印。フォーカスを取らず、クリックを受けず、本文を出さない(J-7)。"""

    def __init__(self, accent: str) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.WindowTransparentForInput)
        self.setWindowTitle(TITLE)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 12)
        self.label = QLabel(f"{G.CHECK}  書きました")
        f = T.ui_font(14)
        f.setBold(True)
        f.setFamilies([*T.UI_FAMILIES[:2], T.icon_family(), *T.UI_FAMILIES[2:]])
        self.label.setFont(f)
        self.label.setStyleSheet(f"color: {T.ON_ACCENT}; background: {accent}; border-radius: 16px; padding: 8px 18px;")
        W.shadow(self.label, 24, 60 if T.IS_LIGHT else 140, 6)
        lay.addWidget(self.label)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fade)
        self._anim: QPropertyAnimation | None = None

    def flash(self, center: QPoint) -> None:
        self.adjustSize()
        self.move(center - QPoint(self.width() // 2, self.height() // 2))
        self.setWindowOpacity(1.0)
        self.show()
        self._timer.start(DONE_MARK_MS)

    def _fade(self) -> None:
        a = QPropertyAnimation(self, b"windowOpacity", self)
        a.setDuration(220)
        a.setStartValue(1.0)
        a.setEndValue(0.0)
        a.setEasingCurve(QEasingCurve.Type.OutCubic)
        a.finished.connect(self.hide)
        self._anim = a
        a.start()
