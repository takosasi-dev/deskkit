# DeskKit 共通の GUI 部品(アニメーション付きトグル・カード・状態ピル・ヒーロー見出し・文字列リスト編集・
# ホットキー入力欄・セグメント切替・確認ダイアログ・フェード切替スタック)。各モジュールの画面もこれを使う。
# ホットキー入力はウィンドウ内のキーイベントだけを読む(グローバルなフックは使わない。C-3)。
from __future__ import annotations

import ctypes
from collections.abc import Callable, Sequence
from typing import Any

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QEvent,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QBrush, QColor, QFont, QKeyEvent, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QCheckBox,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from deskkit.ui import theme as T
from deskkit.ui.theme import G


# ------------------------------------------------------------------ 基本
def label(text: str, role: str | None = None, *, wrap: bool = False) -> QLabel:
    lb = QLabel(text)
    if role:
        lb.setObjectName(role)
    lb.setWordWrap(wrap)
    if wrap:
        lb.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    return lb


def divider() -> QFrame:
    f = QFrame()
    f.setObjectName("Divider")
    return f


def hbox(*items: QWidget | int | None, spacing: int = 8, margins: tuple[int, int, int, int] = (0, 0, 0, 0)) -> QHBoxLayout:
    """items: ウィジェット / 数値(=固定スペース) / None(=伸縮)。"""
    lay = QHBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(*margins)
    for it in items:
        if it is None:
            lay.addStretch(1)
        elif isinstance(it, int):
            lay.addSpacing(it)
        else:
            lay.addWidget(it)
    return lay


def vbox(*items: QWidget | int | None, spacing: int = 8, margins: tuple[int, int, int, int] = (0, 0, 0, 0)) -> QVBoxLayout:
    lay = QVBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(*margins)
    for it in items:
        if it is None:
            lay.addStretch(1)
        elif isinstance(it, int):
            lay.addSpacing(it)
        else:
            lay.addWidget(it)
    return lay


class Glyph(QLabel):
    """Segoe Fluent Icons の1文字アイコン。"""

    def __init__(self, glyph: str, px: int = 16, color: str | None = None) -> None:
        super().__init__(glyph)
        self._px = px
        self.setFont(T.icon_font(px))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedSize(px + 8, px + 8)
        self.setStyleSheet(f"color: {color};" if color else "")

    def setStyleSheet(self, css: str) -> None:  # noqa: N802
        # アプリ全体の QSS の font-size に負けないよう、字形のフォントを必ず先頭に付ける
        super().setStyleSheet(f"font-family: '{T.icon_family()}'; font-size: {self._px}px; {css}")

    def set_color(self, color: str) -> None:
        self.setStyleSheet(f"color: {color};")


def button(text: str, kind: str = "secondary", glyph: str | None = None,
           on_click: Callable[[], Any] | None = None, tooltip: str | None = None) -> QPushButton:
    b = QPushButton((glyph + "  " if glyph else "") + text)
    if glyph:
        f = T.ui_font(13, QFont.Weight.DemiBold)
        f.setFamilies([*T.UI_FAMILIES[:2], T.icon_family(), *T.UI_FAMILIES[2:]])
        b.setFont(f)
    b.setProperty("kind", kind)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if tooltip:
        b.setToolTip(tooltip)
    if on_click is not None:
        b.clicked.connect(lambda _=False: on_click())
    return b


def icon_button(glyph: str, tooltip: str, on_click: Callable[[], Any] | None = None, kind: str = "ghost") -> QPushButton:
    b = QPushButton(glyph)
    b.setFont(T.icon_font(14))
    b.setProperty("kind", kind)
    b.setFixedSize(34, 34)
    b.setToolTip(tooltip)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setStyleSheet(f"padding: 0; font-family: '{T.icon_family()}'; font-size: 14px;")
    if on_click is not None:
        b.clicked.connect(lambda _=False: on_click())
    return b


def shadow(widget: QWidget, blur: int = 28, alpha: int = 110, dy: int = 8) -> QGraphicsDropShadowEffect:
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(blur)
    eff.setOffset(0, dy)
    eff.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(eff)
    return eff


