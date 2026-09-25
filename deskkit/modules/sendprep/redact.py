# 伏せ字エディタ(別ウィンドウのダイアログ。FR-10〜13・FR-19)と、伏せ字を画素に焼き込む処理(S-6)。
# 文字認識は開いたときに裏で1回だけ走らせ、結果は点線の「候補」として画面に出すだけ(自動では伏せない。S-7)。
# 候補の文字列は持たない(種類と枠だけ。INV-4)。塗りつぶしの色は伏せ字の中身そのものなのでテーマではなく黒に固定する。
from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QImage, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QDialog, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget

from deskkit.ui import theme as T
from deskkit.ui import widgets as W
from deskkit.ui.theme import G

if TYPE_CHECKING:
    from PIL import Image

    from deskkit.modules.sendprep.ocr import Candidate

Rect = tuple[int, int, int, int]  # x, y, w, h(画像の座標)
FILL_RGB = (0, 0, 0)
MOSAIC_MIN_BLOCK = 12
CLICK_SLOP = 5
MSG_OCR_UNAVAILABLE = "この PC では自動検出が使えません。手で囲んでください"

_log = logging.getLogger("deskkit.sendprep")


def guard(fn: Callable[..., Any], label: str = "ui") -> Callable[..., Any]:
    """Qt のシグナルから呼ぶ処理の例外を握る。ログには例外の型名だけを書く(パスを含む文を書かない。INV-5)。"""

    def w(*a: Any, **k: Any) -> Any:
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001
            _log.error("handler %s failed: %s", label, type(e).__name__)
            return None

    return w


# ------------------------------------------------------------------ 焼き込み(S-6)
def mosaic_block(w: int, h: int) -> int:
    """モザイクのブロック: 囲んだ範囲の短辺の 1/3 以上、最小 12px。"""
    return max(MOSAIC_MIN_BLOCK, math.ceil(min(w, h) / 3))


def clip_rect(r: Rect, size: tuple[int, int]) -> Rect | None:
    x, y, w, h = r
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(size[0], x + w), min(size[1], y + h)
    if x1 - x0 < 1 or y1 - y0 < 1:
        return None
    return x0, y0, x1 - x0, y1 - y0


def burn(img: Image.Image, rects: Sequence[Rect], style: str) -> Image.Image:
    """伏せ字を画素に書き込んだ新しい画像を返す(元の画像は変えない)。"""
    from PIL import Image as PILImage

    out = img.copy()
    if out.mode not in ("RGB", "RGBA", "L", "LA"):
        out = out.convert("RGBA" if "A" in out.getbands() or "transparency" in out.info else "RGB")
    for r in rects:
        c = clip_rect(r, out.size)
        if c is None:
            continue
        x, y, w, h = c
        if style == "mosaic":
            b = mosaic_block(w, h)
            nx, ny = math.ceil(w / b), math.ceil(h / b)
            region = out.crop((x, y, x + w, y + h))
            # ブロックの格子に合わせて平均し、拡大してから範囲の大きさに切る
            padded = PILImage.new(region.mode, (nx * b, ny * b))
            padded.paste(region, (0, 0))
            small = padded.resize((nx, ny), PILImage.Resampling.BOX)
            big = small.resize((nx * b, ny * b), PILImage.Resampling.NEAREST).crop((0, 0, w, h))
            out.paste(big, (x, y))
        else:
            if out.mode in ("L", "LA"):
                fill: Any = 0 if out.mode == "L" else (0, 255)
            elif out.mode == "RGBA":
                fill = (*FILL_RGB, 255)
            else:
                fill = FILL_RGB
            out.paste(fill, (x, y, x + w, y + h))
    return out


def pil_to_qimage(img: Image.Image) -> QImage:
    rgba = img.convert("RGBA")
    data = rgba.tobytes("raw", "RGBA")
    return QImage(data, rgba.width, rgba.height, rgba.width * 4, QImage.Format.Format_RGBA8888).copy()


def qimage_to_pil(q: QImage) -> Image.Image:
    from PIL import Image as PILImage

    img = q.convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = img.width(), img.height()
    ptr = img.constBits()
    raw = bytes(ptr)[: img.bytesPerLine() * h]
    pil = PILImage.frombuffer("RGBA", (w, h), raw, "raw", "RGBA", img.bytesPerLine(), 1).copy()
    lo, _hi = pil.getchannel("A").getextrema()
    if lo == 255:
        pil = pil.convert("RGB")
    return pil


