# Control Center の ClipTrim 画面。投入(ドロップ・選ぶ)・コマ・タイムライン(スライダー)・時刻の入力・コマ送り・フィルムストリップ・
# 区間の一覧・切り方/書き出し方/撮影場所などの情報(毎回見える選択)・見積もり・進捗と中止・結果・設定(「送る」・フィルムストリップの枚数)。
# ファイル名は画面にだけ出す(V-8)。シグナルから呼ぶ処理は guard で例外を握る(ログには型名だけ)。キー送信・再生はしない。
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QFont,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from deskkit import ffmpeg as _ff
from deskkit.modules.cliptrim import config as cfgmod
from deskkit.modules.cliptrim import plan, sendto
from deskkit.modules.cliptrim.module import SHIFT_WARN_S
from deskkit.ui import theme as T
from deskkit.ui import widgets as W
from deskkit.ui.theme import G

if TYPE_CHECKING:
    from deskkit.modules.cliptrim.module import ClipTrimModule

_log = logging.getLogger("deskkit.cliptrim")
G_VIDEO = ""
G_CUT = ""
FILE_FILTER = "動画 (*.mp4 *.m4v *.mov *.mkv *.webm *.avi *.ts *.m2ts *.mts *.wmv *.flv *.3gp *.mpg *.mpeg);;すべてのファイル (*.*)"
NOTICE_MS = 5000


def guard(fn: Callable[..., Any], label: str = "ui") -> Callable[..., Any]:
    """Qt のシグナルから呼ぶ処理の例外を握る。ログには例外の型名だけを書く(パスを含む文を書かない。INV-3)。"""

    def w(*a: Any, **k: Any) -> Any:
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001
            _log.error("handler %s failed: %s", label, type(e).__name__)
            return None

    return w


def _local_files(md: Any) -> list[str]:
    if md is None or not md.hasUrls():
        return []
    return [u.toLocalFile() for u in md.urls() if u.isLocalFile() and u.toLocalFile()]


def _pixmap(png: bytes | None) -> QPixmap | None:
    if not png:
        return None
    pm = QPixmap()
    return pm if pm.loadFromData(png) else None


class _Note(QFrame):
    """色付きの注意書き。"""

    def __init__(self, color: str, glyph: str) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(10)
        self.glyph = W.Glyph(glyph, 14, color)
        lay.addWidget(self.glyph, 0, Qt.AlignmentFlag.AlignTop)
        self.label = W.label("", None, wrap=True)
        lay.addWidget(self.label, 1)
        self.set_color(color)
        self.setVisible(False)

    def set_color(self, color: str) -> None:
        self.setStyleSheet(f"QFrame {{ background: {T.alpha(color, 0.08)}; border: 1px solid {T.alpha(color, 0.30)}; border-radius: 10px; }}")
        self.glyph.setStyleSheet(f"color: {color}; background: transparent; border: none;")
        self.label.setStyleSheet(f"color: {T.TEXT_DIM}; background: transparent; border: none; font-size: 12px;")

    def show_text(self, kind: str, text: str) -> None:
        color = {"error": T.DANGER, "warn": T.WARN, "ok": T.SUCCESS}.get(kind, T.INFO)
        self.set_color(color)
        self.glyph.setText({"error": G.ERROR, "warn": G.WARNING, "ok": G.CHECK}.get(kind, G.INFO))
        self.label.setText(text)
        self.setVisible(bool(text))


