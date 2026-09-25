# ホイール対策(UX-1)。ページをスクロールしている途中で、下に来たスピンボックス・コンボボックス・スライダーの値が
# 勝手に変わるのを防ぐ。アプリ全体のイベントフィルタで、フォーカスの無いこれらの部品へのホイールを親のスクロール領域へ回す。
# ホイールでフォーカスが移らないよう、これらの部品の WheelFocus は StrongFocus(クリック・Tab)に下げる。
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt
from PySide6.QtWidgets import QAbstractScrollArea, QAbstractSlider, QAbstractSpinBox, QComboBox, QScrollBar, QWidget

_GUARDED = (QAbstractSpinBox, QComboBox, QAbstractSlider)


def _guarded(obj: QObject) -> bool:
    return isinstance(obj, _GUARDED) and not isinstance(obj, QScrollBar)


class WheelGuard(QObject):
    def eventFilter(self, obj: QObject, e: QEvent) -> bool:  # noqa: N802
        try:
            t = e.type()
            if t == QEvent.Type.Polish and _guarded(obj):
                w: Any = obj
                if w.focusPolicy() == Qt.FocusPolicy.WheelFocus:
                    w.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                return False
            if t != QEvent.Type.Wheel or not _guarded(obj):
                return False
            widget: Any = obj
            if widget.hasFocus():
                return False  # クリックしてから回すのは値の変更として扱う
            area = _scroll_parent(widget)
            if area is not None:
                QCoreApplication.sendEvent(area.viewport(), e)  # ページのスクロールとして渡す
            return True
        except Exception:  # noqa: BLE001 - イベントフィルタから例外を漏らさない
            return False


def _scroll_parent(w: QWidget) -> QAbstractScrollArea | None:
    p = w.parentWidget()
    while p is not None:
        if isinstance(p, QAbstractScrollArea):
            return p
        p = p.parentWidget()
    return None


_guard: WheelGuard | None = None


def install(app: QCoreApplication) -> WheelGuard:
    """アプリ全体に1つだけ入れる(何度呼んでもよい)。"""
    global _guard
    if _guard is None:
        _guard = WheelGuard()
        app.installEventFilter(_guard)
    return _guard
