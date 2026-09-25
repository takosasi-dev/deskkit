# ModeShift の画面部品: 状態チップ、モードタイル(現在のモードは光彩が脈打つ)、幅に合わせて並ぶタイル格子、
# 注意バナー、数え上げアニメーション付きの結果表示。色は theme とモジュールのアクセント色だけを使う。
# Qt の仮想メソッドの中身は try/except で包み、例外を Qt へ漏らさない(D-3)。
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from PySide6.QtCore import QEasingCurve, QRectF, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from deskkit.catalog import info as module_info
from deskkit.modules.modeshift.config import ModeDef
from deskkit.modules.modeshift.model import TYPE_LABELS
from deskkit.ui import theme as T
from deskkit.ui import widgets as W
from deskkit.ui.theme import G

log = logging.getLogger("deskkit.modeshift")

# 色はテーマ(ダーク/ライト)で差し替わるので、import 時に値をコピーせず必ず関数で実行時に読む
PALETTE_KEYS: tuple[str, ...] = ("module", "info", "success", "warn", "violet", "danger", "indigo")


def accent() -> str:
    """モジュールのアクセント色(テーマに合わせた現在の値)。"""
    return module_info("modeshift").accent


def palette_color(key: str) -> str:
    table = {"module": accent(), "info": T.INFO, "success": T.SUCCESS, "warn": T.WARN,
             "violet": T.ACCENT_2, "danger": T.DANGER, "indigo": T.ACCENT}
    return table.get(key, accent())


def palette() -> list[tuple[str, str]]:
    """モードの色の選択肢 (キー, 現在のテーマでの色)。設定にはキーを保存する。"""
    return [(k, palette_color(k)) for k in PALETTE_KEYS]


TYPE_GLYPHS: dict[str, str] = {
    "launch_app": G.APP, "close_app": G.CLOSE, "power_plan": G.POWER, "master_volume": G.VOLUME,
    "app_volume": G.VOLUME, "open_path": G.FOLDER, "open_url": G.LINK, "layout_apply": G.LAYOUT,
}


def result_style(kind: str) -> tuple[str, str, str]:
    """結果の種類 → (色, 字形, 表示名)。"""
    table = {
        "ok": (T.SUCCESS, G.CHECK, "成功"),
        "skipped": (T.TEXT_MUTE, G.CHEVRON, "スキップ"),
        "failed": (T.DANGER, G.ERROR, "失敗"),
        "still_running": (T.WARN, G.WARNING, "終了せず"),
        "aborted": (T.DANGER, G.CLOSE, "中断"),
        "planned": (accent(), G.PLAY, "実行"),
        "waiting": (T.TEXT_DIM, G.CLOCK, "待機"),
        "restore": (T.INFO, G.UNDO, "戻す"),
    }
    return table.get(kind, table["waiting"])


def accent_key(mode: ModeDef | dict[str, Any] | None, index: int) -> str:
    acc = mode.accent if isinstance(mode, ModeDef) else (mode or {}).get("accent")
    if isinstance(acc, str) and acc in PALETTE_KEYS:
        return acc
    return PALETTE_KEYS[index % len(PALETTE_KEYS)]


def mode_accent(mode: ModeDef | dict[str, Any] | None, index: int) -> str:
    return palette_color(accent_key(mode, index))


