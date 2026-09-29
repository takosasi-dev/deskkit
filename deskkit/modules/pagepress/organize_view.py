# ページの整理の格子(FR-7・FR-8・P-15)。QListView の IconMode + モデル + 描き役で、1ページ1ウィジェットにしない。
# 並べ替え・回転・抜くは OrganizeState(Qt に依らない純粋なクラス)が持ち、取り消しはメモリだけ(100 手まで)。
# 回転はサムネイルを回して見せ、描き直さない。見えている範囲は、スクロールが止まって 100ms 後に描画スレッドへ頼み直す(P-2)。
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QAbstractItemView, QListView, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QWidget

from deskkit.ui import theme as T

MAX_UNDO = 100
CELL = QSize(150, 206)
THUMB_BOX = QSize(130, 168)
AHEAD, BEHIND = 16, 8
DEBOUNCE_MS = 100

Item = tuple[int, int]  # (元のページ番号 0 始まり, 足す回転)


class OrganizeState:
    """整理中の並びと回転。保存するまで何も書かない(P-15)。"""

    def __init__(self, pages: int) -> None:
        self.items: list[Item] = [(i, 0) for i in range(pages)]
        self._saved: list[Item] = list(self.items)
        self._undo: list[list[Item]] = []
        self._redo: list[list[Item]] = []

    @property
    def dirty(self) -> bool:
        return self.items != self._saved

    def mark_saved(self, items: Sequence[Item] | None = None) -> None:
        """保存した並びを覚える(保存の途中で変えた分は「保存していない変更」のまま)。"""
        self._saved = list(self.items if items is None else items)

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def _push(self) -> None:
        self._undo.append(list(self.items))
        if len(self._undo) > MAX_UNDO:
            del self._undo[0]
        self._redo.clear()

    def rotate(self, rows: Sequence[int], delta: int) -> None:
        rows = sorted({r for r in rows if 0 <= r < len(self.items)})
        if not rows:
            return
        self._push()
        for r in rows:
            o, rot = self.items[r]
            self.items[r] = (o, (rot + delta) % 360)

    def remove(self, rows: Sequence[int]) -> None:
        drop = {r for r in rows if 0 <= r < len(self.items)}
        if not drop:
            return
        self._push()
        self.items = [it for k, it in enumerate(self.items) if k not in drop]

    def move(self, rows: Sequence[int], dest: int) -> list[int]:
        """rows を dest(元の並びでの挿入位置)へ動かす。動かしたあとの行番号を返す。"""
        sel = sorted({r for r in rows if 0 <= r < len(self.items)})
        if not sel:
            return []
        dest = max(0, min(dest, len(self.items)))
        moving = [self.items[r] for r in sel]
        rest = [it for k, it in enumerate(self.items) if k not in set(sel)]
        at = dest - sum(1 for r in sel if r < dest)
        new = rest[:at] + moving + rest[at:]
        if new == self.items:
            return sel
        self._push()
        self.items = new
        return list(range(at, at + len(moving)))

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(list(self.items))
        self.items = self._undo.pop()
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(list(self.items))
        self.items = self._redo.pop()
        return True


class PageModel(QAbstractListModel):
    ItemRole = Qt.ItemDataRole.UserRole + 1

    def __init__(self, state: OrganizeState | None = None) -> None:
        super().__init__()
        self.state = state

    def set_state(self, state: OrganizeState | None) -> None:
        self.beginResetModel()
        self.state = state
        self.endResetModel()

    def refresh(self) -> None:
        self.beginResetModel()
        self.endResetModel()

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008, N802
        if parent.isValid() or self.state is None:
            return 0
        return len(self.state.items)

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if self.state is None or not index.isValid() or not 0 <= index.row() < len(self.state.items):
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return str(index.row() + 1)
        if role == self.ItemRole:
            return self.state.items[index.row()]
        if role == Qt.ItemDataRole.ToolTipRole:
            o, rot = self.state.items[index.row()]
            return f"元の {o + 1} ページ目" + (f"・{rot} 度回す" if rot else "")
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
        if not index.isValid():
            return Qt.ItemFlag.ItemIsDropEnabled
        return base

    def supportedDropActions(self) -> Qt.DropAction:  # noqa: N802
        return Qt.DropAction.MoveAction