# ------------------------------------------------------------------ トグルスイッチ
class ToggleSwitch(QAbstractButton):
    """つまみが滑るトグル。toggled(bool) を使う。"""

    def __init__(self, checked: bool = False, accent: str = T.ACCENT, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._accent = QColor(accent)
        self._pos = 1.0 if checked else 0.0
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)
        self.setFixedSize(46, 26)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(46, 26)

    def _get_pos(self) -> float:
        return self._pos

    def _set_pos(self, v: float) -> None:
        self._pos = v
        self.update()

    knob = Property(float, _get_pos, _set_pos)

    def _animate(self, checked: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def set_checked_silent(self, checked: bool) -> None:
        self.blockSignals(True)
        self.setChecked(checked)
        self.blockSignals(False)
        self._pos = 1.0 if checked else 0.0
        self.update()

    def paintEvent(self, _e: object) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(1, 1, self.width() - 2, self.height() - 2)
        off = QColor(T.SURFACE3)
        on = self._accent
        t = self._pos
        track = QColor(
            int(off.red() + (on.red() - off.red()) * t),
            int(off.green() + (on.green() - off.green()) * t),
            int(off.blue() + (on.blue() - off.blue()) * t),
        )
        if not self.isEnabled():
            track.setAlpha(90)
        p.setPen(QPen(QColor(T.BORDER_HI) if t < 0.5 else track, 1))
        p.setBrush(track)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        d = r.height() - 6
        x = r.left() + 3 + (r.width() - d - 6) * t
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 60))
        p.drawEllipse(QRectF(x, r.top() + 4, d, d))
        p.setBrush(QColor("#FFFFFF") if t > 0.5 else QColor(T.TEXT_DIM))
        p.drawEllipse(QRectF(x, r.top() + 3, d, d))


# ------------------------------------------------------------------ 状態ピル
_PILL_COLORS = {"ok": T.SUCCESS, "warn": T.WARN, "error": T.DANGER, "info": T.INFO, "off": T.TEXT_MUTE, "accent": T.ACCENT}


class StatusPill(QLabel):
    def __init__(self, text: str = "", kind: str = "off") -> None:
        super().__init__()
        self.set_state(kind, text)

    def set_state(self, kind: str, text: str) -> None:
        c = _PILL_COLORS.get(kind, kind if kind.startswith("#") else T.TEXT_MUTE)
        self.setText(f"●  {text}")
        self.setStyleSheet(
            f"QLabel {{ color: {c}; background: {T.alpha(c, 0.12)}; border: 1px solid {T.alpha(c, 0.30)};"
            f" border-radius: 11px; padding: 3px 10px; font-size: 12px; font-weight: 600; }}"
        )


# ------------------------------------------------------------------ カード
class Card(QFrame):
    """角丸・影付きの面。ホバーで影が深くなる。body に中身を積む。"""

    def __init__(self, title: str | None = None, subtitle: str | None = None, glyph: str | None = None,
                 accent: str | None = None, hover: bool = False, padding: int = 18) -> None:
        super().__init__()
        self.setObjectName("Card")
        self._shadow = shadow(self, 24, 70, 6)
        self._hover = hover
        self._anim = QPropertyAnimation(self._shadow, b"blurRadius", self)
        self._anim.setDuration(160)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(padding, padding - 2, padding, padding)
        outer.setSpacing(12)
        self.header = QHBoxLayout()
        self.header.setSpacing(10)
        if glyph:
            gl = Glyph(glyph, 16, accent or T.ACCENT)
            gl.setStyleSheet(f"color: {accent or T.ACCENT}; background: {T.alpha(accent or T.ACCENT, 0.12)}; border-radius: 8px;")
            gl.setFixedSize(32, 32)
            self.header.addWidget(gl)
        if title:
            tbox = QVBoxLayout()
            tbox.setSpacing(1)
            tbox.addWidget(label(title, "H3"))
            if subtitle:
                tbox.addWidget(label(subtitle, "Mute", wrap=True))
            self.header.addLayout(tbox, 1)
            outer.addLayout(self.header)
        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        outer.addLayout(self.body)

    def add(self, w: QWidget) -> QWidget:
        self.body.addWidget(w)
        return w

    def add_layout(self, lay: Any) -> None:
        self.body.addLayout(lay)

    def add_header_widget(self, w: QWidget) -> None:
        self.header.addWidget(w)

    def enterEvent(self, e: Any) -> None:  # noqa: N802
        if self._hover:
            self._anim.stop()
            self._anim.setEndValue(44)
            self._anim.start()
        super().enterEvent(e)

    def leaveEvent(self, e: Any) -> None:  # noqa: N802
        if self._hover:
            self._anim.stop()
            self._anim.setEndValue(24)
            self._anim.start()
        super().leaveEvent(e)