def guard(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Qt から呼ばれる処理の例外を握ってログに出す。"""
    def w(*a: Any, **k: Any) -> Any:
        try:
            return fn(*a, **k)
        except Exception:  # noqa: BLE001
            log.exception("GUI ハンドラで例外: %s", getattr(fn, "__qualname__", fn))
            return None
    return w


class Chip(QLabel):
    """小さな角丸チップ(字形+文字)。"""

    def __init__(self, text: str = "", color: str | None = None, glyph: str | None = None) -> None:
        super().__init__()
        self.set(text, color or T.TEXT_DIM, glyph)

    def set(self, text: str, color: str, glyph: str | None = None) -> None:
        g = f"<span style='font-family:\"{T.icon_family()}\"; font-size:10px'>{glyph}</span>&nbsp;&nbsp;" if glyph else ""
        self.setText(f"{g}{text}")
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setStyleSheet(
            f"QLabel {{ color: {color}; background: {T.alpha(color, 0.13)}; border: 1px solid {T.alpha(color, 0.32)};"
            f" border-radius: 10px; padding: 2px 9px; font-size: 12px; font-weight: 600; }}")
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def set_kind(self, kind: str, text: str | None = None) -> None:
        color, glyph, label = result_style(kind)
        self.set(text or label, color, glyph)


class Banner(QFrame):
    """プレビュー上部などの注意書き。"""

    def __init__(self, glyph: str, title: str, text: str, color: str) -> None:
        super().__init__()
        self.setStyleSheet(f"QFrame {{ background: {T.alpha(color, 0.10)}; border: 1px solid {T.alpha(color, 0.35)};"
                           f" border-radius: 12px; }} QLabel {{ border: none; background: transparent; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(12)
        g = W.Glyph(glyph, 18, color)
        lay.addWidget(g, 0, Qt.AlignmentFlag.AlignTop)
        box = QVBoxLayout()
        box.setSpacing(2)
        t = W.label(title, "H3")
        t.setStyleSheet(f"color: {color};")
        box.addWidget(t)
        box.addWidget(W.label(text, "Dim", wrap=True))
        lay.addLayout(box, 1)


class CountUp(QLabel):
    """数字が 0 から数え上がる表示。"""

    def __init__(self, color: str) -> None:
        super().__init__("0")
        self.setStyleSheet(f"color: {color}; font-size: 26px; font-weight: 700;")
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(650)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(guard(lambda v: self.setText(str(int(round(float(v)))))))

    def run_to(self, n: int) -> None:
        self._anim.stop()
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(float(n))
        self._anim.start()


class ResultStat(QFrame):
    def __init__(self, kind: str) -> None:
        super().__init__()
        color, glyph, label = result_style(kind)
        self.setObjectName("Inset")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 14, 8)
        g = W.Glyph(glyph, 16, color)
        g.setFixedSize(34, 34)
        g.setStyleSheet(f"color: {color}; background: {T.alpha(color, 0.14)}; border-radius: 10px;")
        lay.addWidget(g)
        box = QVBoxLayout()
        box.setSpacing(0)
        self.num = CountUp(color)
        box.addWidget(self.num)
        box.addWidget(W.label(label, "Mute"))
        lay.addLayout(box, 1)


# ------------------------------------------------------------------ モードタイル
class ModeTile(QFrame):
    preview_clicked = Signal(str)
    switch_clicked = Signal(str)

    def __init__(self, mode: ModeDef, accent: str, *, current: bool, busy: bool) -> None:
        super().__init__()
        self.mode = mode
        self._accent = QColor(accent)
        self._current = current
        self._hover = 0.0
        self.setMinimumSize(270, 196)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._fx = QGraphicsDropShadowEffect(self)
        self._fx.setOffset(0, 6)
        self._fx.setBlurRadius(22)
        self._fx.setColor(self._shadow_color())
        self.setGraphicsEffect(self._fx)
        self._glow: QVariantAnimation | None = None
        if current:
            self._start_glow()
        self._hover_anim = QVariantAnimation(self)
        self._hover_anim.setDuration(180)
        self._hover_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._hover_anim.valueChanged.connect(guard(self._set_hover))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(10)
        first = mode.actions[0]["type"] if mode.actions and mode.actions[0].get("type") in TYPE_GLYPHS else None
        ic = W.Glyph(TYPE_GLYPHS.get(first or "", G.MODE) if first else G.MODE, 18, accent)
        ic.setFixedSize(38, 38)
        ic.setStyleSheet(f"color: {accent}; background: {T.alpha(accent, 0.16)}; border-radius: 11px;")
        head.addWidget(ic)
        tb = QVBoxLayout()
        tb.setSpacing(0)
        title = W.label(mode.label, "H2")
        tb.addWidget(title)
        tb.addWidget(W.label(mode.name, "Mute"))
        head.addLayout(tb, 1)
        if not mode.valid:
            head.addWidget(Chip("無効", T.DANGER, G.ERROR), 0, Qt.AlignmentFlag.AlignTop)
        elif mode.confirmed:
            head.addWidget(Chip("確認済み", T.SUCCESS, G.CHECK), 0, Qt.AlignmentFlag.AlignTop)
        else:
            head.addWidget(Chip("未確認", T.WARN, G.SHIELD), 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(head)

        meta = QHBoxLayout()
        meta.setSpacing(6)
        if current:
            meta.addWidget(Chip("現在のモード", accent, G.SPARKLE))
        meta.addWidget(Chip(mode.hotkey or "ホットキーなし", T.TEXT_DIM if mode.hotkey else T.TEXT_MUTE, G.KEYBOARD))
        meta.addStretch(1)
        lay.addLayout(meta)

        counts: dict[str, int] = {}
        for a in mode.actions:
            t = str(a.get("type"))
            counts[t] = counts.get(t, 0) + 1
        summ = QHBoxLayout()
        summ.setSpacing(4)
        for t, n in counts.items():
            if t not in TYPE_GLYPHS:
                continue
            c = Chip(f"{n}" if n > 1 else "", T.TEXT_DIM, TYPE_GLYPHS[t])
            c.setToolTip(f"{TYPE_LABELS[t]} × {n}")
            summ.addWidget(c)
        summ.addStretch(1)
        lay.addLayout(summ)
        if not mode.valid:
            err = W.label(mode.reason(), wrap=True)
            err.setStyleSheet(f"color: {T.DANGER}; font-size: 12px;")
            err.setMaximumHeight(36)
            lay.addWidget(err)
        elif mode.warnings:
            wl = W.label(" / ".join(mode.warnings), wrap=True)
            wl.setStyleSheet(f"color: {T.WARN}; font-size: 12px;")
            lay.addWidget(wl)
        lay.addStretch(1)
        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.btn_preview = W.button("プレビュー", "ghost", G.EYE, on_click=guard(lambda: self.preview_clicked.emit(mode.name)))
        self.btn_switch = W.button("切り替え", "primary", G.PLAY, on_click=guard(lambda: self.switch_clicked.emit(mode.name)))
        self.btn_preview.setEnabled(mode.valid and not busy)
        self.btn_switch.setEnabled(mode.valid and not busy)
        btns.addWidget(self.btn_preview)
        btns.addStretch(1)
        btns.addWidget(self.btn_switch)
        lay.addLayout(btns)

    @staticmethod
    def _shadow_color() -> QColor:
        c = QColor(T.TEXT if T.IS_LIGHT else T.BG0)   # ライトでは濃い文字色を薄く、ダークでは背景より暗い影
        c.setAlpha(40 if T.IS_LIGHT else 150)
        return c

    # 現在のモード: 光彩がゆっくり脈打つ
    def _start_glow(self) -> None:
        a = QVariantAnimation(self)
        a.setDuration(2200)
        a.setStartValue(0.0)
        a.setKeyValueAt(0.5, 1.0)
        a.setEndValue(0.0)
        a.setEasingCurve(QEasingCurve.Type.InOutSine)
        a.setLoopCount(-1)
        a.valueChanged.connect(guard(self._set_glow))
        a.start()
        self._glow = a

    def _set_glow(self, v: Any) -> None:
        t = float(v)
        c = QColor(self._accent)
        c.setAlpha(int(90 + 110 * t))
        self._fx.setColor(c)
        self._fx.setBlurRadius(26 + 26 * t)
        self._fx.setOffset(0, 0)

    def _set_hover(self, v: Any) -> None:
        self._hover = float(v)
        if not self._current:
            self._fx.setBlurRadius(22 + 18 * self._hover)
            self._fx.setOffset(0, 6 + 4 * self._hover)
        self.update()

    def enterEvent(self, e: Any) -> None:
        try:
            self._hover_anim.stop()
            self._hover_anim.setStartValue(self._hover)
            self._hover_anim.setEndValue(1.0)
            self._hover_anim.start()
        except Exception:  # noqa: BLE001
            log.exception("enterEvent")
        super().enterEvent(e)

    def leaveEvent(self, e: Any) -> None:
        try:
            self._hover_anim.stop()
            self._hover_anim.setStartValue(self._hover)
            self._hover_anim.setEndValue(0.0)
            self._hover_anim.start()
        except Exception:  # noqa: BLE001
            log.exception("leaveEvent")
        super().leaveEvent(e)

    def paintEvent(self, _e: Any) -> None:
        try:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            path = QPainterPath()
            path.addRoundedRect(r, 16, 16)
            p.fillPath(path, QColor(T.SURFACE))
            g = QLinearGradient(r.topLeft(), r.bottomRight())
            a = QColor(self._accent)
            a.setAlpha(int((58 if self._current else 30) + 22 * self._hover))
            b = QColor(self._accent)
            b.setAlpha(0)
            g.setColorAt(0.0, a)
            g.setColorAt(0.7, b)
            p.fillPath(path, g)
            edge = QColor(self._accent) if (self._current or self._hover > 0.01) else QColor(T.BORDER)
            if self._current:
                edge.setAlpha(210)
            elif self._hover > 0.01:
                edge.setAlpha(int(60 + 120 * self._hover))
            if not self.mode.valid:
                edge = QColor(T.DANGER)
                edge.setAlpha(120)
            p.setPen(QPen(edge, 1.6 if self._current else 1.0))
            p.drawPath(path)
            p.end()
        except Exception:  # noqa: BLE001
            log.exception("ModeTile.paintEvent")


class TileGrid(QWidget):
    """幅に応じて列数を変えるタイル格子。"""

    def __init__(self, min_w: int = 290) -> None:
        super().__init__()
        self._min_w = min_w
        self._tiles: list[QWidget] = []
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(14)
        self._grid.setVerticalSpacing(14)
        self._cols = 0

    def set_tiles(self, tiles: Sequence[QWidget]) -> None:
        for t in self._tiles:
            self._grid.removeWidget(t)
            t.deleteLater()
        self._tiles = list(tiles)
        self._cols = 0
        self._relayout()

    def _relayout(self) -> None:
        cols = max(1, min(3, self.width() // self._min_w)) if self.width() > 0 else 2
        if cols == self._cols:
            return
        self._cols = cols
        for t in self._tiles:
            self._grid.removeWidget(t)
        for i, t in enumerate(self._tiles):
            self._grid.addWidget(t, i // cols, i % cols)
        for c in range(3):
            self._grid.setColumnStretch(c, 1 if c < cols else 0)

    def resizeEvent(self, e: Any) -> None:
        super().resizeEvent(e)
        try:
            self._relayout()
        except Exception:  # noqa: BLE001
            log.exception("TileGrid.resizeEvent")