class _DropZone(QFrame):
    """点線の枠の投入エリア(ページ全体がドロップを受ける。ここは見た目と「動画を選ぶ」)。"""

    def __init__(self, accent: str, on_pick: Callable[[], Any]) -> None:
        super().__init__()
        self._accent = accent
        self._hot = False
        self.setMinimumHeight(230)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(8)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        g = W.Glyph(G_VIDEO, 26, accent)
        g.setFixedSize(52, 52)
        g.setStyleSheet(f"color: {accent}; background: {T.alpha(accent, 0.12)}; border-radius: 26px;")
        lay.addWidget(g, 0, Qt.AlignmentFlag.AlignCenter)
        t = W.label("動画を1本、ここにドロップ", "H3")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(t)
        s = W.label("始まりと終わりを決めて、その部分だけを別のファイルにします。元の動画は変えません。", "Mute", wrap=True)
        s.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(s)
        lay.addWidget(W.button("動画を選ぶ", "secondary", G.FOLDER, on_click=on_pick), 0, Qt.AlignmentFlag.AlignCenter)

    def set_hot(self, hot: bool) -> None:
        self._hot = hot
        self.update()

    def paintEvent(self, _e: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        bg = QColor(self._accent)
        bg.setAlpha(34 if self._hot else 12)
        p.setBrush(bg)
        c = QColor(self._accent)
        c.setAlpha(230 if self._hot else 140)
        p.setPen(QPen(c, 2 if self._hot else 1.5, Qt.PenStyle.DashLine))
        p.drawRoundedRect(r, 14, 14)
        p.end()


class FrameView(QWidget):
    """コマの静止画。取り出している間は前のコマを薄く出す(FR-5)。"""

    def __init__(self, accent: str) -> None:
        super().__init__()
        self._accent = accent
        self._pm: QPixmap | None = None
        self._loading = False
        self._error = ""
        self._caption = ""
        self._badge = ""
        self.setMinimumHeight(260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._dots = 0
        self._timer = QTimer(self)
        self._timer.setInterval(320)
        self._timer.timeout.connect(guard(self._tick, "frame:tick"))

    def sizeHint(self) -> Any:  # noqa: N802
        from PySide6.QtCore import QSize

        return QSize(640, 360)

    def set_state(self, png: bytes | None, loading: bool, error: str, caption: str, badge: str) -> None:
        pm = _pixmap(png)
        if pm is not None:
            self._pm = pm
        elif png is None and not loading and not error:
            self._pm = None
        self._loading = loading
        self._error = error
        self._caption = caption
        self._badge = badge
        if loading and not self._timer.isActive():
            self._timer.start()
        elif not loading:
            self._timer.stop()
        self.update()

    def _tick(self) -> None:
        self._dots = (self._dots + 1) % 4
        self.update()

    def paintEvent(self, _e: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 12, 12)
        p.fillPath(path, QColor(T.BG0))
        p.setClipPath(path)
        if self._pm is not None and not self._pm.isNull():
            sz = self._pm.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            x = (self.width() - sz.width()) / 2
            y = (self.height() - sz.height()) / 2
            p.setOpacity(0.4 if (self._loading or self._error) else 1.0)
            p.drawPixmap(QRectF(x, y, sz.width(), sz.height()), self._pm, QRectF(self._pm.rect()))
            p.setOpacity(1.0)
        f = T.ui_font(12, QFont.Weight.DemiBold)
        p.setFont(f)
        if self._loading:
            self._chip(p, QPointF(14, 14), "読み込み中" + "." * self._dots, T.TEXT)
        if self._error:
            p.setPen(QColor(T.WARN))
            p.drawText(QRectF(self.rect()), Qt.AlignmentFlag.AlignCenter, self._error)
        if self._caption:
            self._chip(p, QPointF(14, self.height() - 38), self._caption, T.TEXT)
        if self._badge:
            fm = p.fontMetrics()
            w = fm.horizontalAdvance(self._badge) + 20
            self._chip(p, QPointF(self.width() - w - 14, 14), self._badge, self._accent)
        p.setClipping(False)
        p.setPen(QPen(QColor(T.BORDER), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.end()

    def _chip(self, p: QPainter, at: QPointF, text: str, color: str) -> None:
        fm = p.fontMetrics()
        w = fm.horizontalAdvance(text) + 20
        rr = QRectF(at.x(), at.y(), w, 24)
        bg = QColor(T.BG0)
        bg.setAlpha(200)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(rr, 12, 12)
        p.setPen(QColor(color))
        p.drawText(rr, Qt.AlignmentFlag.AlignCenter, text)


class Timeline(QWidget):
    """位置のスライダー。区間・印・今の位置を描き、押す・引くで位置を変える(FR-3)。"""

    scrubbed = Signal(float)

    def __init__(self, accent: str) -> None:
        super().__init__()
        self._accent = accent
        self.duration = 0.0
        self.at = 0.0
        self.segments: list[tuple[float, float, float | None]] = []  # (開始, 終了, 実際の開始)
        self.mark_in: float | None = None
        self.mark_out: float | None = None
        self._drag = False
        self._hover: float | None = None
        self.setMinimumHeight(46)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def _track(self) -> QRectF:
        return QRectF(10, self.height() / 2 - 5, max(1.0, self.width() - 20.0), 10)

    def _x(self, t: float) -> float:
        tr = self._track()
        return tr.left() + (t / self.duration if self.duration > 0 else 0.0) * tr.width()

    def _t(self, x: float) -> float:
        tr = self._track()
        frac = (x - tr.left()) / tr.width()
        return max(0.0, min(1.0, frac)) * self.duration

    def paintEvent(self, _e: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        tr = self._track()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(T.SURFACE3))
        p.drawRoundedRect(tr, 5, 5)
        acc = QColor(self._accent)
        for s, e, actual in self.segments:
            if actual is not None and actual < s - 1e-6:
                c = QColor(acc)
                c.setAlpha(70)
                p.setBrush(c)
                p.drawRoundedRect(QRectF(self._x(actual), tr.top(), max(2.0, self._x(s) - self._x(actual)), tr.height()), 3, 3)
            c = QColor(acc)
            c.setAlpha(200)
            p.setBrush(c)
            p.drawRoundedRect(QRectF(self._x(s), tr.top(), max(3.0, self._x(e) - self._x(s)), tr.height()), 3, 3)
        for m, col in ((self.mark_in, T.SUCCESS), (self.mark_out, T.WARN)):
            if m is not None:
                x = self._x(m)
                p.setPen(QPen(QColor(col), 2))
                p.drawLine(QPointF(x, tr.top() - 9), QPointF(x, tr.bottom() + 9))
        if self.mark_in is not None and self.mark_out is not None and self.mark_out > self.mark_in:
            c = QColor(T.SUCCESS)
            c.setAlpha(60)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawRect(QRectF(self._x(self.mark_in), tr.top(), self._x(self.mark_out) - self._x(self.mark_in), tr.height()))
        if self._hover is not None and not self._drag:
            p.setPen(QPen(QColor(T.TEXT_MUTE), 1))
            x = self._x(self._hover)
            p.drawLine(QPointF(x, tr.top() - 4), QPointF(x, tr.bottom() + 4))
        if self.duration > 0:
            x = self._x(self.at)
            p.setPen(QPen(QColor(T.BG0), 2))
            p.setBrush(QColor(T.TEXT))
            p.drawEllipse(QPointF(x, tr.center().y()), 8, 8)
        p.end()

    def mousePressEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        try:
            if e.button() == Qt.MouseButton.LeftButton and self.duration > 0:
                self._drag = True
                self.at = self._t(e.position().x())
                self.update()
                self.scrubbed.emit(self.at)
        except Exception as ex:  # noqa: BLE001
            _log.error("timeline press failed: %s", type(ex).__name__)

    def mouseMoveEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        try:
            t = self._t(e.position().x())
            if self._drag:
                self.at = t
                self.scrubbed.emit(t)
            else:
                self._hover = t
                self.setToolTip(plan.fmt_time(t))
            self.update()
        except Exception as ex:  # noqa: BLE001
            _log.error("timeline move failed: %s", type(ex).__name__)

    def mouseReleaseEvent(self, _e: QMouseEvent) -> None:  # noqa: N802
        self._drag = False

    def leaveEvent(self, _e: Any) -> None:  # noqa: N802
        self._hover = None
        self.update()


class Filmstrip(QWidget):
    """長さを等分した位置の切れ目のコマを左から並べる。押すとその位置へ(FR-6)。"""

    picked = Signal(int)

    def __init__(self, accent: str) -> None:
        super().__init__()
        self._accent = accent
        self.pixmaps: list[QPixmap | None] = []
        self.targets: list[float] = []
        self.duration = 0.0
        self.at = 0.0
        self._hover = -1
        self.setMinimumHeight(58)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def cells(self) -> list[QRectF]:
        n = len(self.pixmaps)
        if n == 0:
            return []
        w = self.width() / n
        return [QRectF(i * w + 1, 0, w - 2, self.height()) for i in range(n)]

    def paintEvent(self, _e: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        for i, r in enumerate(self.cells()):
            path = QPainterPath()
            path.addRoundedRect(r, 6, 6)
            p.fillPath(path, QColor(T.SURFACE2))
            pm = self.pixmaps[i]
            if pm is not None and not pm.isNull():
                p.save()
                p.setClipPath(path)
                src = QRectF(pm.rect())
                # 枠いっぱいに(はみ出す分は中央で切る)
                scale = max(r.width() / src.width(), r.height() / src.height())
                sw, sh = r.width() / scale, r.height() / scale
                p.drawPixmap(r, pm, QRectF(src.center().x() - sw / 2, src.center().y() - sh / 2, sw, sh))
                p.restore()
            if i == self._hover:
                p.setPen(QPen(QColor(self._accent), 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(path)
        if self.duration > 0 and self.pixmaps:
            x = self.at / self.duration * self.width()
            p.setPen(QPen(QColor(self._accent), 2))
            p.drawLine(QPointF(x, 0), QPointF(x, self.height()))
        p.end()

    def _index(self, x: float) -> int:
        n = len(self.pixmaps)
        if n == 0 or self.width() <= 0:
            return -1
        return max(0, min(n - 1, int(x / (self.width() / n))))

    def mouseMoveEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        i = self._index(e.position().x())
        if i != self._hover:
            self._hover = i
            self.update()

    def leaveEvent(self, _e: Any) -> None:  # noqa: N802
        self._hover = -1
        self.update()

    def mousePressEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        try:
            i = self._index(e.position().x())
            if i >= 0 and e.button() == Qt.MouseButton.LeftButton:
                self.picked.emit(i)
        except Exception as ex:  # noqa: BLE001
            _log.error("filmstrip press failed: %s", type(ex).__name__)


class _SegRow(QFrame):
    def __init__(self, page: ClipTrimPage, index: int) -> None:
        super().__init__()
        self.setObjectName("Inset")
        m = page.m
        seg = m.segments[index]
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 8, 8)
        lay.setSpacing(10)
        acc = page.accent
        num = QLabel(str(index + 1))
        num.setFixedSize(28, 28)
        num.setAlignment(Qt.AlignmentFlag.AlignCenter)
        num.setStyleSheet(f"color: {acc}; background: {T.alpha(acc, 0.14)}; border-radius: 14px; font-weight: 700;")
        lay.addWidget(num, 0, Qt.AlignmentFlag.AlignTop)
        mid = QVBoxLayout()
        mid.setSpacing(2)
        head = W.label(f"{plan.fmt_time_ms(seg.start)} 〜 {plan.fmt_time_ms(seg.end)}   ({plan.fmt_seconds(seg.length)})")
        head.setStyleSheet("font-weight: 600;")
        mid.addWidget(head)
        sub = W.label("", "Mute", wrap=True)
        color = T.TEXT_MUTE
        if m.effective_mode() == "copy":
            if seg.copy_state == "reading":
                sub.setText("画質そのままで切れる位置を確かめています…")
            elif seg.copy_state == "error":
                sub.setText("画質そのままで切れる位置を読めませんでした")
                color = T.WARN
            elif seg.copy is not None:
                sh = seg.shift
                text = f"実際の開始 {plan.fmt_time_ms(seg.copy.actual)}"
                if sh >= 0.0005:
                    text += f"・開始が {sh:.1f} 秒早まります"
                if sh > SHIFT_WARN_S:
                    text += "。ぴったりを選ぶと指定どおりになります"
                    color = T.WARN
                ov = m.overlap_note(index) if m.effective_output() == "join" else 0.0
                if ov > 0.0005:
                    text += f"・つなぎ目で {ov:.1f} 秒重なります"
                sub.setText(text)
        else:
            sub.setText("指定どおりの位置で作り直します")
        sub.setStyleSheet(f"color: {color}; font-size: 12px;")
        mid.addWidget(sub)
        lay.addLayout(mid, 1)
        go = W.button("移る", "ghost", G.PLAY, on_click=guard(lambda: m.jump_segment(index), "seg:jump"))
        rm = W.button("消す", "ghost", G.DELETE, on_click=guard(lambda: m.remove_segment(index), "seg:remove"))
        rm.setEnabled(not m.busy())
        lay.addWidget(go)
        lay.addWidget(rm)


class ClipTrimPage(W.ScrollPage):
    def __init__(self, module: ClipTrimModule) -> None:
        super().__init__()
        self.m = module
        self.accent = module.accent
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self._build_hero()
        self.notice = _Note(T.INFO, G.INFO)
        self.add(self.notice)
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(guard(lambda: self.notice.setVisible(False), "notice:hide"))
        self._build_empty()
        self._build_editor()
        self._build_segments()
        self._build_export()
        self._build_settings()
        self.finish()
        self._build_shortcuts()
        s = module.signals
        s.video.connect(guard(self._refresh_video, "page:video"))
        s.frame.connect(guard(self._refresh_frame, "page:frame"))
        s.position.connect(guard(self._refresh_position, "page:position"))
        s.thumbs.connect(guard(self._refresh_thumb, "page:thumbs"))
        s.segments.connect(guard(self._refresh_segments, "page:segments"))
        s.job.connect(guard(self._refresh_job, "page:job"))
        s.info.connect(guard(self._refresh_info, "page:info"))
        s.notice.connect(guard(self._show_notice, "page:notice"))
        self._refresh_video()
        self._refresh_thumb(-1)
        self._refresh_segments()
        self._refresh_job()
        self._refresh_info()

    # ================================================================ 共通
    def _parent(self) -> QWidget | None:
        return self.window()

    def _show_notice(self, kind: str, text: str) -> None:
        self.notice.show_text(kind, text)
        self._notice_timer.start(NOTICE_MS)

    def _say(self, err: str | None, kind: str = "warn") -> None:
        if err:
            self._show_notice(kind, err)

    # ================================================================ ドロップ(FR-1)
    def dragEnterEvent(self, e: QDragEnterEvent) -> None:  # noqa: N802
        try:
            if _local_files(e.mimeData()) and not self.m.busy():
                e.acceptProposedAction()
                self.drop.set_hot(True)
                return
        except Exception:  # noqa: BLE001
            pass
        e.ignore()

    def dragMoveEvent(self, e: QDragMoveEvent) -> None:  # noqa: N802
        if e.mimeData() is not None and e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragLeaveEvent(self, e: Any) -> None:  # noqa: N802
        self.drop.set_hot(False)
        super().dragLeaveEvent(e)

    def dropEvent(self, e: QDropEvent) -> None:  # noqa: N802
        self.drop.set_hot(False)
        try:
            files = _local_files(e.mimeData())
            if files:
                e.acceptProposedAction()
                self.m.open_video(files[0])  # 1本だけ開く(FR-1)
        except Exception as ex:  # noqa: BLE001
            _log.error("drop failed: %s", type(ex).__name__)

    def _pick(self) -> None:
        f, _flt = QFileDialog.getOpenFileName(self._parent(), "切り出す動画を選ぶ", "", FILE_FILTER)
        if f:
            self.m.open_video(f)

    # ================================================================ ヒーロー
    def _build_hero(self) -> None:
        from deskkit.catalog import info

        hero = W.Hero("ClipTrim", "録画した動画から、要る区間だけを切り出して新しいファイルにします。元の動画は変えません。",
                      info("cliptrim").glyph or G_CUT, self.accent)
        self.state_pill = W.StatusPill("待機中", "off")
        hero.add_pill(self.state_pill)
        self.pick_btn = W.button("動画を選ぶ", "primary", G.FOLDER, on_click=guard(self._pick, "hero:pick"))
        hero.add_action(self.pick_btn)
        self.add(hero)

    # ================================================================ 動画が無いとき
    def _build_empty(self) -> None:
        self.empty_card = W.Card("動画を開く", "ゲーム・会議・スマホの長い録画から、見せたい所だけを取り出せます。", G_VIDEO, self.accent)
        self.drop = _DropZone(self.accent, guard(self._pick, "drop:pick"))
        self.empty_card.add(self.drop)
        self.opening_label = W.label("", "Dim", wrap=True)
        self.opening_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_card.add(self.opening_label)
        self.add(self.empty_card)

    # ================================================================ 位置を決める(FR-2〜7)
    def _build_editor(self) -> None:
        self.editor = W.Card("位置を決める", None, G_VIDEO, self.accent)
        self.title_label = QLabel()
        self.title_label.setStyleSheet("font-weight: 600;")
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.meta_label = W.label("", "Mute")
        close = W.icon_button(G.CLOSE, "この動画を閉じる", guard(self.m.close_video, "editor:close"))
        self.close_btn = close
        head = QHBoxLayout()
        tb = QVBoxLayout()
        tb.setSpacing(1)
        tb.addWidget(self.title_label)
        tb.addWidget(self.meta_label)
        head.addLayout(tb, 1)
        head.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        self.editor.add_layout(head)
        self.frame_view = FrameView(self.accent)
        self.editor.add(self.frame_view)
        self.timeline = Timeline(self.accent)
        self.timeline.scrubbed.connect(guard(self._scrub, "timeline:scrub"))
        self.editor.add(self.timeline)
        self._scrub_timer = QTimer(self)
        self._scrub_timer.setSingleShot(True)
        self._scrub_timer.setInterval(60)
        self._scrub_timer.timeout.connect(guard(self._scrub_apply, "timeline:apply"))
        self._scrub_to: float | None = None

        row = QHBoxLayout()
        row.setSpacing(6)
        self.time_label = W.label("0:00.0 / 0:00.0", None)
        self.time_label.setStyleSheet("font-weight: 600; font-size: 14px;")
        self.time_label.setMinimumWidth(170)
        row.addWidget(self.time_label)
        self.time_edit = QLineEdit()
        self.time_edit.setPlaceholderText("時刻へ(1:02:10.5・2:30・90)")
        self.time_edit.setMaximumWidth(210)
        self.time_edit.returnPressed.connect(guard(self._go_time, "time:go"))
        row.addWidget(self.time_edit)
        row.addStretch(1)
        self.key_hint = W.label("切れ目 = 画質そのままで切れる位置", "Mute")
        row.addWidget(self.key_hint)
        self.editor.add_layout(row)

        nav = QHBoxLayout()
        nav.setSpacing(4)
        m = self.m
        self.nav_buttons = [
            W.button("◀ 切れ目", "secondary", on_click=guard(lambda: m.step_key(-1), "nav:key-"),
                     tooltip="前の切れ目へ(画質そのままで切れる位置)"),
            W.button("−5秒", "ghost", on_click=guard(lambda: m.step_seconds(-5), "nav:-5"), tooltip="Ctrl+←"),
            W.button("−1秒", "ghost", on_click=guard(lambda: m.step_seconds(-1), "nav:-1"), tooltip="Shift+←"),
            W.button("◀ 1コマ", "secondary", on_click=guard(lambda: m.step_frame(-1), "nav:f-"), tooltip="←"),
            W.button("1コマ ▶", "secondary", on_click=guard(lambda: m.step_frame(1), "nav:f+"), tooltip="→"),
            W.button("+1秒", "ghost", on_click=guard(lambda: m.step_seconds(1), "nav:+1"), tooltip="Shift+→"),
            W.button("+5秒", "ghost", on_click=guard(lambda: m.step_seconds(5), "nav:+5"), tooltip="Ctrl+→"),
            W.button("切れ目 ▶", "secondary", on_click=guard(lambda: m.step_key(1), "nav:key+"),
                     tooltip="次の切れ目へ(画質そのままで切れる位置)"),
        ]
        for b in self.nav_buttons:
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            nav.addWidget(b, 1)
        self.editor.add_layout(nav)

        self.filmstrip = Filmstrip(self.accent)
        self.filmstrip.picked.connect(guard(self._pick_thumb, "filmstrip:pick"))
        self.editor.add(self.filmstrip)

        marks = QHBoxLayout()
        marks.setSpacing(8)
        self.in_btn = W.button("ここから", "secondary", G.UP, on_click=guard(m.set_in, "mark:in"),
                               tooltip="今のコマを区間の始まりにします")
        self.out_btn = W.button("ここまで", "secondary", G.DOWN, on_click=guard(m.set_out, "mark:out"),
                                tooltip="今のコマまでを区間に入れます(このコマを含む)")
        self.marks_label = W.label("", "Dim")
        self.add_btn = W.button("区間を足す", "primary", G.ADD, on_click=guard(self._add_segment, "mark:add"))
        for b in (self.in_btn, self.out_btn, self.add_btn):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        marks.addWidget(self.in_btn)
        marks.addWidget(self.out_btn)
        marks.addWidget(self.marks_label, 1)
        marks.addWidget(self.add_btn)
        self.editor.add_layout(marks)
        self.add(self.editor)

    def _build_shortcuts(self) -> None:
        m = self.m
        keys: list[tuple[Any, Callable[[], None]]] = [
            (Qt.Key.Key_Left, lambda: m.step_frame(-1)),
            (Qt.Key.Key_Right, lambda: m.step_frame(1)),
            (Qt.KeyboardModifier.ShiftModifier | Qt.Key.Key_Left, lambda: m.step_seconds(-1)),
            (Qt.KeyboardModifier.ShiftModifier | Qt.Key.Key_Right, lambda: m.step_seconds(1)),
            (Qt.KeyboardModifier.ControlModifier | Qt.Key.Key_Left, lambda: m.step_seconds(-5)),
            (Qt.KeyboardModifier.ControlModifier | Qt.Key.Key_Right, lambda: m.step_seconds(5)),
        ]
        self._shortcuts: list[QShortcut] = []
        for seq, fn in keys:
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(guard(fn, "key"))
            self._shortcuts.append(sc)

    def _scrub(self, t: float) -> None:
        self._scrub_to = t
        self.time_label.setText(f"{plan.fmt_time(t)} / {plan.fmt_time(self.m.duration)}")
        if not self._scrub_timer.isActive():
            self._scrub_timer.start()

    def _scrub_apply(self) -> None:
        if self._scrub_to is not None:
            self.m.seek(self._scrub_to)
            self._scrub_to = None

    def _go_time(self) -> None:
        t = plan.parse_time(self.time_edit.text())
        if t is None:
            self._show_notice("warn", "時刻を読めませんでした(例: 1:02:10.5 / 2:30 / 90)")
            return
        self.m.seek(t)
        self.time_edit.clear()

    def _pick_thumb(self, i: int) -> None:
        th = self.m.thumbs[i] if 0 <= i < len(self.m.thumbs) else None
        if th is not None:
            self.m.seek(th.pos)
        elif 0 <= i < len(self.filmstrip.targets):
            self.m.seek(self.filmstrip.targets[i])

    def _add_segment(self) -> None:
        self._say(self.m.add_segment())

    # ================================================================ 区間(FR-8)
    def _build_segments(self) -> None:
        self.seg_card = W.Card("区間", "押すとその始まりへ移ります。最大 20 個。", G.LIST, self.accent)
        self.seg_box = QVBoxLayout()
        self.seg_box.setSpacing(6)
        self.seg_card.add_layout(self.seg_box)
        self.seg_empty = W.EmptyState(G.ADD, "まだ区間がありません", "「ここから」「ここまで」で決めて「区間を足す」を押します。")
        self.seg_card.add(self.seg_empty)
        self.add(self.seg_card)

    # ================================================================ 書き出す(FR-9〜16)
    def _build_export(self) -> None:
        m = self.m
        card = W.Card("書き出す", "元の動画の隣に「<元の名前>_clip」で保存します。", G.SAVE, self.accent)
        self.mode_seg = W.Segmented([("copy", "画質そのまま(速い)"), ("precise", "ぴったり(少し時間がかかる)")],
                                    m.config.mode, self.accent)
        self.mode_seg.changed.connect(guard(lambda v: self._say(m.set_mode(v)), "export:mode"))
        card.add(W.SettingRow("切り方", "画質そのままは切れ目から切るので、開始が少し早まることがあります。", self.mode_seg))
        self.out_seg = W.Segmented([("separate", "区間ごとに別のファイル"), ("join", "つなげて1本")], m.config.output, self.accent)
        self.out_seg.changed.connect(guard(lambda v: self._say(m.set_output(v)), "export:output"))
        card.add(W.SettingRow("書き出し方", "区間が2つ以上のときに選べます。", self.out_seg))
        self.meta_seg = W.Segmented([("strip", "消す"), ("keep", "残す")],
                                    "strip" if m.config.strip_metadata else "keep", self.accent)
        self.meta_seg.changed.connect(guard(lambda v: self._say(m.set_strip_metadata(v == "strip")), "export:meta"))
        card.add(W.SettingRow("撮影場所などの情報", "撮影した場所・日時・機種など。人に送るなら「消す」が安心です。"
                              "回転の情報はどちらでも残ります。", self.meta_seg))
        self.mode_notes = QVBoxLayout()
        self.mode_notes.setSpacing(6)
        card.add_layout(self.mode_notes)
        row = QHBoxLayout()
        self.estimate_label = W.label("", "Mute", wrap=True)
        row.addWidget(self.estimate_label, 1)
        self.go_btn = W.button("切り出す", "primary", G_CUT, on_click=guard(self._go, "export:go"))
        self.go_btn.setMinimumWidth(160)
        row.addWidget(self.go_btn)
        card.add_layout(row)
        prog = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        self.progress_label = W.label("", "Dim")
        self.cancel_btn = W.button("中止", "danger", G.CLOSE, on_click=guard(m.cancel_export, "export:cancel"))
        prog.addWidget(self.progress_label)
        prog.addWidget(self.progress, 1)
        prog.addWidget(self.cancel_btn)
        self.prog_box = QWidget()
        self.prog_box.setLayout(prog)
        card.add(self.prog_box)
        self.results_box = QVBoxLayout()
        self.results_box.setSpacing(6)
        card.add_layout(self.results_box)
        self.export_card = card
        self.add(card)

    def _go(self) -> None:
        self._say(self.m.export())

    # ================================================================ 設定
    def _build_settings(self) -> None:
        m = self.m
        card = W.Card("設定", None, G.SETTINGS, self.accent)
        self.sendto_toggle = W.ToggleSwitch(m.config.sendto_enabled, self.accent)
        self.sendto_toggle.toggled.connect(guard(self._toggle_sendto, "settings:sendto"))
        card.add(W.SettingRow("エクスプローラーの「送る」に追加",
                              "動画を右クリック →「送る」→「ClipTrim で切り出す」で開けます。オフにすると、追加したものだけを消します。",
                              self.sendto_toggle))
        self.sendto_note = W.label("", "Mute", wrap=True)
        card.add(self.sendto_note)
        spin = QSpinBox()
        spin.setRange(cfgmod.FILMSTRIP_MIN, cfgmod.FILMSTRIP_MAX)
        spin.setValue(m.config.filmstrip_count)
        spin.setSuffix(" 枚")
        spin.setKeyboardTracking(False)
        spin.valueChanged.connect(guard(lambda v: self._say(m.set_filmstrip_count(int(v))), "settings:strip"))
        self.strip_spin = spin
        card.add(W.SettingRow("フィルムストリップの枚数", "動画の長さを等分した位置のコマを並べます(8〜48 枚)。", spin))
        self.ffmpeg_label = W.label("", "Mute")
        card.add(W.SettingRow("動画の部品", None, self.ffmpeg_label))
        self.add(card)

    def _toggle_sendto(self, v: bool) -> None:
        err = self.m.set_sendto(v)
        if err:
            self.sendto_toggle.set_checked_silent(not v)
            self._show_notice("warn", err)
        self._refresh_info()

    # ================================================================ 表示の更新
    def _refresh_video(self) -> None:
        m = self.m
        v = m.video
        has = v is not None
        self.empty_card.setVisible(not has)
        self.editor.setVisible(has)
        self.seg_card.setVisible(has)
        self.export_card.setVisible(has)
        if m.opening:
            self.opening_label.setText(m.opening_phase or "動画を読んでいます…")
            self.state_pill.set_state("info", "開いています")
        else:
            self.opening_label.setText(m.error)
            self.opening_label.setStyleSheet(f"color: {T.WARN};" if m.error else "")
        if v is None:
            if not m.opening:
                self.state_pill.set_state("off", "待機中")
            return
        info = v.info
        self.title_label.setText(v.path.name)
        self.title_label.setToolTip(str(v.path))
        parts = [plan.fmt_time(m.duration)]
        ds = info.display_size
        if ds:
            parts.append(f"{ds[0]}×{ds[1]}")
        if info.fps:
            fps = f"{info.fps:.2f}".rstrip("0").rstrip(".")
            parts.append(f"{fps} コマ/秒")
        parts.append(plan.fmt_bytes(v.size))
        if info.audio_count > 1:
            parts.append(f"音声 {info.audio_count} 本")
        elif info.audio_count == 0:
            parts.append("音声なし")
        self.meta_label.setText("・".join(parts))
        self.timeline.duration = m.duration
        self.filmstrip.duration = m.duration
        self.state_pill.set_state("accent", "編集中")
        self._refresh_position()
        self._refresh_frame()

    def _refresh_frame(self) -> None:
        m = self.m
        a = m.around
        badge = ""
        if a is not None and any(abs(k.pos - m.pos) <= 1e-6 for k in a.keys):
            badge = "切れ目"
        self.frame_view.set_state(m.frame_png, m.frame_loading, m.frame_error, plan.fmt_time_ms(m.pos), badge)

    def _refresh_position(self) -> None:
        m = self.m
        if m.video is None:
            return
        self.time_label.setText(f"{plan.fmt_time(m.pos)} / {plan.fmt_time(m.duration)}")
        tl = self.timeline
        if not tl._drag:
            tl.at = m.pos
        tl.segments = [(s.start, s.end, s.copy.actual if s.copy is not None and m.effective_mode() == "copy" else None)
                       for s in m.segments]
        tl.mark_in, tl.mark_out = m.mark_in, m.mark_out
        tl.update()
        self.filmstrip.at = m.pos
        self.filmstrip.update()
        parts = []
        if m.mark_in is not None:
            parts.append(f"始まり {plan.fmt_time_ms(m.mark_in)}")
        if m.mark_out is not None:
            parts.append(f"終わり {plan.fmt_time_ms(m.mark_out)}")
        self.marks_label.setText("・".join(parts) if parts else "「ここから」「ここまで」で区間を決めます")
        busy = m.busy()
        for b in (self.in_btn, self.out_btn, self.add_btn):
            b.setEnabled(not busy)
        self._refresh_frame()

    def _refresh_thumb(self, index: int) -> None:
        m = self.m
        fs = self.filmstrip
        if index < 0 or len(fs.pixmaps) != len(m.thumbs):
            from deskkit.modules.cliptrim import frames

            fs.pixmaps = [None] * len(m.thumbs)
            fs.targets = frames.thumb_targets(m.duration, m.config.filmstrip_count) if m.video is not None else []
            for i, th in enumerate(m.thumbs):
                if th is not None:
                    fs.pixmaps[i] = _pixmap(th.png)
        elif 0 <= index < len(m.thumbs):
            th = m.thumbs[index]
            fs.pixmaps[index] = _pixmap(th.png) if th is not None else None
        fs.setVisible(bool(fs.pixmaps))
        fs.update()

    def _clear_layout(self, lay: QVBoxLayout) -> None:
        while lay.count():
            it = lay.takeAt(0)
            w = it.widget() if it is not None else None
            if w is not None:
                w.hide()
                w.setParent(None)  # 消える前の古い行が重なって描かれないように、すぐ外す
                w.deleteLater()

    def _refresh_segments(self) -> None:
        m = self.m
        self._clear_layout(self.seg_box)
        for i in range(len(m.segments)):
            row = _SegRow(self, i)
            self.seg_box.addWidget(row)
        self.seg_empty.setVisible(not m.segments)
        busy = m.busy()
        eff = m.effective_mode()
        self.mode_seg.set_value(eff, animate=False)
        self.mode_seg.setEnabled(not busy and m.copy_ok() and m.precise_state() != "unavailable")
        self.out_seg.set_value(m.config.output if len(m.segments) > 1 else "separate", animate=False)
        self.out_seg.setEnabled(not busy and len(m.segments) > 1)
        self.meta_seg.set_value("strip" if m.config.strip_metadata else "keep", animate=False)
        self.meta_seg.setEnabled(not busy)
        self._clear_layout(self.mode_notes)
        for kind, text in m.mode_notes():
            n = _Note(T.INFO, G.INFO)
            n.show_text(kind, text)
            self.mode_notes.addWidget(n)
        est = m.estimate_text()
        if not m.segments:
            est = "区間を足すと、ここに見積もりが出ます"
        elif m.copy_pending():
            est = "区間の位置を確かめています…"
        self.estimate_label.setText(est)
        self.go_btn.setEnabled(bool(m.segments) and not busy and m.can_cut())
        self._refresh_position()

    def _refresh_job(self) -> None:
        m = self.m
        busy = m.busy()
        job = m.job
        self.prog_box.setVisible(busy)
        if busy and job is not None:
            self.progress.setValue(int(job.progress * 1000))
            self.progress_label.setText(f"書き出しています… {int(job.progress * 100)}%")
        self.go_btn.setEnabled(bool(m.segments) and not busy and m.can_cut())
        self.pick_btn.setEnabled(not busy)
        self.close_btn.setEnabled(not busy)
        if busy:
            self.state_pill.set_state("info", "書き出し中")
        elif m.video is not None:
            self.state_pill.set_state("accent", "編集中")
        self._clear_layout(self.results_box)
        res = m.last_result
        if res is None or busy:
            return
        head = W.label({"ok": "完了", "partial": "一部だけ完了", "cancelled": "中止しました"}.get(res.result, "書き出せませんでした"),
                       "H3")
        self.results_box.addWidget(head)
        if res.message and not res.outputs:
            n = _Note(T.WARN, G.WARNING)
            n.show_text("warn" if res.result != "error" else "error", res.message)
            self.results_box.addWidget(n)
        for o in res.outputs:
            row = QFrame()
            row.setObjectName("Inset")
            lay = QHBoxLayout(row)
            lay.setContentsMargins(12, 8, 8, 8)
            if o.ok and o.path is not None:
                g = W.Glyph(G.CHECK, 14, T.SUCCESS)
                lay.addWidget(g)
                name = QLabel(o.path.name)
                name.setToolTip(str(o.path))
                name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                lay.addWidget(name, 1)
                lay.addWidget(W.label(f"{plan.fmt_seconds(o.seconds)}・{plan.fmt_bytes(o.bytes)}", "Dim"))
                p = o.path
                lay.addWidget(W.button("フォルダを開く", "ghost", G.FOLDER,
                                       on_click=guard(lambda p=p: m.open_folder(p), "result:folder")))
            else:
                g = W.Glyph(G.WARNING, 14, T.WARN)
                lay.addWidget(g)
                lay.addWidget(W.label(f"{o.index + 1} 本目: {o.message or '書き出せませんでした'}", None, wrap=True), 1)
            self.results_box.addWidget(row)
        if res.fallback and res.folder is not None:
            n = _Note(T.INFO, G.INFO)
            n.show_text("info", f"元のフォルダに書けなかったため、{res.folder} に保存しました")
            self.results_box.addWidget(n)
        if res.ok_count:
            self.results_box.addWidget(W.label("送る前に小さくするなら SendPrep", "Mute"))

    def _refresh_info(self) -> None:
        m = self.m
        st = m.ffmpeg().state()
        text = {_ff.STATE_EXTRACTED: "準備できています", _ff.STATE_NOT_EXTRACTED: "初めて使うときに数秒で用意します",
                _ff.STATE_MISSING: "見つかりません(DeskKit を入れ直してください)",
                _ff.STATE_VERIFY_FAILED: "壊れています(DeskKit を入れ直してください)"}.get(st, st)
        h = m.precise_state()
        if h == "unavailable":
            text += "・この PC では「ぴったり」が使えません"
        self.ffmpeg_label.setText(text)
        stt = m.sendto_status()
        self.sendto_toggle.set_checked_silent(m.config.sendto_enabled and stt == sendto.STATUS_REGISTERED)
        self.sendto_note.setText("「送る」に同じ名前のショートカットがあります(DeskKit が作ったものではないので触りません)"
                                 if stt == sendto.STATUS_OTHER else "")
        self.sendto_note.setVisible(stt == sendto.STATUS_OTHER)
        if self.strip_spin.value() != m.config.filmstrip_count:
            self.strip_spin.blockSignals(True)
            self.strip_spin.setValue(m.config.filmstrip_count)
            self.strip_spin.blockSignals(False)