class Hero(QFrame):
    """モジュール画面の先頭に置く、アクセント色のグラデーション見出し。"""

    def __init__(self, title: str, tagline: str, glyph: str, accent: str) -> None:
        super().__init__()
        self._accent = QColor(accent)
        self.setMinimumHeight(128)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 22)
        lay.setSpacing(18)
        ic = Glyph(glyph, 30, T.HERO_GLYPH)
        ic.setFixedSize(62, 62)
        ic.setStyleSheet(f"color: {T.HERO_GLYPH}; background: {T.alpha(accent, 0.28)}; border: 1px solid {T.alpha(T.HERO_GLYPH, 0.18)}; border-radius: 16px;")
        lay.addWidget(ic)
        tb = QVBoxLayout()
        tb.setSpacing(4)
        self.title_label = label(title, "H1")
        tb.addWidget(self.title_label)
        self.tag = label(tagline, "Dim", wrap=True)
        tb.addWidget(self.tag)
        self.pills = QHBoxLayout()
        self.pills.setSpacing(8)
        self.pills.addStretch(1)
        tb.addLayout(self.pills)
        lay.addLayout(tb, 1)
        self.right = QVBoxLayout()
        self.right.setSpacing(8)
        self.right.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
        lay.addLayout(self.right)

    def set_title(self, title: str) -> None:
        self.title_label.setText(title)

    def add_pill(self, pill: QWidget) -> None:
        self.pills.insertWidget(self.pills.count() - 1, pill)

    def add_action(self, w: QWidget) -> None:
        self.right.addWidget(w)

    def paintEvent(self, _e: object) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 18, 18)
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        a = QColor(self._accent)
        a.setAlpha(70)
        b = QColor(self._accent)
        b.setAlpha(14)
        g.setColorAt(0.0, a)
        g.setColorAt(0.55, b)
        g.setColorAt(1.0, QColor(T.SURFACE))
        p.fillPath(path, QColor(T.SURFACE))
        p.fillPath(path, QBrush(g))
        # 右上の光彩
        glow = QLinearGradient(r.topRight(), r.center())
        c = QColor(self._accent)
        c.setAlpha(40)
        glow.setColorAt(0, c)
        glow.setColorAt(1, QColor(0, 0, 0, 0))
        p.fillPath(path, QBrush(glow))
        edge = QColor(self._accent)
        edge.setAlpha(60)
        p.setPen(QPen(edge, 1))
        p.drawPath(path)


# ------------------------------------------------------------------ 数値タイル
class StatTile(QFrame):
    def __init__(self, title: str, value: str, glyph: str, accent: str = T.ACCENT) -> None:
        super().__init__()
        self.setObjectName("Card")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        g = Glyph(glyph, 18, accent)
        g.setFixedSize(40, 40)
        g.setStyleSheet(f"color: {accent}; background: {T.alpha(accent, 0.13)}; border-radius: 12px;")
        lay.addWidget(g)
        box = QVBoxLayout()
        box.setSpacing(0)
        self.value = label(value, "H2")
        box.addWidget(self.value)
        box.addWidget(label(title, "Mute"))
        lay.addLayout(box, 1)

    def set_value(self, v: str) -> None:
        self.value.setText(v)


# ------------------------------------------------------------------ 設定行
class SettingRow(QWidget):
    """左に見出しと説明、右に操作部品。カードの中に縦に並べる。"""

    def __init__(self, title: str, description: str | None, control: QWidget | None, glyph: str | None = None) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(12)
        if glyph:
            lay.addWidget(Glyph(glyph, 15, T.TEXT_DIM), 0, Qt.AlignmentFlag.AlignTop)
        tb = QVBoxLayout()
        tb.setSpacing(2)
        tb.addWidget(label(title))
        if description:
            tb.addWidget(label(description, "Mute", wrap=True))
        lay.addLayout(tb, 1)
        if control is not None:
            lay.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)