# ------------------------------------------------------------------ 画面: 画像の上で四角を描く
class RedactCanvas(QWidget):
    """画像を縮めて表示し、ドラッグで伏せる範囲を足し、クリックで消す。候補は点線の枠で出し、クリックで伏せる範囲にする。"""

    changed = Signal()

    def __init__(self, image: QImage, accent: str) -> None:
        super().__init__()
        self._img = image
        self._accent = accent
        self.rects: list[Rect] = []
        self.candidates: list[Candidate] = []
        self._adopted: set[int] = set()  # 伏せる範囲にした候補の番号
        self._press: QPoint | None = None
        self._drag: QPoint | None = None
        self.redact_style = "fill"
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMinimumSize(360, 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(900, 600)

    # ---- 座標の変換
    def _geom(self) -> tuple[float, float, float]:
        iw, ih = max(1, self._img.width()), max(1, self._img.height())
        m = 8
        s = min((self.width() - 2 * m) / iw, (self.height() - 2 * m) / ih, 2.0)
        s = max(s, 0.01)
        ox = (self.width() - iw * s) / 2
        oy = (self.height() - ih * s) / 2
        return s, ox, oy

    def to_image(self, p: QPoint | QPointF) -> tuple[int, int]:
        s, ox, oy = self._geom()
        return int((p.x() - ox) / s), int((p.y() - oy) / s)

    def to_widget(self, r: Rect) -> QRectF:
        s, ox, oy = self._geom()
        return QRectF(ox + r[0] * s, oy + r[1] * s, r[2] * s, r[3] * s)

    # ---- 範囲の操作
    def all_rects(self) -> list[Rect]:
        return list(self.rects) + [self.candidates[i].rect for i in sorted(self._adopted) if i < len(self.candidates)]

    def count(self) -> int:
        return len(self.all_rects())

    def set_candidates(self, cands: Sequence[Candidate]) -> None:
        self.candidates = list(cands)
        self._adopted.clear()
        self.update()
        self.changed.emit()

    def adopt_all(self) -> None:
        self._adopted = set(range(len(self.candidates)))
        self.update()
        self.changed.emit()

    def clear_all(self) -> None:
        self.rects.clear()
        self._adopted.clear()
        self.update()
        self.changed.emit()

    def add_rect(self, r: Rect) -> None:
        c = clip_rect(r, (self._img.width(), self._img.height()))
        if c is not None and c[2] >= 2 and c[3] >= 2:
            self.rects.append(c)
            self.update()
            self.changed.emit()

    def click_at(self, x: int, y: int) -> None:
        """クリック: 伏せる範囲の上なら消す(新しいものから)、候補の上なら伏せる範囲にする。"""
        for i in range(len(self.rects) - 1, -1, -1):
            rx, ry, rw, rh = self.rects[i]
            if rx <= x < rx + rw and ry <= y < ry + rh:
                del self.rects[i]
                self.update()
                self.changed.emit()
                return
        for i, c in enumerate(self.candidates):
            rx, ry, rw, rh = c.rect
            if rx <= x < rx + rw and ry <= y < ry + rh:
                if i in self._adopted:
                    self._adopted.discard(i)
                else:
                    self._adopted.add(i)
                self.update()
                self.changed.emit()
                return

    # ---- マウス
    def mousePressEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        try:
            if e.button() == Qt.MouseButton.LeftButton:
                self._press = e.position().toPoint()
                self._drag = self._press
        except Exception as ex:  # noqa: BLE001
            _log.error("canvas press failed: %s", type(ex).__name__)

    def mouseMoveEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        try:
            if self._press is not None:
                self._drag = e.position().toPoint()
                self.update()
        except Exception as ex:  # noqa: BLE001
            _log.error("canvas move failed: %s", type(ex).__name__)

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        try:
            if self._press is None or e.button() != Qt.MouseButton.LeftButton:
                return
            a, b = self._press, e.position().toPoint()
            self._press = self._drag = None
            if abs(a.x() - b.x()) < CLICK_SLOP and abs(a.y() - b.y()) < CLICK_SLOP:
                self.click_at(*self.to_image(b))
            else:
                x0, y0 = self.to_image(QPoint(min(a.x(), b.x()), min(a.y(), b.y())))
                x1, y1 = self.to_image(QPoint(max(a.x(), b.x()), max(a.y(), b.y())))
                self.add_rect((x0, y0, x1 - x0 + 1, y1 - y0 + 1))
            self.update()
        except Exception as ex:  # noqa: BLE001
            _log.error("canvas release failed: %s", type(ex).__name__)

    # ---- 描画
    def paintEvent(self, _e: QPaintEvent) -> None:  # noqa: N802
        try:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            s, ox, oy = self._geom()
            target = QRectF(ox, oy, self._img.width() * s, self._img.height() * s)
            p.fillRect(self.rect(), QColor(T.SURFACE2))
            p.drawImage(target, self._img)
            p.setPen(QPen(QColor(T.BORDER_HI), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(target)
            # 候補(点線)
            accent = QColor(self._accent)
            for i, c in enumerate(self.candidates):
                if i in self._adopted:
                    continue
                pen = QPen(accent, 2, Qt.PenStyle.DashLine)
                p.setPen(pen)
                fillc = QColor(accent)
                fillc.setAlpha(40)
                p.setBrush(fillc)
                p.drawRect(self.to_widget(c.rect))
            # 伏せる範囲(焼き込んだときの見た目に近く)
            for r in self.all_rects():
                wr = self.to_widget(r)
                if self.redact_style == "mosaic":
                    p.setPen(QPen(accent, 1.5))
                    p.setBrush(QBrush(QColor(*FILL_RGB, 170), Qt.BrushStyle.Dense4Pattern))
                else:
                    p.setPen(QPen(accent, 1.5))
                    p.setBrush(QColor(*FILL_RGB, 235))
                p.drawRect(wr)
            if self._press is not None and self._drag is not None:
                p.setPen(QPen(accent, 1.5, Qt.PenStyle.DashLine))
                band = QColor(accent)
                band.setAlpha(50)
                p.setBrush(band)
                p.drawRect(QRect(self._press, self._drag).normalized())
            p.end()
        except Exception as ex:  # noqa: BLE001
            _log.error("canvas paint failed: %s", type(ex).__name__)


class _OcrBridge(QObject):
    done = Signal(object)  # list[Candidate] か None(使えない)


class RedactEditor(QDialog):
    """伏せ字エディタ。on_apply(rects, style) が焼き込みを始める。終わったら finish()/fail() が呼ばれる。"""

    def __init__(self, parent: QWidget | None, image: Image.Image, *, accent: str, style: str,
                 on_apply: Callable[[list[Rect], str], None], title: str = "伏せ字",
                 clipboard_mode: bool = False,
                 find: Callable[[Image.Image], list[Candidate]] | None = None,
                 on_style: Callable[[str], None] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"SendPrep — {title}")
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._on_apply = on_apply
        self._on_style = on_style
        self._busy = False
        self.clipboard_mode = clipboard_mode
        self.image = image
        self.accent = accent
        self.ocr_state = "running" if find is not None else "unavailable"
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)
        head = QHBoxLayout()
        gl = W.Glyph(G.EDIT, 18, accent)
        gl.setFixedSize(36, 36)
        gl.setStyleSheet(f"color: {accent}; background: {T.alpha(accent, 0.14)}; border-radius: 10px;")
        head.addWidget(gl)
        tb = QVBoxLayout()
        tb.setSpacing(0)
        tb.addWidget(W.label("伏せ字", "H2"))
        tb.addWidget(W.label("ドラッグで囲むと伏せます。囲んだ所をクリックすると外せます。", "Mute", wrap=True))
        head.addLayout(tb, 1)
        self.ocr_pill = W.StatusPill("文字を探しています…", "info")
        head.addWidget(self.ocr_pill, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(head)
        self.note = W.label("", "Dim", wrap=True)
        self.note.setVisible(False)
        lay.addWidget(self.note)
        self.canvas = RedactCanvas(pil_to_qimage(image), accent)
        self.canvas.redact_style = style
        self.canvas.changed.connect(guard(self._update_count, "editor:count"))
        lay.addWidget(self.canvas, 1)
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self.style_seg = W.Segmented([("fill", "塗りつぶし"), ("mosaic", "モザイク")], style, accent)
        self.style_seg.changed.connect(guard(self._set_style, "editor:style"))
        row1.addWidget(self.style_seg)
        row1.addSpacing(8)
        self.adopt_btn = W.button("候補をすべて伏せる", "secondary", G.SPARKLE, on_click=guard(self.canvas.adopt_all, "editor:adopt"))
        self.adopt_btn.setEnabled(False)
        row1.addWidget(self.adopt_btn)
        row1.addWidget(W.button("すべて外す", "ghost", G.CLEAR, on_click=guard(self.canvas.clear_all, "editor:clear")))
        row1.addStretch(1)
        lay.addLayout(row1)
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self.count_label = W.label("", "Mute")
        row2.addWidget(self.count_label)
        row2.addStretch(1)
        row2.addWidget(W.button("閉じる", "ghost", on_click=self.reject))
        self.apply_btn = W.button("クリップボードに戻す" if clipboard_mode else "伏せ字を焼き込む", "primary",
                                  G.PASTE if clipboard_mode else G.SAVE, on_click=guard(self._apply, "editor:apply"))
        row2.addWidget(self.apply_btn)
        lay.addLayout(row2)
        self._update_count()
        self._fit_to_screen()
        W.dark_titlebar(self)
        self._bridge = _OcrBridge()
        self._bridge.done.connect(guard(self._on_ocr, "editor:ocr"))
        if find is not None:
            self._start_ocr(find)
        else:
            self._on_ocr(None)

    def _fit_to_screen(self) -> None:
        scr = self.screen().availableGeometry() if self.screen() is not None else QRect(0, 0, 1280, 800)
        self.resize(int(scr.width() * 0.72), int(scr.height() * 0.8))

    # ---- 文字認識(裏で1回)
    def _start_ocr(self, find: Callable[[Image.Image], list[Candidate]]) -> None:
        img = self.image
        bridge = self._bridge

        def work() -> None:
            res: Any
            try:
                res = find(img)
            except Exception:  # noqa: BLE001 - 使えない・失敗は手で囲む操作だけにする(FR-13)
                res = None
            try:
                bridge.done.emit(res)
            except RuntimeError:
                pass  # 画面が先に閉じられた

        threading.Thread(target=work, name="sendprep-ocr", daemon=True).start()

    def _on_ocr(self, res: Any) -> None:
        if res is None:
            self.ocr_state = "unavailable"
            self.ocr_pill.set_state("off", "自動検出なし")
            self.note.setText(MSG_OCR_UNAVAILABLE)
            self.note.setVisible(True)
            return
        self.ocr_state = "done"
        cands = list(res)
        self.canvas.set_candidates(cands)
        self.adopt_btn.setEnabled(bool(cands))
        if cands:
            self.ocr_pill.set_state("warn", f"候補 {len(cands)} か所")
            self.note.setText("点線の枠は、個人情報かもしれない文字です。クリックすると伏せます(自動では伏せません)。")
            self.note.setVisible(True)
        else:
            self.ocr_pill.set_state("ok", "候補なし")

    # ---- 操作
    def _set_style(self, v: str) -> None:
        self.canvas.redact_style = v
        self.canvas.update()
        if self._on_style is not None:
            self._on_style(v)

    def _update_count(self) -> None:
        n = self.canvas.count()
        self.count_label.setText(f"伏せる所: {n} か所" if n else "伏せる所はまだありません")
        if not self._busy:
            self.apply_btn.setEnabled(n > 0 or self.clipboard_mode)

    def _apply(self) -> None:
        if self._busy:
            return
        rects = self.canvas.all_rects()
        if not rects and not self.clipboard_mode:
            return
        self.set_busy(True)
        self._on_apply(rects, self.canvas.redact_style)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.apply_btn.setEnabled(not busy)
        self.apply_btn.setText("処理しています…" if busy else ("クリップボードに戻す" if self.clipboard_mode else "伏せ字を焼き込む"))

    def finish(self) -> None:
        self._busy = False
        self.accept()

    def fail(self, message: str, *, close: bool = False) -> None:
        self.set_busy(False)
        self._update_count()
        W.message(self, "伏せ字", message, kind="error")
        if close:
            self.reject()
