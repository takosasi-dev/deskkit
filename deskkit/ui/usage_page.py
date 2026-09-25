# 利用状況ページ。動作中の各モジュールの usage(days) を呼び、指標ごとの小さな棒グラフ(1系列1グラフ)にする。
# 色はモジュールの識別色(theme.chart_color。明度帯・色覚差を検証済み)。文字は常に文字色。棒にマウスを載せると日付と件数。
# 表示するのは日ごとの件数だけ(本文・パスは扱わない)。表でも見られる。
from __future__ import annotations

import datetime as _dt
import logging
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from deskkit import catalog
from deskkit.ui import theme as T
from deskkit.ui.theme import G
from deskkit.ui.widgets import Card, EmptyState, Glyph, Hero, ScrollPage, Segmented, StatusPill, label
from deskkit.usage import UsageSeries

if TYPE_CHECKING:
    from deskkit.host import Host

log = logging.getLogger("deskkit.host.ui")
_WD = "月火水木金土日"


def _day_label(d: _dt.date) -> str:
    return f"{d.month}/{d.day}({_WD[d.weekday()]})"


class BarChart(QWidget):
    """1系列の日別棒グラフ。細い棒・上端だけ角丸・2px の隙間・控えめな目盛り。"""

    def __init__(self, values: list[int], color: str, unit: str, end: _dt.date) -> None:
        super().__init__()
        self._v = values
        self._c = QColor(color)
        self._unit = unit
        self._end = end
        self._hover = -1
        self.setMouseTracking(True)
        self.setMinimumHeight(118)

    def _geom(self) -> tuple[QRectF, float, int]:
        r = QRectF(self.rect()).adjusted(30, 8, -6, -20)
        n = max(1, len(self._v))
        return r, r.width() / n, max(1, max(self._v) if self._v else 1)

    def _date(self, i: int) -> _dt.date:
        return self._end - _dt.timedelta(days=len(self._v) - 1 - i)

    def paintEvent(self, _e: Any) -> None:  # noqa: N802
        try:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            r, slot, vmax = self._geom()
            if not any(self._v):
                p.setPen(QPen(QColor(T.BORDER_HI), 1))
                p.drawLine(int(r.left()), int(r.bottom()), int(r.right()), int(r.bottom()))
                p.setPen(QColor(T.TEXT_MUTE))
                p.setFont(T.ui_font(11))
                p.drawText(r, Qt.AlignmentFlag.AlignCenter, "この期間はまだ記録がありません")
                return
            grid = QColor(T.BORDER)
            p.setFont(T.ui_font(10))
            ticks = [vmax] if vmax < 2 else [vmax // 2, vmax]  # 控えめな目盛り(整数のみ)
            for val in ticks:
                y = r.bottom() - r.height() * val / vmax
                p.setPen(QPen(grid, 1, Qt.PenStyle.DashLine))
                p.drawLine(int(r.left()), int(y), int(r.right()), int(y))
                p.setPen(QColor(T.TEXT_MUTE))
                p.drawText(QRectF(0, y - 8, 26, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(val))
            p.setPen(QPen(QColor(T.BORDER_HI), 1))
            p.drawLine(int(r.left()), int(r.bottom()), int(r.right()), int(r.bottom()))
            gap = 2.0 if slot > 5 else 1.0
            bw = max(1.0, min(slot - gap, 22.0))
            for i, v in enumerate(self._v):
                if v <= 0:
                    continue
                h = max(2.0, r.height() * v / vmax)
                x = r.left() + i * slot + (slot - bw) / 2
                c = QColor(self._c)
                if self._hover >= 0 and i != self._hover:
                    c.setAlpha(150)
                path = QPainterPath()
                rad = min(4.0, bw / 2, h)
                rect = QRectF(x, r.bottom() - h, bw, h)
                path.addRoundedRect(rect, rad, rad)
                path.addRect(QRectF(x, r.bottom() - rad, bw, rad))  # 底辺は角丸にしない(基線に接地)
                p.fillPath(path.simplified(), c)
            if 0 <= self._hover < len(self._v):  # ホバー中の列の縦線
                x = r.left() + self._hover * slot + slot / 2
                hl = QColor(T.TEXT_MUTE)
                hl.setAlpha(90)
                p.setPen(QPen(hl, 1))
                p.drawLine(int(x), int(r.top()), int(x), int(r.bottom()))
            p.setPen(QColor(T.TEXT_MUTE))
            n = len(self._v)
            for i in sorted({0, n // 2, n - 1}):
                d = self._date(i)
                x = r.left() + i * slot + slot / 2
                al = Qt.AlignmentFlag.AlignLeft if i == 0 else Qt.AlignmentFlag.AlignRight if i == n - 1 else Qt.AlignmentFlag.AlignHCenter
                box = QRectF(x - 40, r.bottom() + 3, 80, 14)
                if i == 0:
                    box = QRectF(x - slot / 2, r.bottom() + 3, 80, 14)
                elif i == n - 1:
                    box = QRectF(x + slot / 2 - 80, r.bottom() + 3, 80, 14)
                p.drawText(box, al | Qt.AlignmentFlag.AlignVCenter, "今日" if i == n - 1 else f"{d.month}/{d.day}")
        except Exception:  # noqa: BLE001 - 描画で落とさない
            pass

    def mouseMoveEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        try:
            r, slot, _vmax = self._geom()
            i = int((e.position().x() - r.left()) // slot)
            if 0 <= i < len(self._v) and r.left() <= e.position().x() <= r.right():
                if i != self._hover:
                    self._hover = i
                    self.update()
                QToolTip.showText(e.globalPosition().toPoint() + QPoint(12, 12),
                                  f"{_day_label(self._date(i))}  {self._v[i]} {self._unit}", self)
            elif self._hover != -1:
                self._hover = -1
                self.update()
                QToolTip.hideText()
        except Exception:  # noqa: BLE001
            pass

    def leaveEvent(self, _e: Any) -> None:  # noqa: N802
        self._hover = -1
        self.update()


def _series_card(s: UsageSeries, color: str, end: _dt.date) -> QWidget:
    w = QWidget()
    w.setObjectName("Inset")
    w.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    w.setStyleSheet(f"QWidget#Inset {{ background: {T.SURFACE2}; border: 1px solid {T.BORDER}; border-radius: 12px; }}")
    lay = QVBoxLayout(w)
    lay.setContentsMargins(14, 12, 14, 10)
    lay.setSpacing(4)
    head = QHBoxLayout()
    dot = label("●")
    dot.setStyleSheet(f"color: {color}; font-size: 10px;")
    head.addWidget(dot)
    head.addWidget(label(s.label, "H3"), 1)
    total = sum(s.per_day)
    tv = label(f"{total:,}", "H2")
    head.addWidget(tv)
    head.addWidget(label(s.unit, "Mute"))
    lay.addLayout(head)
    days = max(1, len(s.per_day))
    active = sum(1 for v in s.per_day if v > 0)
    lay.addWidget(label(f"1日平均 {total / days:.1f} {s.unit} ・ {active}/{days} 日で発生", "Mute"))
    lay.addWidget(BarChart(s.per_day, color, s.unit, end))
    if s.hint:
        hl = QHBoxLayout()
        hl.addWidget(Glyph(G.INFO, 12, T.TEXT_MUTE))
        hl.addWidget(label(s.hint, "Mute", wrap=True), 1)
        lay.addLayout(hl)
    return w


class UsagePage(ScrollPage):
    def __init__(self, host: Host) -> None:
        super().__init__()
        self._host = host
        hero = Hero("利用状況", "各モジュールがどれくらい役に立っているか。使われていない道具は、仕様書の撤退基準で見直しの目安にできます。",
                    G.LIST, T.ACCENT)
        self.period = Segmented([("7", "7日"), ("30", "30日"), ("90", "90日")], "30")
        self.period.changed.connect(lambda _v: self.refresh())
        self.view = Segmented([("chart", "グラフ"), ("table", "表")], "chart")
        self.view.changed.connect(lambda _v: self.refresh())
        hero.add_action(self.period)
        hero.add_action(self.view)
        hero.add_pill(StatusPill("件数だけを集計(本文は扱いません)", "info"))
        self.add(hero)
        self.box = QVBoxLayout()
        self.box.setSpacing(16)
        holder = QWidget()
        holder.setLayout(self.box)
        self.add(holder)
        self.finish()

    def refresh(self) -> None:
        while self.box.count():
            it = self.box.takeAt(0)
            w = it.widget() if it is not None else None
            if w is not None:
                w.deleteLater()
        days = int(self.period.value())
        end = _dt.date.today()
        any_running = False
        for m in catalog.MODULES:
            slot = self._host.loader.slots.get(m.name)
            if slot is None or slot.state != "running" or slot.module is None or slot.ctx is None:
                continue
            any_running = True
            color = T.chart_color(m.name)
            card = Card(m.title, m.tagline, m.glyph, m.accent)
            fn = getattr(slot.module, "usage", None)
            series: list[UsageSeries] = []
            if fn is not None:
                got = slot.ctx.safe(fn, "usage")(days)
                series = [s for s in (got or []) if isinstance(s, UsageSeries)]
            if not series:
                card.add(label("このモジュールはまだ集計できるデータがありません。", "Mute"))
                self.box.addWidget(card)
                continue
            prim = next((s for s in series if s.primary), series[0])
            ptotal = sum(prim.per_day)
            head = QHBoxLayout()
            big = label(f"{ptotal:,}", "H1")
            head.addWidget(big)
            head.addWidget(label(f"{prim.unit}  {prim.label}(直近{days}日)", "Dim"), 1)
            card.add_layout(head)
            if self.view.value() == "table":
                card.add(self._table(series, days, end))
            else:
                grid = QGridLayout()
                grid.setSpacing(10)
                for i, s in enumerate(series):
                    grid.addWidget(_series_card(s, color, end), i // 2, i % 2)
                card.add_layout(grid)
            self.box.addWidget(card)
        if not any_running:
            self.box.addWidget(EmptyState(G.LIST, "動作中のモジュールがありません", "ホームでモジュールをオンにすると、ここに利用状況が出ます。"))

    def _table(self, series: list[UsageSeries], days: int, end: _dt.date) -> QTableWidget:
        t = QTableWidget(days, len(series) + 1)
        t.setHorizontalHeaderLabels(["日付", *[f"{s.label}({s.unit})" for s in series]])
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        t.setAlternatingRowColors(True)
        for row in range(days):
            i = days - 1 - row  # 新しい日を上に
            d = end - _dt.timedelta(days=days - 1 - i)
            t.setItem(row, 0, QTableWidgetItem(_day_label(d)))
            for c, s in enumerate(series, start=1):
                v = s.per_day[i] if i < len(s.per_day) else 0
                it = QTableWidgetItem(str(v))
                it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if v == 0:
                    it.setForeground(QColor(T.TEXT_MUTE))
                t.setItem(row, c, it)
        t.resizeColumnsToContents()
        t.horizontalHeader().setStretchLastSection(True)
        t.setMinimumHeight(min(420, 34 + days * 30))
        f = QFont(t.font())
        f.setPixelSize(12)
        t.setFont(f)
        return t