# ------------------------------------------------------------------ セグメント切替
class Segmented(QFrame):
    """横並びの選択肢。選択中の背景がすべるように動く。changed(value)。"""

    changed = Signal(str)

    def __init__(self, options: Sequence[tuple[str, str]], value: str | None = None, accent: str = T.ACCENT) -> None:
        super().__init__()
        self.setObjectName("Inset")
        self._accent = accent
        self._values = [v for v, _ in options]
        self._buttons: list[QPushButton] = []
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        self._pill = QFrame(self)
        self._pill.setStyleSheet(f"background: {T.alpha(accent, 0.22)}; border: 1px solid {T.alpha(accent, 0.5)}; border-radius: 7px;")
        self._pill.lower()
        for v, text in options:
            b = QPushButton(text)
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(
                f"QPushButton {{ background: transparent; border: none; padding: 5px 12px; color: {T.TEXT_DIM}; font-weight: 600; }}"
                f"QPushButton:checked {{ color: {T.TEXT}; }}"
            )
            b.clicked.connect(lambda _=False, vv=v: self.set_value(vv, emit=True))
            lay.addWidget(b)
            self._buttons.append(b)
        self._anim = QPropertyAnimation(self._pill, b"geometry", self)
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._value = value if value in self._values else (self._values[0] if self._values else "")
        QTimer.singleShot(0, lambda: self.set_value(self._value, animate=False))

    def value(self) -> str:
        return self._value

    def set_value(self, v: str, *, emit: bool = False, animate: bool = True) -> None:
        if v not in self._values:
            return
        changed = v != self._value
        self._value = v
        i = self._values.index(v)
        for j, b in enumerate(self._buttons):
            b.setChecked(j == i)
        target = self._buttons[i].geometry()
        if animate and self._pill.geometry().width() > 0:
            self._anim.stop()
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self._pill.setGeometry(target)
        if emit and changed:
            self.changed.emit(v)

    def resizeEvent(self, e: Any) -> None:  # noqa: N802
        super().resizeEvent(e)
        if self._values:
            self._pill.setGeometry(self._buttons[self._values.index(self._value)].geometry())


# ------------------------------------------------------------------ 文字列リスト編集
class StringListEditor(QWidget):
    """exe 名・拡張子などの文字列リスト。changed(list) を出す。"""

    changed = Signal(list)

    def __init__(self, items: Sequence[str], placeholder: str = "追加する値", normalize: Callable[[str], str] | None = None,
                 height: int = 150, extra_buttons: Sequence[QPushButton] = ()) -> None:
        super().__init__()
        self._normalize = normalize or (lambda s: s.strip())
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.list = QListWidget()
        self.list.setFixedHeight(height)
        self.list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        lay.addWidget(self.list)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.returnPressed.connect(self._add)
        add = icon_button(G.ADD, "追加", self._add, kind="secondary")
        rm = icon_button(G.DELETE, "選択を削除", self._remove, kind="secondary")
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(self.edit, 1)
        row.addWidget(add)
        row.addWidget(rm)
        for b in extra_buttons:
            row.addWidget(b)
        lay.addLayout(row)
        self.set_items(items)

    def items(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def set_items(self, items: Sequence[str]) -> None:
        self.list.clear()
        for s in items:
            self.list.addItem(QListWidgetItem(s))

    def add_value(self, raw: str) -> None:
        v = self._normalize(raw)
        if v and v not in self.items():
            self.list.addItem(v)
            self.changed.emit(self.items())

    def _add(self) -> None:
        self.add_value(self.edit.text())
        self.edit.clear()

    def _remove(self) -> None:
        rows = sorted({self.list.row(i) for i in self.list.selectedItems()}, reverse=True)
        for r in rows:
            self.list.takeItem(r)
        if rows:
            self.changed.emit(self.items())


# ------------------------------------------------------------------ ホットキー入力
class HotkeyEdit(QLineEdit):
    """クリックして押したキーの組み合わせを 'Ctrl+Shift+Space' 形式で記録する。changed(str)。
    ウィンドウ内のキーイベント(nativeVirtualKey)だけを読む。空文字は未割り当て。"""

    changed = Signal(str)

    def __init__(self, value: str | None = None) -> None:
        super().__init__(value or "")
        self.setReadOnly(True)
        self.setPlaceholderText("クリックしてキーを押す(未割り当て)")
        self.setMinimumWidth(220)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._recording = False

    def focusInEvent(self, e: Any) -> None:  # noqa: N802
        self._recording = True
        self._prev = self.text()
        self.setText("")
        self.setPlaceholderText("キーを押してください… (Esc で取消 / Backspace で解除)")
        super().focusInEvent(e)

    def focusOutEvent(self, e: Any) -> None:  # noqa: N802
        if self._recording and not self.text():
            self.setText(self._prev)
        self._recording = False
        self.setPlaceholderText("クリックしてキーを押す(未割り当て)")
        super().focusOutEvent(e)

    def keyPressEvent(self, e: QKeyEvent) -> None:  # noqa: N802
        from deskkit.hotkeys import MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, VK_NAMES, format_hotkey

        key = e.key()
        if key == Qt.Key.Key_Escape and e.modifiers() == Qt.KeyboardModifier.NoModifier:
            self.setText(self._prev)
            self.clearFocus()
            return
        if key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete) and e.modifiers() == Qt.KeyboardModifier.NoModifier:
            self.setText("")
            self._prev = ""
            self.changed.emit("")
            self.clearFocus()
            return
        if key in (Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt, Qt.Key.Key_Meta, Qt.Key.Key_AltGr):
            return
        mods = 0
        m = e.modifiers()
        if m & Qt.KeyboardModifier.ControlModifier:
            mods |= MOD_CONTROL
        if m & Qt.KeyboardModifier.AltModifier:
            mods |= MOD_ALT
        if m & Qt.KeyboardModifier.ShiftModifier:
            mods |= MOD_SHIFT
        if m & Qt.KeyboardModifier.MetaModifier:
            mods |= MOD_WIN
        vk = e.nativeVirtualKey()
        if mods == 0 or vk not in VK_NAMES:
            self.setPlaceholderText("修飾キー(Ctrl / Alt / Shift / Win)と一緒に押してください")
            return
        text = format_hotkey(mods, vk)
        self.setText(text)
        self._prev = text
        self._recording = False
        self.changed.emit(text)
        self.clearFocus()


