# モニタ構成を縮尺どおりに描き、保存済みウィンドウの矩形を半透明の箱で重ねる自前描画ウィジェット。
# データが変わると前の位置から新しい位置へ補間して動く。箱にホバーすると hovered(添字) を出す(表の行と連動)。
# 描画中の例外は握りつぶす(Qt の仮想メソッドから例外を漏らさない)。色は描画のたびに theme(T.*)から読む(ライト/ダーク対応)。
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QEasingCurve, QPointF, QRectF, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from deskkit.ui import theme as T

log = logging.getLogger("deskkit.layoutkeep.map")


@dataclass(frozen=True)
class MapMonitor:
    key: str
    rect: tuple[int, int, int, int]
    work: tuple[int, int, int, int] | None
    primary: bool
    dpi: int | None
    number: int


@dataclass(frozen=True)
class MapBox:
    key: str
    index: int  # 表の行と対応する添字
    rect: tuple[int, int, int, int]  # 保存した位置(スクリーン座標)
    label: str
    maximized: bool = False
    current: tuple[int, int, int, int] | None = None  # 計画表示時の今の位置
    moving: bool = False
    muted: bool = False  # 動かさない(曖昧・除外など)


def _qr(r: tuple[int, int, int, int]) -> QRectF:
    return QRectF(r[0], r[1], r[2] - r[0], r[3] - r[1])


def _lerp(a: QRectF, b: QRectF, t: float) -> QRectF:
    return QRectF(a.x() + (b.x() - a.x()) * t, a.y() + (b.y() - a.y()) * t,
                  a.width() + (b.width() - a.width()) * t, a.height() + (b.height() - a.height()) * t)


def _color(hex_: str, a: float) -> QColor:
    c = QColor(hex_)
    c.setAlphaF(max(0.0, min(1.0, a)))
    return c