class PageDelegate(QStyledItemDelegate):
    """1ページ分の枠: サムネイル(回転を当てて描く)・番号。未描画は灰色の枠、描けなければ「表示できません」。"""

    def __init__(self, thumb: Callable[[int], tuple[bool, Any]], accent: Callable[[], str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._thumb = thumb
        self._accent = accent

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex) -> QSize:  # noqa: N802
        return CELL

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex) -> None:
        item = index.data(PageModel.ItemRole)
        if item is None:
            return
        orig, rot = item
        r = QRect(option.rect).adjusted(5, 5, -5, -5)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        accent = QColor(self._accent())
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        card = QPainterPath()
        card.addRoundedRect(QRectF(r), 12, 12)
        bg = QColor(accent) if selected else QColor(T.SURFACE2)
        if selected:
            bg.setAlpha(46)
        p.fillPath(card, bg)
        p.setPen(QPen(accent if selected else QColor(T.BORDER), 2 if selected else 1))
        p.drawPath(card)
        box = QRect(r.left() + (r.width() - THUMB_BOX.width()) // 2, r.top() + 8, THUMB_BOX.width(), THUMB_BOX.height())
        has, img = self._thumb(orig)
        if has and img is not None and not img.isNull():
            iw, ih = img.width(), img.height()
            if rot in (90, 270):
                iw, ih = ih, iw
            k = min(box.width() / max(1, iw), box.height() / max(1, ih))
            dw, dh = iw * k, ih * k
            p.save()
            p.translate(box.center().x() + 0.5, box.center().y() + 0.5)
            p.rotate(rot)
            if rot in (90, 270):
                dw, dh = dh, dw
            target = QRectF(-dw / 2, -dh / 2, dw, dh)
            shadow = QColor(0, 0, 0, 40)
            p.fillRect(target.translated(0, 2), shadow)
            p.drawImage(target, img)
            p.restore()
        else:
            ph = QPainterPath()
            inner = QRectF(box).adjusted(14, 6, -14, -6)
            ph.addRoundedRect(inner, 6, 6)
            p.fillPath(ph, QColor(T.SURFACE3))
            if has:  # 描けなかった(FR-7)
                p.setPen(QColor(T.TEXT_MUTE))
                f = QFont(p.font())
                f.setPixelSize(11)
                p.setFont(f)
                p.drawText(inner, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, "表示できません")
        num = QRect(r.left(), box.bottom() + 6, r.width(), r.bottom() - box.bottom() - 8)
        f = QFont(p.font())
        f.setPixelSize(12)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(T.TEXT) if selected else QColor(T.TEXT_DIM))
        label = str(index.row() + 1)
        if rot:
            label += f"  ↻{rot}°"
        p.drawText(num, Qt.AlignmentFlag.AlignCenter, label)
        p.restore()


class OrganizeView(QListView):
    """格子。ドラッグで並べ替え、Delete で抜く、Ctrl+Z / Ctrl+Y で取り消し・やり直し(FR-8)。"""

    moved = Signal(list, int)          # (動かす行, 挿入位置)
    delete_pressed = Signal()
    undo_pressed = Signal()
    redo_pressed = Signal()
    window_changed = Signal(int, int)  # (見えている最初の行, 最後の行)。スクロールが止まって 100ms 後

    def __init__(self) -> None:
        super().__init__()
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setMovement(QListView.Movement.Static)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setUniformItemSizes(True)
        self.setGridSize(CELL)
        self.setSpacing(0)
        self.setLayoutMode(QListView.LayoutMode.Batched)
        self.setBatchSize(200)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)  # setMovement(Static) が切るので後で付け直す
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDropIndicatorShown(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(24)
        self.setMouseTracking(True)
        self.setStyleSheet(f"QListView {{ background: {T.BG1}; border: 1px solid {T.BORDER}; border-radius: 12px; padding: 6px; }}")
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(self._emit_window)
        self.verticalScrollBar().valueChanged.connect(lambda _v: self._timer.start())

    def schedule(self) -> None:
        self._timer.start()

    def visible_rows(self) -> tuple[int, int]:
        m = self.model()
        n = m.rowCount() if m is not None else 0
        if n == 0:
            return 0, -1
        vp = self.viewport().rect()
        first = self.indexAt(QPoint(vp.left() + CELL.width() // 2, vp.top() + 8))
        last = self.indexAt(QPoint(vp.right() - 8, vp.bottom() - 8))
        f = first.row() if first.isValid() else 0
        if last.isValid():
            lst = last.row()
        else:
            cols = max(1, vp.width() // CELL.width())
            rows = vp.height() // CELL.height() + 2
            lst = min(n - 1, f + cols * rows)
        return f, max(f, lst)

    def _emit_window(self) -> None:
        f, last = self.visible_rows()
        if last >= f:
            self.window_changed.emit(f, last)

    def resizeEvent(self, e: Any) -> None:  # noqa: N802
        super().resizeEvent(e)
        self._timer.start()

    def keyPressEvent(self, e: Any) -> None:  # noqa: N802
        try:
            if e.key() == Qt.Key.Key_Delete:
                self.delete_pressed.emit()
                return
            if e.matches(QKeySequence.StandardKey.Undo):
                self.undo_pressed.emit()
                return
            if e.matches(QKeySequence.StandardKey.Redo) or (
                    e.key() == Qt.Key.Key_Y and e.modifiers() & Qt.KeyboardModifier.ControlModifier):
                self.redo_pressed.emit()
                return
        except Exception:  # noqa: BLE001 - キー処理の失敗で画面を止めない
            return
        super().keyPressEvent(e)

    # ---- ドラッグでの並べ替え(自分の中だけ)
    def dragEnterEvent(self, e: Any) -> None:  # noqa: N802
        if e.source() is self:
            e.setDropAction(Qt.DropAction.MoveAction)
            e.accept()
        else:
            e.ignore()

    def dragMoveEvent(self, e: Any) -> None:  # noqa: N802
        if e.source() is self:
            e.setDropAction(Qt.DropAction.MoveAction)
            e.accept()
        else:
            e.ignore()

    def drop_row(self, pos: QPoint) -> int:
        m = self.model()
        n = m.rowCount() if m is not None else 0
        idx = self.indexAt(pos)
        if not idx.isValid():
            return n
        rect = self.visualRect(idx)
        return idx.row() + (1 if pos.x() > rect.center().x() else 0)

    def dropEvent(self, e: Any) -> None:  # noqa: N802
        try:
            if e.source() is not self:
                e.ignore()
                return
            rows = sorted({i.row() for i in self.selectedIndexes()})
            dest = self.drop_row(e.position().toPoint())
            e.setDropAction(Qt.DropAction.MoveAction)
            e.accept()
            if rows:
                self.moved.emit(rows, dest)
        except Exception:  # noqa: BLE001
            e.ignore()