# ------------------------------------------------------------------ フェード切替スタック
class FadeStack(QStackedWidget):
    """ページ切替時に、新しいページを少し下からフェードインさせる。"""

    def switch_to(self, w: QWidget) -> None:
        if self.currentWidget() is w:
            return
        self.setCurrentWidget(w)
        eff = QGraphicsOpacityEffect(w)
        w.setGraphicsEffect(eff)
        fade = QPropertyAnimation(eff, b"opacity", w)
        fade.setDuration(220)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        slide = QPropertyAnimation(w, b"pos", w)
        slide.setDuration(260)
        end = QPoint(0, 0)
        slide.setStartValue(QPoint(0, 14))
        slide.setEndValue(end)
        slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        grp = QParallelAnimationGroup(w)
        grp.addAnimation(fade)
        grp.addAnimation(slide)
        grp.finished.connect(lambda: w.setGraphicsEffect(None))  # type: ignore[arg-type]
        grp.start(QParallelAnimationGroup.DeletionPolicy.DeleteWhenStopped)


class ScrollPage(QScrollArea):
    """余白付きで縦に積むページ。self.lay に追加する。"""

    def __init__(self, max_width: int = 1040) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        inner.setObjectName("PageInner")
        outer = QHBoxLayout(inner)
        outer.setContentsMargins(32, 26, 32, 32)
        col = QWidget()
        col.setMaximumWidth(max_width)
        self.lay = QVBoxLayout(col)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(16)
        outer.addWidget(col, 1)
        self.setWidget(inner)

    def add(self, w: QWidget) -> QWidget:
        self.lay.addWidget(w)
        return w

    def finish(self) -> None:
        self.lay.addStretch(1)


class EmptyState(QWidget):
    def __init__(self, glyph: str, title: str, text: str) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 28, 20, 28)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        g = Glyph(glyph, 28, T.TEXT_MUTE)
        g.setFixedSize(56, 56)
        g.setStyleSheet(f"color: {T.TEXT_MUTE}; background: {T.SURFACE2}; border-radius: 28px;")
        lay.addWidget(g, 0, Qt.AlignmentFlag.AlignCenter)
        t = label(title, "H3")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(t)
        d = label(text, "Mute", wrap=True)
        d.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(d)


# ------------------------------------------------------------------ ダイアログ
def dark_titlebar(widget: QWidget) -> None:
    """Windows 11 のタイトルバーをテーマに合わせる(失敗しても無視)。"""
    if T.IS_LIGHT:
        return
    try:
        hwnd = int(widget.winId())
        val = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))  # DWMWA_USE_IMMERSIVE_DARK_MODE
        cap = ctypes.c_int(0x00130F0B)  # COLORREF(BGR) = BG0 に近い色
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(cap), ctypes.sizeof(cap))  # DWMWA_CAPTION_COLOR
    except Exception:  # noqa: BLE001
        pass


