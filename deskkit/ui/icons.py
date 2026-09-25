# アプリ/トレイのアイコンを QPainter で描く(外部画像ファイルを持たない)。
# 4モジュールのアクセント色の角丸タイル 2x2 を暗色の角丸板に載せた意匠。状態バッジを重ねられる。
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPixmap

from deskkit.catalog import MODULES

_BADGE = {"error": "#F87171", "warn": "#FBBF24", "paused": "#94A3B8"}


def render(size: int, badge: str | None = None, dim: frozenset[str] = frozenset()) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = float(size)
    plate = QRectF(s * 0.03, s * 0.03, s * 0.94, s * 0.94)
    path = QPainterPath()
    path.addRoundedRect(plate, s * 0.24, s * 0.24)
    g = QLinearGradient(plate.topLeft(), plate.bottomRight())
    g.setColorAt(0, QColor("#1E2438"))
    g.setColorAt(1, QColor("#0C0F18"))
    p.fillPath(path, g)
    p.setPen(QColor(255, 255, 255, 34))
    p.drawPath(path)
    gap = s * 0.07
    cell = (plate.width() - gap * 3) / 2
    for i, m in enumerate(MODULES):
        r, c = divmod(i, 2)
        rect = QRectF(plate.left() + gap + c * (cell + gap), plate.top() + gap + r * (cell + gap), cell, cell)
        col = QColor(m.accent)
        if m.name in dim:
            col.setAlpha(70)
        tg = QLinearGradient(rect.topLeft(), rect.bottomRight())
        tg.setColorAt(0, col.lighter(118))
        tg.setColorAt(1, col.darker(112))
        tp = QPainterPath()
        tp.addRoundedRect(rect, cell * 0.28, cell * 0.28)
        p.fillPath(tp, tg)
    if badge in _BADGE:
        d = s * 0.42
        br = QRectF(s - d - s * 0.01, s - d - s * 0.01, d, d)
        p.setPen(QColor("#0A0D13"))
        p.setBrush(QColor(_BADGE[badge]))
        p.drawEllipse(br)
    p.end()
    return pm


def app_icon(badge: str | None = None) -> QIcon:
    ic = QIcon()
    for sz in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        ic.addPixmap(render(sz, badge))
    return ic