class MonitorMap(QWidget):
    hovered = Signal(int)

    def __init__(self, accent: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._accent = accent
        self.setMouseTracking(True)
        self.setMinimumHeight(260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._mons: list[MapMonitor] = []
        self._boxes: list[MapBox] = []
        self._from_mon: dict[str, QRectF] = {}
        self._from_box: dict[str, QRectF] = {}
        self._from_bounds: QRectF | None = None
        self._gone: list[tuple[str, QRectF, bool]] = []  # 消えていく物(キー, 矩形, モニタか)
        self._t = 1.0
        self._hover = -1
        self._highlight = -1
        self._empty_text = "モニタ情報がありません"
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(460)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)

    # ------------------------------------------------------------ データ
    def _on_anim(self, v: object) -> None:
        try:
            self._t = float(v)  # type: ignore[arg-type]
            self.update()
        except Exception:  # noqa: BLE001
            log.exception("map anim")

    def set_empty_text(self, text: str) -> None:
        self._empty_text = text
        self.update()

    def set_data(self, mons: Sequence[MapMonitor], boxes: Sequence[MapBox], *, animate: bool = True) -> None:
        # 今表示中の位置を「補間の起点」として覚える
        cur_mon = {m.key: self._mon_rect(m) for m in self._mons}
        cur_box = {b.key: self._box_rect(b) for b in self._boxes}
        bounds = self._bounds_now()
        new_keys_m = {m.key for m in mons}
        new_keys_b = {b.key for b in boxes}
        self._gone = [(k, r, True) for k, r in cur_mon.items() if k not in new_keys_m]
        self._gone += [(k, r, False) for k, r in cur_box.items() if k not in new_keys_b]
        self._from_mon, self._from_box, self._from_bounds = cur_mon, cur_box, bounds
        self._mons, self._boxes = list(mons), list(boxes)
        self._hover = -1
        if animate and self.isVisible():
            self._anim.stop()
            self._t = 0.0
            self._anim.start()
        else:
            self._t = 1.0
            self.update()

    def set_highlight(self, index: int) -> None:
        if index != self._highlight:
            self._highlight = index
            self.update()

    # ------------------------------------------------------------ 幾何
    def _target_bounds(self) -> QRectF | None:
        rs = [_qr(m.rect) for m in self._mons]
        if not rs:
            return None
        b = rs[0]
        for r in rs[1:]:
            b = b.united(r)
        return b

    def _bounds_now(self) -> QRectF | None:
        tb = self._target_bounds()
        if tb is None:
            return self._from_bounds
        if self._from_bounds is None:
            return tb
        return _lerp(self._from_bounds, tb, self._t)

    def _mon_rect(self, m: MapMonitor) -> QRectF:
        to = _qr(m.rect)
        fr = self._from_mon.get(m.key)
        return to if fr is None else _lerp(fr, to, self._t)

    def _box_rect(self, b: MapBox) -> QRectF:
        to = _qr(b.rect)
        fr = self._from_box.get(b.key)
        if fr is None:  # 新しい箱: 中心から広がる
            c = to.center()
            s = 0.6 + 0.4 * self._t
            return QRectF(c.x() - to.width() * s / 2, c.y() - to.height() * s / 2, to.width() * s, to.height() * s)
        return _lerp(fr, to, self._t)

    def _xform(self) -> tuple[float, float, float] | None:
        b = self._bounds_now()
        if b is None or b.width() <= 0 or b.height() <= 0:
            return None
        m = 22.0
        w, h = self.width() - 2 * m, self.height() - 2 * m
        s = min(w / b.width(), h / b.height())
        ox = m + (w - b.width() * s) / 2 - b.x() * s
        oy = m + (h - b.height() * s) / 2 - b.y() * s
        return s, ox, oy

    @staticmethod
    def _map(r: QRectF, xf: tuple[float, float, float]) -> QRectF:
        s, ox, oy = xf
        return QRectF(r.x() * s + ox, r.y() * s + oy, r.width() * s, r.height() * s)

    # ------------------------------------------------------------ 描画
    def paintEvent(self, _e: object) -> None:  # noqa: N802
        try:
            self._paint()
        except Exception:  # noqa: BLE001 - 描画の例外で落とさない
            log.exception("monitor map paint")

    def _paint(self) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        frame = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        bg = QLinearGradient(frame.topLeft(), frame.bottomLeft())
        bg.setColorAt(0, QColor(T.SURFACE2))
        bg.setColorAt(1, QColor(T.BG1))
        path = QPainterPath()
        path.addRoundedRect(frame, 12, 12)
        p.fillPath(path, bg)
        p.setPen(QPen(QColor(T.BORDER), 1))
        p.drawPath(path)
        # 方眼
        p.save()
        p.setClipPath(path)
        p.setPen(QPen(_color(T.BORDER, 0.35), 1))
        step = 24
        for x in range(0, self.width(), step):
            p.drawLine(x, 0, x, self.height())
        for y in range(0, self.height(), step):
            p.drawLine(0, y, self.width(), y)
        p.restore()
        xf = self._xform()
        if xf is None:
            p.setPen(QColor(T.TEXT_MUTE))
            p.setFont(T.ui_font(13))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._empty_text)
            return
        t = self._t
        # 消えていく物
        for _k, r, is_mon in self._gone:
            a = 1.0 - t
            if a <= 0:
                continue
            rr = self._map(r, xf)
            p.setPen(QPen(_color(T.BORDER_HI if is_mon else self._accent, 0.6 * a), 1))
            p.setBrush(_color(T.SURFACE3 if is_mon else self._accent, 0.25 * a))
            p.drawRoundedRect(rr, 6, 6)
        for m in self._mons:
            self._paint_monitor(p, m, xf)
        for b in self._boxes:
            self._paint_box(p, b, xf)

    def _paint_monitor(self, p: QPainter, m: MapMonitor, xf: tuple[float, float, float]) -> None:
        rr = self._map(self._mon_rect(m), xf).adjusted(3, 3, -3, -3)
        fade = 1.0 if m.key in self._from_mon else self._t
        g = QLinearGradient(rr.topLeft(), rr.bottomRight())
        g.setColorAt(0, _color(T.SURFACE3, 0.95 * fade))
        g.setColorAt(1, _color(T.SURFACE, 0.95 * fade))
        path = QPainterPath()
        path.addRoundedRect(rr, 9, 9)
        p.fillPath(path, g)
        # タスクバー部分(作業領域の外)
        if m.work is not None:
            s = xf[0]
            mon = _qr(m.rect)
            wk = _qr(m.work)
            p.save()
            p.setClipPath(path)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_color(T.TEXT_MUTE, 0.20 * fade))
            if wk.bottom() < mon.bottom() - 1:
                p.drawRect(QRectF(rr.left(), rr.bottom() - (mon.bottom() - wk.bottom()) * s, rr.width(), rr.height()))
            if wk.top() > mon.top() + 1:
                p.drawRect(QRectF(rr.left(), rr.top(), rr.width(), (wk.top() - mon.top()) * s))
            if wk.left() > mon.left() + 1:
                p.drawRect(QRectF(rr.left(), rr.top(), (wk.left() - mon.left()) * s, rr.height()))
            if wk.right() < mon.right() - 1:
                p.drawRect(QRectF(rr.right() - (mon.right() - wk.right()) * s, rr.top(), rr.width(), rr.height()))
            p.restore()
        edge = self._accent if m.primary else T.BORDER_HI
        p.setPen(QPen(_color(edge, (0.9 if m.primary else 0.8) * fade), 1.6 if m.primary else 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        # 番号(大きく薄く)
        big = T.ui_font(max(14, int(min(rr.width(), rr.height()) * 0.32)), QFont.Weight.Bold)
        p.setFont(big)
        p.setPen(_color(T.TEXT, 0.07 * fade))
        p.drawText(rr, Qt.AlignmentFlag.AlignCenter, str(m.number))
        # 解像度・拡大率
        w, h = m.rect[2] - m.rect[0], m.rect[3] - m.rect[1]
        info = f"{w}×{h}"
        if m.dpi:
            info += f"  ·  {round(m.dpi / 96 * 100)}%"
        p.setFont(T.ui_font(11, QFont.Weight.DemiBold))
        p.setPen(_color(T.TEXT_DIM, fade))
        p.drawText(rr.adjusted(10, 0, -10, -8), int(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight), info)
        if m.primary:
            badge = "主モニタ"
            fm = QFontMetrics(T.ui_font(10, QFont.Weight.Bold))
            bw = fm.horizontalAdvance(badge) + 14
            br = QRectF(rr.left() + 8, rr.bottom() - 26, bw, 18)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_color(self._accent, 0.9 * fade))
            p.drawRoundedRect(br, 9, 9)
            p.setFont(T.ui_font(10, QFont.Weight.Bold))
            p.setPen(_color(T.ON_ACCENT, fade))
            p.drawText(br, Qt.AlignmentFlag.AlignCenter, badge)

    def _paint_box(self, p: QPainter, b: MapBox, xf: tuple[float, float, float]) -> None:
        hot = b.index in (self._hover, self._highlight)
        fade = 1.0 if b.key in self._from_box else self._t
        rr = self._map(self._box_rect(b), xf).adjusted(1, 1, -1, -1)
        col = T.TEXT_MUTE if b.muted else self._accent
        # 計画表示: 今の位置(破線)と移動の矢印
        if b.current is not None and b.moving:
            cr = self._map(_qr(b.current), xf).adjusted(1, 1, -1, -1)
            pen = QPen(_color(T.TEXT_DIM, 0.7 * fade), 1, Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(cr, 5, 5)
            a, z = cr.center(), rr.center()
            p.setPen(QPen(_color(col, 0.85 * fade), 1.6))
            p.drawLine(a, z)
            self._arrow_head(p, a, z, _color(col, 0.85 * fade))
        fill = 0.40 if hot else (0.10 if b.muted else 0.20)
        p.setPen(QPen(_color(col, (1.0 if hot else 0.75) * fade), 2.0 if hot else 1.2))
        p.setBrush(_color(col, fill * fade))
        p.drawRoundedRect(rr, 5, 5)
        if rr.width() > 34 and rr.height() > 16:
            p.setFont(T.ui_font(11, QFont.Weight.DemiBold))
            fm = p.fontMetrics()
            text = b.label + ("  · 最大化" if b.maximized else "")
            el = fm.elidedText(text, Qt.TextElideMode.ElideRight, int(rr.width() - 14))
            chip = QRectF(rr.left() + 3, rr.top() + 3, fm.horizontalAdvance(el) + 8, fm.height() + 2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_color(T.SURFACE, (0.85 if hot else 0.7) * fade))
            p.drawRoundedRect(chip, 4, 4)
            p.setPen(_color(T.TEXT, (1.0 if hot else 0.85) * fade))
            p.drawText(chip, int(Qt.AlignmentFlag.AlignCenter), el)

    @staticmethod
    def _arrow_head(p: QPainter, a: QPointF, z: QPointF, c: QColor) -> None:
        import math

        dx, dy = z.x() - a.x(), z.y() - a.y()
        ln = math.hypot(dx, dy)
        if ln < 8:
            return
        ux, uy = dx / ln, dy / ln
        s = 7.0
        p1 = QPointF(z.x() - ux * s - uy * s * 0.6, z.y() - uy * s + ux * s * 0.6)
        p2 = QPointF(z.x() - ux * s + uy * s * 0.6, z.y() - uy * s - ux * s * 0.6)
        path = QPainterPath(z)
        path.lineTo(p1)
        path.lineTo(p2)
        path.closeSubpath()
        p.fillPath(path, c)

    # ------------------------------------------------------------ ホバー
    def _box_at(self, pos: QPointF) -> int:
        xf = self._xform()
        if xf is None:
            return -1
        for b in reversed(self._boxes):
            if self._map(self._box_rect(b), xf).contains(pos):
                return b.index
        return -1

    def mouseMoveEvent(self, e: object) -> None:  # noqa: N802
        try:
            idx = self._box_at(e.position())  # type: ignore[attr-defined]
            if idx != self._hover:
                self._hover = idx
                self.setCursor(Qt.CursorShape.PointingHandCursor if idx >= 0 else Qt.CursorShape.ArrowCursor)
                self.update()
                self.hovered.emit(idx)
        except Exception:  # noqa: BLE001
            log.exception("monitor map hover")

    def leaveEvent(self, e: object) -> None:  # noqa: N802
        try:
            if self._hover != -1:
                self._hover = -1
                self.update()
                self.hovered.emit(-1)
        except Exception:  # noqa: BLE001
            log.exception("monitor map leave")