class StyledDialog(QDialog):
    """枠なし・角丸・影付きのダイアログの土台。フェードインで出る。"""

    def __init__(self, parent: QWidget | None, title: str, glyph: str = G.INFO, accent: str = T.ACCENT, width: int = 440) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle(title)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        self.panel = QFrame()
        self.panel.setObjectName("Card")
        self.panel.setStyleSheet(f"QFrame#Card {{ background: {T.BG1}; border: 1px solid {T.BORDER_HI}; border-radius: 16px; }}")
        shadow(self.panel, 40, 160, 12)
        outer.addWidget(self.panel)
        self.panel.setMinimumWidth(width)
        lay = QVBoxLayout(self.panel)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(12)
        head = QHBoxLayout()
        gl = Glyph(glyph, 18, accent)
        gl.setFixedSize(36, 36)
        gl.setStyleSheet(f"color: {accent}; background: {T.alpha(accent, 0.14)}; border-radius: 10px;")
        head.addWidget(gl)
        head.addWidget(label(title, "H2"), 1)
        lay.addLayout(head)
        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        lay.addLayout(self.body)
        self.buttons = QHBoxLayout()
        self.buttons.addStretch(1)
        lay.addSpacing(4)
        lay.addLayout(self.buttons)
        self._drag: QPoint | None = None

    def showEvent(self, e: Any) -> None:  # noqa: N802
        super().showEvent(e)
        self.setWindowOpacity(0.0)
        a = QPropertyAnimation(self, b"windowOpacity", self)
        a.setDuration(160)
        a.setStartValue(0.0)
        a.setEndValue(1.0)
        a.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def mousePressEvent(self, e: Any) -> None:  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e: Any) -> None:  # noqa: N802
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _e: Any) -> None:  # noqa: N802
        self._drag = None


def confirm(parent: QWidget | None, title: str, text: str, *, ok_text: str = "実行", cancel_text: str = "キャンセル",
            danger: bool = False, checks: Sequence[tuple[str, bool]] = (), glyph: str | None = None) -> tuple[bool, list[bool]]:
    """確認ダイアログ。戻り値は (OK が押されたか, 各チェックボックスの値)。"""
    accent = T.DANGER if danger else T.ACCENT
    dlg = StyledDialog(parent, title, glyph or (G.WARNING if danger else G.INFO), accent)
    dlg.body.addWidget(label(text, "Dim", wrap=True))
    boxes: list[QCheckBox] = []
    for text_, default in checks:
        cb = QCheckBox(text_)
        cb.setChecked(default)
        dlg.body.addWidget(cb)
        boxes.append(cb)
    cancel = button(cancel_text, "ghost", on_click=dlg.reject)
    ok = button(ok_text, "danger" if danger else "primary", on_click=dlg.accept)
    dlg.buttons.addWidget(cancel)
    dlg.buttons.addWidget(ok)
    ok.setDefault(True)
    res = dlg.exec() == QDialog.DialogCode.Accepted
    return res, [b.isChecked() for b in boxes]


def message(parent: QWidget | None, title: str, text: str, *, kind: str = "info") -> None:
    colors = {"info": (T.INFO, G.INFO), "ok": (T.SUCCESS, G.CHECK), "warn": (T.WARN, G.WARNING), "error": (T.DANGER, G.ERROR)}
    accent, glyph = colors.get(kind, colors["info"])
    dlg = StyledDialog(parent, title, glyph, accent)
    lb = label(text, "Dim", wrap=True)
    lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    dlg.body.addWidget(lb)
    dlg.buttons.addWidget(button("OK", "primary", on_click=dlg.accept))
    dlg.exec()


def text_input(parent: QWidget | None, title: str, prompt: str, value: str = "", *, multiline: bool = False) -> str | None:
    from PySide6.QtWidgets import QPlainTextEdit

    dlg = StyledDialog(parent, title, G.EDIT, T.ACCENT, width=480)
    dlg.body.addWidget(label(prompt, "Dim", wrap=True))
    if multiline:
        ed: Any = QPlainTextEdit(value)
        ed.setMinimumHeight(160)
    else:
        ed = QLineEdit(value)
        ed.returnPressed.connect(dlg.accept)
    dlg.body.addWidget(ed)
    dlg.buttons.addWidget(button("キャンセル", "ghost", on_click=dlg.reject))
    dlg.buttons.addWidget(button("OK", "primary", on_click=dlg.accept))
    ed.setFocus()
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    return str(ed.toPlainText() if multiline else ed.text())


class KeyFilter(QWidget):
    """任意ウィジェットのキーイベントを横取りするための補助(パレット等で使う)。"""

    def __init__(self, handler: Callable[[QKeyEvent], bool]) -> None:
        super().__init__()
        self._h = handler

    def eventFilter(self, obj: Any, e: Any) -> bool:  # noqa: N802
        if e.type() == QEvent.Type.KeyPress:
            return bool(self._h(e))
        return False
