# 画面右下に積み上がる通知トースト。フォーカスを奪わず(WA_ShowWithoutActivating)、スライド+フェードで出入りする。
# クリックで on_click を呼ぶ。host は通知の種類だけをログに書き、本文はここで表示するだけ(INV-7)。
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QEasingCurve, QParallelAnimationGroup, QPoint, QPropertyAnimation, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from deskkit.ui import theme as T
from deskkit.ui.theme import G
from deskkit.ui.widgets import Glyph, label

_LEVEL = {"info": (T.INFO, G.INFO), "ok": (T.SUCCESS, G.CHECK), "warn": (T.WARN, G.WARNING), "error": (T.DANGER, G.ERROR)}


class Toast(QWidget):
    WIDTH = 380

    def __init__(self, manager: ToastManager, source: str, accent: str, title: str, text: str,
                 on_click: Callable[[], Any] | None, level: str, glyph: str | None) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._mgr = manager
        self._on_click = on_click
        self._accent = QColor(accent)
        lvl_color, lvl_glyph = _LEVEL.get(level, _LEVEL["info"])
        self._level_color = QColor(lvl_color if level != "info" else accent)
        self.setFixedWidth(self.WIDTH)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 14, 14, 14)
        lay.setSpacing(12)
        g = Glyph(glyph or lvl_glyph, 17, self._level_color.name())
        g.setFixedSize(36, 36)
        g.setStyleSheet(f"color: {self._level_color.name()}; background: {T.alpha(self._level_color.name(), 0.15)}; border-radius: 10px;")
        lay.addWidget(g, 0, Qt.AlignmentFlag.AlignTop)
        box = QVBoxLayout()
        box.setSpacing(2)
        src = label(source.upper(), "Eyebrow")
        src.setStyleSheet(f"color: {accent}; font-size: 10px; font-weight: 700;")
        box.addWidget(src)
        t = label(title, "H3", wrap=True)
        box.addWidget(t)
        if text:
            d = QLabel(text)
            d.setWordWrap(True)
            d.setStyleSheet(f"color: {T.TEXT_DIM}; font-size: 12px;")
            box.addWidget(d)
        if on_click is not None:
            hint = label("クリックして開く", "Mute")
            hint.setStyleSheet(f"color: {accent}; font-size: 11px;")
            box.addWidget(hint)
        lay.addLayout(box, 1)
        close = QLabel(G.CLOSE)
        close.setFont(T.icon_font(10))
        close.setStyleSheet(f"color: {T.TEXT_MUTE};")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.mousePressEvent = lambda _e: self.dismiss()  # type: ignore[method-assign]
        lay.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        self.setCursor(Qt.CursorShape.PointingHandCursor if on_click else Qt.CursorShape.ArrowCursor)
        self.adjustSize()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)
        self._timer.start(7000 if level in ("warn", "error") else 5000)
        self._closing = False
        self._progress = 1.0

    def paintEvent(self, _e: Any) -> None:  # noqa: N802
        try:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            r = self.rect().adjusted(6, 6, -6, -6)
            for i in range(6, 0, -1):  # 柔らかい影
                sh = QPainterPath()
                sh.addRoundedRect(r.adjusted(-i, -i + 3, i, i + 3), 16 + i, 16 + i)
                p.fillPath(sh, QColor(0, 0, 0, 10))
            path = QPainterPath()
            path.addRoundedRect(r, 14, 14)
            p.fillPath(path, QColor(T.BG1))
            p.setPen(QColor(T.BORDER_HI))
            p.drawPath(path)
            bar = QPainterPath()
            bar.addRoundedRect(r.left() + 1, r.top() + 12, 3, r.height() - 24, 1.5, 1.5)
            p.fillPath(bar, self._level_color)
        except Exception:  # noqa: BLE001 - 描画で落とさない
            pass

    def enterEvent(self, e: Any) -> None:  # noqa: N802
        self._timer.stop()
        super().enterEvent(e)

    def leaveEvent(self, e: Any) -> None:  # noqa: N802
        if not self._closing:
            self._timer.start(2500)
        super().leaveEvent(e)

    def mousePressEvent(self, e: Any) -> None:  # noqa: N802
        try:
            if e.button() == Qt.MouseButton.LeftButton and self._on_click is not None:
                cb = self._on_click
                self._on_click = None
                cb()
        except Exception:  # noqa: BLE001
            pass
        self.dismiss()

    def dismiss(self) -> None:
        if self._closing:
            return
        self._closing = True
        a = QPropertyAnimation(self, b"windowOpacity", self)
        a.setDuration(180)
        a.setStartValue(self.windowOpacity())
        a.setEndValue(0.0)
        a.finished.connect(self._finish)
        a.start()

    def _finish(self) -> None:
        self._mgr.remove(self)
        self.close()


class ToastManager:
    MAX = 4

    def __init__(self) -> None:
        self._toasts: list[Toast] = []

    def show(self, source: str, accent: str, title: str, text: str, on_click: Callable[[], Any] | None = None,
             level: str = "info", glyph: str | None = None) -> None:
        t = Toast(self, source, accent, title, text, on_click, level, glyph)
        self._toasts.insert(0, t)
        while len(self._toasts) > self.MAX:
            self._toasts[-1].dismiss()
            self._toasts.pop()
        area = self._area()
        start = QPoint(area.right() - t.width() - 12, area.bottom() - t.height() - 12)
        t.move(start + QPoint(40, 0))
        t.setWindowOpacity(0.0)
        t.show()
        grp = QParallelAnimationGroup(t)
        a1 = QPropertyAnimation(t, b"pos", t)
        a1.setDuration(320)
        a1.setStartValue(start + QPoint(40, 0))
        a1.setEndValue(start)
        a1.setEasingCurve(QEasingCurve.Type.OutCubic)
        a2 = QPropertyAnimation(t, b"windowOpacity", t)
        a2.setDuration(240)
        a2.setStartValue(0.0)
        a2.setEndValue(1.0)
        grp.addAnimation(a1)
        grp.addAnimation(a2)
        grp.start(QParallelAnimationGroup.DeletionPolicy.DeleteWhenStopped)
        self._relayout(skip=t)

    def remove(self, t: Toast) -> None:
        if t in self._toasts:
            self._toasts.remove(t)
        self._relayout()

    def _area(self) -> QRect:
        scr = QGuiApplication.primaryScreen()
        return scr.availableGeometry() if scr else QRect(0, 0, 1280, 720)

    def _relayout(self, skip: Toast | None = None) -> None:
        try:
            self._relayout_inner(skip)
        except RuntimeError:  # 破棄済みのトーストが混ざっていたら取り除く
            import shiboken6

            self._toasts = [t for t in self._toasts if shiboken6.isValid(t)]

    def _relayout_inner(self, skip: Toast | None) -> None:
        area = self._area()
        y = area.bottom() - 12
        for t in self._toasts:
            y -= t.height()
            target = QPoint(area.right() - t.width() - 12, y)
            y -= 2
            if t is skip:
                continue
            a = QPropertyAnimation(t, b"pos", t)
            a.setDuration(220)
            a.setEndValue(target)
            a.setEasingCurve(QEasingCurve.Type.OutCubic)
            a.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
