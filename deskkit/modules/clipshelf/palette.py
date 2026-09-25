# 検索パレット(枠なし・角丸・影・フェード+スライド)。検索欄+タブ(履歴 / 定型文)+独自描画の一覧+プレビュー+キー案内、
# 定型文の入力欄フォーム(C1)と変換して貼り付け(C2)のシート。キー操作は Qt のウィンドウ内イベントだけで読む(C-3 / FR-11)。
# 本文を画面に出すのはこのパレットだけ。ここからログへ本文を書かない(例外時もトレースバックだけ)。
from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypeVar

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QEasingCurve,
    QEvent,
    QModelIndex,
    QObject,
    QParallelAnimationGroup,
    QPersistentModelIndex,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QKeyEvent, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QScrollArea,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from deskkit.modules.clipshelf import transforms
from deskkit.modules.clipshelf.editor import edit_snippet_dialog
from deskkit.modules.clipshelf.snippets import FIELD_SELECT, Field
from deskkit.modules.clipshelf.store import KIND_SNIPPET, Item
from deskkit.ui import theme as T
from deskkit.ui import widgets as W
from deskkit.ui.theme import G

if TYPE_CHECKING:
    from deskkit.modules.clipshelf.module import ClipShelfModule

_log = logging.getLogger("deskkit.clipshelf.palette")
F = TypeVar("F", bound=Callable[..., Any])
ROLE_ITEM = int(Qt.ItemDataRole.UserRole) + 1
TAB_HISTORY, TAB_SNIPPETS = 0, 1
SHEET_LIST, SHEET_FORM, SHEET_TRANSFORM = 0, 1, 2
PANEL_W, PANEL_H, SHADOW_MARGIN = 900, 560, 30


def guard(fn: F) -> F:
    """Qt のシグナル・仮想メソッドから呼ばれる処理の例外を握りつぶしてログに出す(本文は含まれない)。"""

    @functools.wraps(fn)
    def wrapper(*a: Any, **k: Any) -> Any:
        try:
            return fn(*a, **k)
        except Exception:  # noqa: BLE001 - Qt へ例外を漏らさない(host D-3 と同じ方針)
            _log.exception("palette handler %s failed", getattr(fn, "__name__", "?"))
            return None

    return wrapper  # type: ignore[return-value]


# ------------------------------------------------------------------ 表示用の補助
def relative_time(dt: datetime, now: datetime) -> str:
    s = (now - dt).total_seconds()
    if s < 45:
        return "たった今"
    if s < 3600:
        return f"{max(1, int(s // 60))}分前"
    if s < 86400 and now.date() == dt.date():
        return f"{int(s // 3600)}時間前"
    days = (now.date() - dt.date()).days
    if days <= 1:
        return "昨日"
    if days < 7:
        return f"{days}日前"
    if dt.year == now.year:
        return f"{dt.month}/{dt.day}"
    return f"{dt.year}/{dt.month}/{dt.day}"


def _lines(text: str) -> list[str]:
    return [ln.strip().replace("\t", "  ") for ln in text.splitlines() if ln.strip()]


def title_and_sub(item: Item) -> tuple[str, str]:
    lines = _lines(item.text)
    if item.kind == KIND_SNIPPET:
        return (item.name or "無題の定型文"), (lines[0] if lines else "(本文なし)")
    if not lines:
        return "(空白のみ)", ""
    rest = len(lines) - 1
    sub = lines[1] if rest >= 1 else f"{len(item.text)} 文字"
    if rest >= 2:
        sub = f"{sub}  ·  ほか {rest - 1} 行"
    return lines[0], sub


def exe_label(exe: str | None) -> str:
    if not exe:
        return ""
    return exe[:-4] if exe.lower().endswith(".exe") else exe


# ------------------------------------------------------------------ 一覧のモデルと描画
class ItemModel(QAbstractListModel):
    def __init__(self) -> None:
        super().__init__()
        self._items: list[Item] = []

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008, N802
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)) -> Any:
        if not index.isValid() or index.row() >= len(self._items):
            return None
        if role == ROLE_ITEM:
            return self._items[index.row()]
        if role == int(Qt.ItemDataRole.AccessibleTextRole):
            return title_and_sub(self._items[index.row()])[0]
        return None

    def set_items(self, items: list[Item]) -> None:
        self.beginResetModel()
        self._items = items
        self.endResetModel()

    def item_at(self, row: int) -> Item | None:
        return self._items[row] if 0 <= row < len(self._items) else None

    def row_of(self, item_id: int) -> int:
        for i, it in enumerate(self._items):
            if it.id == item_id:
                return i
        return -1


class ItemDelegate(QStyledItemDelegate):
    ROW_H = 64

    def __init__(self, accent: str, now: Callable[[], datetime], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._accent = QColor(accent)
        self._now = now
        self._title_font = T.ui_font(14, QFont.Weight.DemiBold)
        self._sub_font = T.ui_font(12)
        self._meta_font = T.ui_font(11)
        self._badge_font = T.ui_font(11, QFont.Weight.DemiBold)
        self._icon_font = T.icon_font(15)

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), self.ROW_H)

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex) -> None:
        try:
            self._paint(p, option, index)
        except Exception:  # noqa: BLE001 - 描画から例外を漏らさない
            _log.exception("delegate paint failed")

    def _paint(self, p: QPainter, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex) -> None:
        item = index.data(ROLE_ITEM)
        if not isinstance(item, Item):
            return
        rect: QRect = option.rect
        state = option.state
        r = QRectF(rect).adjusted(6, 3, -6, -3)
        selected = bool(state & QStyle.StateFlag.State_Selected)
        hover = bool(state & QStyle.StateFlag.State_MouseOver)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(r, 11, 11)
        if selected:
            fill = QColor(self._accent)
            fill.setAlpha(38)
            p.fillPath(path, fill)
            edge = QColor(self._accent)
            edge.setAlpha(110)
            p.setPen(QPen(edge, 1))
            p.drawPath(path)
            bar = QRectF(r.left() + 1, r.top() + 12, 3, r.height() - 24)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._accent)
            p.drawRoundedRect(bar, 1.5, 1.5)
        elif hover:
            p.fillPath(path, QColor(T.SURFACE2))
        # 左のアイコン
        ic = QRectF(r.left() + 12, r.center().y() - 18, 36, 36)
        is_snip = item.kind == KIND_SNIPPET
        tint = QColor(self._accent) if (item.pinned or is_snip) else QColor(T.TEXT_DIM)
        bg = QColor(tint)
        bg.setAlpha(34 if (item.pinned or is_snip) else 18)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(ic, 10, 10)
        p.setFont(self._icon_font)
        p.setPen(tint)
        glyph = G.PIN if item.pinned else (G.SPARKLE if is_snip else G.TEXT)
        p.drawText(ic, int(Qt.AlignmentFlag.AlignCenter), glyph)
        # 右側のメタ情報(相対時刻・コピー元)
        right_w = 128.0
        meta = QRectF(r.right() - right_w - 10, r.top() + 9, right_w, 18)
        p.setFont(self._meta_font)
        p.setPen(QColor(T.TEXT_MUTE))
        when = relative_time(item.last_used_at, self._now())
        p.drawText(meta, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), when)
        badge_text = "定型文" if is_snip else exe_label(item.source_exe)
        if badge_text:
            p.setFont(self._badge_font)
            fm = QFontMetrics(self._badge_font)
            badge_text = fm.elidedText(badge_text, Qt.TextElideMode.ElideMiddle, int(right_w - 16))
            bw = fm.horizontalAdvance(badge_text) + 16
            b = QRectF(r.right() - 10 - bw, r.bottom() - 27, bw, 19)
            bc = QColor(self._accent) if is_snip else QColor(T.INFO)
            fillc = QColor(bc)
            fillc.setAlpha(26)
            edgec = QColor(bc)
            edgec.setAlpha(80)
            p.setBrush(fillc)
            p.setPen(QPen(edgec, 1))
            p.drawRoundedRect(b, 9.5, 9.5)
            p.setPen(bc)
            p.drawText(b, int(Qt.AlignmentFlag.AlignCenter), badge_text)
        # 本文(1行目と2行目)
        title, sub = title_and_sub(item)
        x = ic.right() + 14
        tw = int(meta.left() - x - 12)
        p.setFont(self._title_font)
        p.setPen(QColor(T.TEXT))
        tfm = QFontMetrics(self._title_font)
        p.drawText(QRectF(x, r.top() + 9, tw, 22), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   tfm.elidedText(title, Qt.TextElideMode.ElideRight, tw))
        p.setFont(self._sub_font)
        p.setPen(QColor(T.TEXT_DIM))
        sfm = QFontMetrics(self._sub_font)
        p.drawText(QRectF(x, r.top() + 32, tw, 20), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   sfm.elidedText(sub, Qt.TextElideMode.ElideRight, tw))
        p.restore()


# ------------------------------------------------------------------ タブ(動く下地つき)
class TabHeader(QWidget):
    changed = Signal(int)

    def __init__(self, tabs: list[tuple[str, str]], accent: str) -> None:
        super().__init__()
        self._tabs = tabs
        self._counts = [0] * len(tabs)
        self._index = 0
        self._pos = 0.0
        self._accent = QColor(accent)
        self._font = T.ui_font(13, QFont.Weight.DemiBold)
        self._count_font = T.ui_font(11, QFont.Weight.DemiBold)
        self._icon_font = T.icon_font(13)
        self.setFixedHeight(40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._anim = QPropertyAnimation(self, b"indicator", self)
        self._anim.setDuration(240)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _get(self) -> float:
        return self._pos

    def _set(self, v: float) -> None:
        self._pos = v
        self.update()

    indicator = Property(float, _get, _set)

    def index(self) -> int:
        return self._index

    def set_index(self, i: int, *, emit: bool = True) -> None:
        if not 0 <= i < len(self._tabs) or i == self._index:
            return
        self._index = i
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(float(i))
        self._anim.start()
        if emit:
            self.changed.emit(i)

    def set_counts(self, counts: list[int]) -> None:
        self._counts = counts
        self.update()

    def _tab_rects(self) -> list[QRectF]:
        rects: list[QRectF] = []
        x = 4.0
        fm = QFontMetrics(self._font)
        cfm = QFontMetrics(self._count_font)
        for i, (_g, text) in enumerate(self._tabs):
            w = 16 + 18 + fm.horizontalAdvance(text) + 10 + cfm.horizontalAdvance(str(self._counts[i])) + 16 + 14
            rects.append(QRectF(x, 4, w, self.height() - 8))
            x += w + 6
        return rects

    def mousePressEvent(self, e: Any) -> None:  # noqa: N802
        try:
            pos = e.position()
            for i, r in enumerate(self._tab_rects()):
                if r.contains(pos):
                    self.set_index(i)
                    return
        except Exception:  # noqa: BLE001
            _log.exception("tab click failed")

    def paintEvent(self, _e: Any) -> None:  # noqa: N802
        try:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            rects = self._tab_rects()
            if not rects:
                return
            i0 = max(0, min(len(rects) - 1, int(self._pos)))
            i1 = min(len(rects) - 1, i0 + 1)
            t = self._pos - i0
            a, b = rects[i0], rects[i1]
            ind = QRectF(a.x() + (b.x() - a.x()) * t, a.y(), a.width() + (b.width() - a.width()) * t, a.height())
            fill = QColor(self._accent)
            fill.setAlpha(36)
            edge = QColor(self._accent)
            edge.setAlpha(90)
            p.setPen(QPen(edge, 1))
            p.setBrush(fill)
            p.drawRoundedRect(ind, ind.height() / 2, ind.height() / 2)
            fm = QFontMetrics(self._font)
            for i, ((glyph, text), r) in enumerate(zip(self._tabs, rects, strict=True)):
                active = i == self._index
                col = QColor(T.TEXT) if active else QColor(T.TEXT_DIM)
                x = r.x() + 16
                p.setFont(self._icon_font)
                p.setPen(QColor(self._accent) if active else col)
                p.drawText(QRectF(x, r.y(), 16, r.height()), int(Qt.AlignmentFlag.AlignCenter), glyph)
                x += 18
                p.setFont(self._font)
                p.setPen(col)
                tw = fm.horizontalAdvance(text)
                p.drawText(QRectF(x, r.y(), tw + 2, r.height()), int(Qt.AlignmentFlag.AlignVCenter), text)
                x += tw + 10
                p.setFont(self._count_font)
                cnt = str(self._counts[i])
                cw = QFontMetrics(self._count_font).horizontalAdvance(cnt) + 12
                cr = QRectF(x, r.center().y() - 9, cw, 18)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(T.SURFACE3) if not active else QColor(self._accent))
                p.drawRoundedRect(cr, 9, 9)
                p.setPen(QColor(T.ON_ACCENT) if active else QColor(T.TEXT_DIM))
                p.drawText(cr, int(Qt.AlignmentFlag.AlignCenter), cnt)
            p.end()
        except Exception:  # noqa: BLE001
            _log.exception("tab paint failed")


def _kbd(key: str, text: str) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(5)
    k = QLabel(key)
    k.setFixedHeight(22)
    k.setAlignment(Qt.AlignmentFlag.AlignCenter)
    k.setStyleSheet(
        f"QLabel {{ color: {T.TEXT}; background: {T.SURFACE3}; border: 1px solid {T.BORDER_HI}; border-bottom-width: 2px;"
        f" border-radius: 5px; padding: 0px 6px; font-size: 11px; font-weight: 600; }}"
    )
    lay.addWidget(k, 0, Qt.AlignmentFlag.AlignVCenter)
    lay.addWidget(W.label(text, "Mute"), 0, Qt.AlignmentFlag.AlignVCenter)
    return w


# ------------------------------------------------------------------ パレット本体
class Palette(QWidget):
    def __init__(self, module: ClipShelfModule) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.m = module
        self.accent = module.accent
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setWindowTitle("ClipShelf")
        self._modal = False
        self._closing = False
        self._anim: QParallelAnimationGroup | None = None
        # 入力フォーム・変換の選択の途中状態(値はメモリだけ。閉じる・戻るときに消す)
        self._pending_item: Item | None = None
        self._pending_transform: str | None = None
        self._form_widgets: list[tuple[Field, QLineEdit | QComboBox]] = []
        self.resize(PANEL_W + SHADOW_MARGIN * 2, PANEL_H + SHADOW_MARGIN * 2)
        self._build()
        self.m.notifier.changed.connect(self._on_module_changed)

    # ------------------------------------------------------------ 構築
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW_MARGIN, SHADOW_MARGIN - 6, SHADOW_MARGIN, SHADOW_MARGIN + 6)
        self.panel = QFrame()
        self.panel.setObjectName("PalettePanel")
        self.panel.setStyleSheet(
            f"QFrame#PalettePanel {{ background: {T.BG1}; border: 1px solid {T.BORDER_HI}; border-radius: 18px; }}"
        )
        W.shadow(self.panel, 48, 70 if T.IS_LIGHT else 170, 14)  # ライトでは影を淡く
        outer.addWidget(self.panel)
        lay = QVBoxLayout(self.panel)
        lay.setContentsMargins(18, 16, 18, 12)
        lay.setSpacing(12)

        # 検索欄
        sbox = QFrame()
        sbox.setObjectName("SearchBox")
        sbox.setStyleSheet(
            f"QFrame#SearchBox {{ background: {T.SURFACE}; border: 1px solid {T.BORDER}; border-radius: 13px; }}"
        )
        srow = QHBoxLayout(sbox)
        srow.setContentsMargins(14, 6, 10, 6)
        srow.setSpacing(10)
        srow.addWidget(W.Glyph(G.SEARCH, 18, self.accent))
        self.search = QLineEdit()
        self.search.setPlaceholderText("履歴と定型文を検索(空白区切りで絞り込み)")
        self.search.setStyleSheet("QLineEdit { background: transparent; border: none; font-size: 18px; padding: 6px 2px; }")
        self.search.setClearButtonEnabled(True)
        srow.addWidget(self.search, 1)
        self.hits = W.label("", "Mute")
        srow.addWidget(self.hits)
        lay.addWidget(sbox)

        # タブ+右上の状態
        trow = QHBoxLayout()
        trow.setSpacing(10)
        self.tabs = TabHeader([(G.CLOCK, "履歴"), (G.SPARKLE, "定型文")], self.accent)
        trow.addWidget(self.tabs, 1)
        self.state_pill = W.StatusPill("", "ok")
        trow.addWidget(self.state_pill, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addLayout(trow)

        # 一覧+プレビュー(シート 0)/入力フォーム(シート 1)/変換の選択(シート 2)
        body_w = QWidget()
        body = QHBoxLayout(body_w)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(14)
        self.model = ItemModel()
        self.view = QListView()
        self.view.setModel(self.model)
        self.view.setItemDelegate(ItemDelegate(self.accent, self.m._now, self.view))
        self.view.setUniformItemSizes(True)
        self.view.setMouseTracking(True)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setStyleSheet("QListView { background: transparent; border: none; }")
        self.view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.empty = W.EmptyState(G.CLIPBOARD, "まだ履歴がありません", "")
        self.list_stack = QStackedWidget()
        self.list_stack.addWidget(self.view)
        self.list_stack.addWidget(self.empty)
        body.addWidget(self.list_stack, 1)
        body.addWidget(self._build_preview())
        self.sheets = QStackedWidget()
        self.sheets.addWidget(body_w)
        self.sheets.addWidget(self._build_form_sheet())
        self.sheets.addWidget(self._build_transform_sheet())
        lay.addWidget(self.sheets, 1)

        lay.addWidget(W.divider())
        # フッター(キー案内)
        frow = QHBoxLayout()
        frow.setSpacing(14)
        self.hints_box = QHBoxLayout()
        self.hints_box.setSpacing(14)
        frow.addLayout(self.hints_box)
        frow.addStretch(1)
        self.new_btn = W.button("新規", "ghost", G.ADD, on_click=guard(self._new_snippet), tooltip="定型文を作る(Ctrl+N)")
        self.clear_btn = W.button("全消去", "ghost", G.DELETE, on_click=guard(self._clear_all), tooltip="履歴を全消去")
        frow.addWidget(self.new_btn)
        frow.addWidget(self.clear_btn)
        lay.addLayout(frow)

        # シグナル(すべて guard を通す)
        self.search.textChanged.connect(guard(lambda _t: self.refresh()))
        self.tabs.changed.connect(guard(lambda _i: self._on_tab_changed()))
        self.view.selectionModel().currentChanged.connect(guard(lambda *_a: self._update_preview()))
        self.view.doubleClicked.connect(guard(lambda _i: self._choose_current()))
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(guard(self._show_context_menu))
        self.search.installEventFilter(self)
        self.view.installEventFilter(self)
        self.tf_list.installEventFilter(self)
        self._update_hints()

    def _build_preview(self) -> QWidget:
        pane = QFrame()
        pane.setObjectName("PreviewPane")
        pane.setFixedWidth(300)
        pane.setStyleSheet(f"QFrame#PreviewPane {{ background: {T.SURFACE}; border: 1px solid {T.BORDER}; border-radius: 13px; }}")
        lay = QVBoxLayout(pane)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(W.Glyph(G.EYE, 13, T.TEXT_DIM))
        head.addWidget(W.label("プレビュー", "Eyebrow"))
        head.addStretch(1)
        self.pv_kind = W.StatusPill("", "info")
        head.addWidget(self.pv_kind)
        lay.addLayout(head)
        self.pv_title = W.label("", "H3", wrap=True)
        self.pv_title.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(self.pv_title)
        self.pv_text = QPlainTextEdit()
        self.pv_text.setReadOnly(True)
        self.pv_text.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.pv_text.setStyleSheet(f"QPlainTextEdit {{ background: {T.SURFACE2}; border: 1px solid {T.BORDER}; border-radius: 9px;"
                                   f" font-size: 12px; padding: 6px; }}")
        lay.addWidget(self.pv_text, 1)
        self.pv_warn = QLabel()
        self.pv_warn.setWordWrap(True)
        self.pv_warn.setTextFormat(Qt.TextFormat.PlainText)
        self.pv_warn.setStyleSheet(f"color: {T.WARN}; font-size: 12px; background: {T.alpha(T.WARN, 0.08)};"
                                   f" border: 1px solid {T.alpha(T.WARN, 0.3)}; border-radius: 8px; padding: 6px 8px;")
        lay.addWidget(self.pv_warn)
        self.pv_meta = QLabel()
        self.pv_meta.setWordWrap(True)
        self.pv_meta.setTextFormat(Qt.TextFormat.PlainText)
        self.pv_meta.setStyleSheet(f"color: {T.TEXT_MUTE}; font-size: 11px;")
        lay.addWidget(self.pv_meta)
        return pane

    def _sheet_frame(self, name: str) -> tuple[QFrame, QVBoxLayout]:
        pane = QFrame()
        pane.setObjectName(name)
        pane.setStyleSheet(f"QFrame#{name} {{ background: {T.SURFACE}; border: 1px solid {T.BORDER}; border-radius: 13px; }}")
        lay = QVBoxLayout(pane)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(10)
        return pane, lay

    def _sheet_preview(self) -> QPlainTextEdit:
        pv = QPlainTextEdit()
        pv.setReadOnly(True)
        pv.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        pv.setFixedWidth(340)
        pv.setStyleSheet(f"QPlainTextEdit {{ background: {T.SURFACE2}; border: 1px solid {T.BORDER}; border-radius: 9px;"
                         f" font-size: 12px; padding: 6px; }}")
        return pv

    def _build_form_sheet(self) -> QWidget:
        """定型文の {input:…} / {select:…} に値を入れる小さなフォーム(v0.2 C1)。キーボードだけで完結する。"""
        pane, lay = self._sheet_frame("FormPane")
        head = QHBoxLayout()
        head.addWidget(W.Glyph(G.EDIT, 16, self.accent))
        self.form_title = W.label("", "H3")
        self.form_title.setTextFormat(Qt.TextFormat.PlainText)
        head.addWidget(self.form_title, 1)
        self.form_pill = W.StatusPill("", "info")
        head.addWidget(self.form_pill)
        lay.addLayout(head)
        lay.addWidget(W.label("貼り付ける前に値を入れてください。入れた値は保存しません。", "Mute", wrap=True))
        row = QHBoxLayout()
        row.setSpacing(14)
        host = QWidget()
        self.form_grid = QGridLayout(host)
        self.form_grid.setContentsMargins(0, 0, 0, 0)
        self.form_grid.setHorizontalSpacing(12)
        self.form_grid.setVerticalSpacing(10)
        scroll = QScrollArea()
        scroll.setWidget(host)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; }")
        row.addWidget(scroll, 1)
        right = QVBoxLayout()
        right.setSpacing(6)
        right.addWidget(W.label("貼り付ける内容", "Eyebrow"))
        self.form_preview = self._sheet_preview()
        right.addWidget(self.form_preview, 1)
        row.addLayout(right)
        lay.addLayout(row, 1)
        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(W.button("戻る", "ghost", G.UNDO, on_click=guard(self._back_to_list), tooltip="一覧に戻る(Esc)"))
        btns.addWidget(W.button("貼り付け", "primary", G.PASTE, on_click=guard(self._submit_form), tooltip="Enter"))
        lay.addLayout(btns)
        return pane

    def _build_transform_sheet(self) -> QWidget:
        """変換して貼り付け(v0.2 C2)の選択。利用者が選んだ1回だけ変換する(自動では変換しない。INV-8)。"""
        pane, lay = self._sheet_frame("TransformPane")
        head = QHBoxLayout()
        head.addWidget(W.Glyph(G.FILTER, 16, self.accent))
        self.tf_title = W.label("変換して貼り付け", "H3")
        head.addWidget(self.tf_title, 1)
        lay.addLayout(head)
        lay.addWidget(W.label("変換した結果をクリップボードに置きます。元の履歴・定型文は変わりません。", "Mute", wrap=True))
        row = QHBoxLayout()
        row.setSpacing(14)
        self.tf_list = QListWidget()
        self.tf_list.setStyleSheet(
            f"QListWidget {{ background: transparent; border: none; font-size: 14px; }}"
            f"QListWidget::item {{ padding: 8px 10px; border-radius: 8px; }}"
            f"QListWidget::item:selected {{ background: {T.alpha(self.accent, 0.18)}; color: {T.TEXT}; }}"
        )
        for i, (tid, label, _fn) in enumerate(transforms.TRANSFORMS):
            li = QListWidgetItem(f"{i + 1}    {label}")
            li.setData(Qt.ItemDataRole.UserRole, tid)
            self.tf_list.addItem(li)
        self.tf_list.currentRowChanged.connect(guard(lambda _r: self._update_transform_preview()))
        self.tf_list.itemActivated.connect(guard(lambda _it: self._apply_transform()))
        row.addWidget(self.tf_list, 1)
        right = QVBoxLayout()
        right.setSpacing(6)
        right.addWidget(W.label("変換後", "Eyebrow"))
        self.tf_preview = self._sheet_preview()
        right.addWidget(self.tf_preview, 1)
        row.addLayout(right)
        lay.addLayout(row, 1)
        return pane

    # ------------------------------------------------------------ 開閉
    def open_for(self, target_rect: tuple[int, int, int, int] | None, tab: int = TAB_HISTORY) -> None:
        screen = self._pick_screen(target_rect)
        ag = screen.availableGeometry() if screen is not None else QRect(0, 0, 1280, 720)
        w, h = self.width(), self.height()
        x = ag.x() + (ag.width() - w) // 2
        y = ag.y() + max(0, int((ag.height() - h) * 0.42))
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self._reset_sheets()
        self.tabs.set_index(tab, emit=False)
        self._on_tab_changed()
        self._closing = False
        end = QPoint(x, y)
        self.move(end + QPoint(0, 18))
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self.activateWindow()
        self.search.setFocus()
        self._animate(end, 1.0, 200)

    def _pick_screen(self, rect: tuple[int, int, int, int] | None) -> Any:
        if rect is not None:
            cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
            for s in QGuiApplication.screens():
                g = s.geometry()
                dpr = s.devicePixelRatio() or 1.0
                native = QRect(g.x(), g.y(), int(g.width() * dpr), int(g.height() * dpr))  # Qt6 の画面原点は物理座標
                if native.contains(cx, cy):
                    return s
        return QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()

    def _animate(self, end: QPoint, opacity: float, ms: int, on_done: Callable[[], None] | None = None) -> None:
        if self._anim is not None:
            self._anim.stop()
        grp = QParallelAnimationGroup(self)
        fade = QPropertyAnimation(self, b"windowOpacity", grp)
        fade.setDuration(ms)
        fade.setStartValue(self.windowOpacity())
        fade.setEndValue(opacity)
        fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        slide = QPropertyAnimation(self, b"pos", grp)
        slide.setDuration(ms + 40)
        slide.setStartValue(self.pos())
        slide.setEndValue(end)
        slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        grp.addAnimation(fade)
        grp.addAnimation(slide)
        if on_done is not None:
            grp.finished.connect(guard(on_done))
        self._anim = grp
        grp.start()

    def dismiss(self, animated: bool = True) -> None:
        if not self.isVisible() or self._closing:
            return
        if not animated:
            if self._anim is not None:
                self._anim.stop()
            self.hide()
            self._reset_sheets()
            return
        self._closing = True
        self._animate(self.pos() + QPoint(0, 10), 0.0, 120, self._finish_hide)

    def _finish_hide(self) -> None:
        self._closing = False
        self.hide()
        self.setWindowOpacity(1.0)
        self._reset_sheets()

    # ------------------------------------------------------------ Qt 仮想メソッド(すべて例外を握る)
    def eventFilter(self, obj: QObject, e: QEvent) -> bool:  # noqa: N802
        try:
            if e.type() == QEvent.Type.KeyPress and isinstance(e, QKeyEvent):
                return self._on_key(e)
        except Exception:  # noqa: BLE001
            _log.exception("palette key handling failed")
            return True
        return False

    def keyPressEvent(self, e: QKeyEvent) -> None:  # noqa: N802
        try:
            if not self._on_key(e):
                super().keyPressEvent(e)
        except Exception:  # noqa: BLE001
            _log.exception("palette keyPressEvent failed")

    def changeEvent(self, e: QEvent) -> None:  # noqa: N802
        try:
            super().changeEvent(e)
            if e.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
                QTimer.singleShot(0, guard(self._hide_if_inactive))
        except Exception:  # noqa: BLE001
            _log.exception("palette changeEvent failed")

    def _hide_if_inactive(self) -> None:
        if self._modal or not self.isVisible() or self.isActiveWindow():
            return
        aw = QApplication.activeWindow()
        if aw is not None and aw is not self and self.isAncestorOf(aw):
            return
        self.dismiss()

    # ------------------------------------------------------------ キー操作(FR-11)
    def _on_key(self, e: QKeyEvent) -> bool:
        sheet = self.sheets.currentIndex()
        if sheet == SHEET_FORM:
            return self._on_form_key(e)
        if sheet == SHEET_TRANSFORM:
            return self._on_transform_key(e)
        key = e.key()
        ctrl = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
        shift = bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if key == Qt.Key.Key_Escape:
            self.dismiss()
            return True
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            self.tabs.set_index(1 - self.tabs.index())
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if shift:
                self._open_transform()  # Shift+Enter: 変換して貼り付け(C2)
            else:
                self._choose_current()
            return True
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            step = {Qt.Key.Key_Up: -1, Qt.Key.Key_Down: 1, Qt.Key.Key_PageUp: -6, Qt.Key.Key_PageDown: 6}[Qt.Key(key)]
            self._move(step)
            return True
        if ctrl and key == Qt.Key.Key_P:
            self._toggle_pin()
            return True
        if key == Qt.Key.Key_Delete and not ctrl:
            self._delete_current()
            return True
        if ctrl and key == Qt.Key.Key_N and self.tabs.index() == TAB_SNIPPETS:
            self._new_snippet()
            return True
        if ctrl and key == Qt.Key.Key_E and self.tabs.index() == TAB_SNIPPETS:
            self._edit_current()
            return True
        return False

    def _move(self, step: int) -> None:
        n = self.model.rowCount()
        if n == 0:
            return
        row = self.view.currentIndex().row()
        row = max(0, min(n - 1, (row if row >= 0 else -1) + step))
        self._select_row(row)

    def _select_row(self, row: int) -> None:
        idx = self.model.index(row, 0)
        if idx.isValid():
            self.view.setCurrentIndex(idx)
            self.view.scrollTo(idx, QAbstractItemView.ScrollHint.EnsureVisible)
        self._update_preview()

    def current_item(self) -> Item | None:
        return self.model.item_at(self.view.currentIndex().row())

    # ------------------------------------------------------------ 動作
    def _choose_current(self) -> None:
        item = self.current_item()
        if item is not None:
            self._paste(item, None)

    def _paste(self, item: Item, transform: str | None) -> None:
        """聞く欄がある定型文ならフォームを出し、無ければそのまま貼り付ける。"""
        if self.m.snippet_fields(item):
            self._open_form(item, transform)
        else:
            self.m.choose(item, transform=transform)

    # ------------------------------------------------------------ シート(入力フォーム・変換の選択)
    def _show_sheet(self, index: int) -> None:
        self.sheets.setCurrentIndex(index)
        self._update_hints()

    def _reset_sheets(self) -> None:
        """フォームの値と途中状態を捨てて一覧に戻す(入れた値をメモリの部品に残さない)。"""
        self._pending_item = None
        self._pending_transform = None
        self._clear_form()
        self.form_preview.setPlainText("")
        self.tf_preview.setPlainText("")
        if self.sheets.currentIndex() != SHEET_LIST:
            self._show_sheet(SHEET_LIST)

    def _back_to_list(self) -> None:
        self._reset_sheets()
        self.search.setFocus()

    def _clear_form(self) -> None:
        for _f, w in self._form_widgets:
            if isinstance(w, QLineEdit):
                w.clear()
        self._form_widgets = []
        while self.form_grid.count():
            it = self.form_grid.takeAt(0)
            wdg = it.widget() if it is not None else None
            if wdg is not None:
                # 親から外さない(外すと Python 側の参照切れで即座に破棄され、キー処理中の欄を消して落ちる)。
                # 隠して、イベントループに戻ってから消す
                wdg.hide()
                wdg.deleteLater()

    def _open_form(self, item: Item, transform: str | None) -> None:
        self._clear_form()
        self._pending_item = item
        self._pending_transform = transform
        self.form_title.setText(item.name or "無題の定型文")
        self.form_pill.set_state(self.accent, f"変換: {transforms.LABELS[transform]}" if transform else "定型文")
        for row, f in enumerate(self.m.snippet_fields(item)):
            lab = W.label(f.label, "Dim")
            lab.setTextFormat(Qt.TextFormat.PlainText)
            w: QLineEdit | QComboBox
            if f.kind == FIELD_SELECT:
                w = QComboBox()
                w.addItems(list(f.options))
                w.currentIndexChanged.connect(guard(lambda _i: self._update_form_preview()))
            else:
                w = QLineEdit(f.default)
                w.setPlaceholderText(f.label)
                w.textChanged.connect(guard(lambda _t: self._update_form_preview()))
            w.setMinimumHeight(34)
            w.installEventFilter(self)
            self.form_grid.addWidget(lab, row, 0, Qt.AlignmentFlag.AlignVCenter)
            self.form_grid.addWidget(w, row, 1)
            self._form_widgets.append((f, w))
        self.form_grid.setRowStretch(len(self._form_widgets), 1)
        self.form_grid.setColumnStretch(1, 1)
        self._show_sheet(SHEET_FORM)
        self._update_form_preview()
        if self._form_widgets:
            first = self._form_widgets[0][1]
            first.setFocus()
            if isinstance(first, QLineEdit):
                first.selectAll()

    def form_values(self) -> dict[str, str]:
        return {f.key: (w.currentText() if isinstance(w, QComboBox) else w.text()) for f, w in self._form_widgets}

    def _update_form_preview(self) -> None:
        item = self._pending_item
        if item is None:
            return
        text = self.m.snippet_preview(item.text, self.form_values()).text
        if self._pending_transform:
            text = transforms.apply(self._pending_transform, text)
        self.form_preview.setPlainText(text)

    def _submit_form(self) -> None:
        item, transform = self._pending_item, self._pending_transform
        if item is None:
            return
        values = self.form_values()
        self._reset_sheets()  # 値はここで部品から消す(choose に渡す分だけが残る)
        self.m.choose(item, values=values, transform=transform)

    def _focus_next_field(self, step: int) -> None:
        ws = [w for _f, w in self._form_widgets]
        if not ws:
            return
        fw = self.focusWidget()  # 窓が非アクティブでも「この窓で次にフォーカスを持つ部品」が分かる
        cur = next((i for i, w in enumerate(ws) if w is fw), -1)
        nxt = ws[(cur + step) % len(ws)] if cur >= 0 else ws[0]
        nxt.setFocus()
        if isinstance(nxt, QLineEdit):
            nxt.selectAll()

    def _on_form_key(self, e: QKeyEvent) -> bool:
        key = e.key()
        if key == Qt.Key.Key_Escape:
            self._back_to_list()
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._submit_form()
            return True
        if key == Qt.Key.Key_Tab and not (e.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self._focus_next_field(1)
            return True
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            self._focus_next_field(-1)
            return True
        return False  # 文字入力・選択欄の ↑↓ はそのまま部品に渡す

    def _open_transform(self, item: Item | None = None) -> None:
        item = item or self.current_item()
        if item is None:
            return
        self._pending_item = item
        self._pending_transform = None
        self.tf_title.setText(f"変換して貼り付け — {title_and_sub(item)[0]}")
        self._show_sheet(SHEET_TRANSFORM)
        self.tf_list.setCurrentRow(0)
        self.tf_list.setFocus()
        self._update_transform_preview()

    def _current_transform(self) -> str | None:
        li = self.tf_list.currentItem()
        return str(li.data(Qt.ItemDataRole.UserRole)) if li is not None else None

    def _update_transform_preview(self) -> None:
        item, tid = self._pending_item, self._current_transform()
        if item is None or tid is None:
            return
        base = self.m.snippet_preview(item.text).text if item.kind == KIND_SNIPPET else item.text
        out = transforms.apply(tid, base)
        self.tf_preview.setPlainText(out if len(out) <= 20000 else out[:20000] + "\n…(以下省略)")

    def _apply_transform(self, tid: str | None = None) -> None:
        item = self._pending_item
        tid = tid or self._current_transform()
        if item is None or tid is None:
            return
        self._reset_sheets()
        self._paste(item, tid)

    def _on_transform_key(self, e: QKeyEvent) -> bool:
        key = e.key()
        if key == Qt.Key.Key_Escape:
            self._back_to_list()
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._apply_transform()
            return True
        k1 = int(Qt.Key.Key_1)
        if k1 <= int(key) < k1 + len(transforms.IDS):
            self._apply_transform(transforms.IDS[int(key) - k1])
            return True
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            n = self.tf_list.count()
            self.tf_list.setCurrentRow((self.tf_list.currentRow() + (1 if key == Qt.Key.Key_Down else -1)) % n)
            return True
        return True  # ほかのキーは検索欄などに流さない

    # ------------------------------------------------------------ 右クリックメニュー
    def build_context_menu(self, item: Item) -> QMenu:
        menu = QMenu(self)
        menu.addAction("貼り付け\tEnter", guard(lambda: self._paste(item, None)))
        sub = menu.addMenu("変換して貼り付け\tShift+Enter")
        for i, (tid, label, _fn) in enumerate(transforms.TRANSFORMS):
            sub.addAction(f"&{i + 1}  {label}", guard(lambda t=tid: self._paste(item, t)))
        menu.addSeparator()
        if item.kind == KIND_SNIPPET:
            menu.addAction("編集\tCtrl+E", guard(self._edit_current))
        else:
            menu.addAction("ピン留めを外す\tCtrl+P" if item.pinned else "ピン留め\tCtrl+P", guard(self._toggle_pin))
        menu.addAction("削除…\tDel", guard(self._delete_current))
        return menu

    def _show_context_menu(self, pos: QPoint) -> None:
        idx = self.view.indexAt(pos)
        if not idx.isValid():
            return
        self._select_row(idx.row())
        item = self.current_item()
        if item is None:
            return
        menu = self.build_context_menu(item)
        self._run_modal(lambda: menu.exec(self.view.viewport().mapToGlobal(pos)))
        menu.deleteLater()

    def _toggle_pin(self) -> None:
        item = self.current_item()
        if item is None or item.kind == KIND_SNIPPET:
            return
        self.m.toggle_pin(item)
        self.refresh(keep_id=item.id)

    def _delete_current(self) -> None:
        item = self.current_item()
        if item is None:
            return
        what = "この定型文" if item.kind == KIND_SNIPPET else ("このピン留めした履歴" if item.pinned else "この履歴")
        ok, _ = self._confirm("削除の確認", f"{what}を削除します。元に戻せません。", ok_text="削除", danger=True)
        if not ok:
            return
        row = self.view.currentIndex().row()
        self.m.delete_item(item)
        self.refresh()
        if self.model.rowCount():
            self._select_row(min(row, self.model.rowCount() - 1))

    def _new_snippet(self) -> None:
        if self.tabs.index() != TAB_SNIPPETS:
            self.tabs.set_index(TAB_SNIPPETS)
        res = self._run_modal(lambda: edit_snippet_dialog(self, "定型文を作る", "", "", self.m.snippet_preview, self.accent))
        if res is None:
            return
        item = self.m.save_snippet(None, res[0], res[1])
        self.refresh(keep_id=item.id if item else None)

    def _edit_current(self) -> None:
        item = self.current_item()
        if item is None or item.kind != KIND_SNIPPET:
            return
        res = self._run_modal(lambda: edit_snippet_dialog(self, "定型文を編集", item.name or "", item.text,
                                                          self.m.snippet_preview, self.accent))
        if res is None:
            return
        self.m.save_snippet(item.id, res[0], res[1])
        self.refresh(keep_id=item.id)

    def _clear_all(self) -> None:
        self._run_modal(lambda: self.m.clear_all_interactive(self))
        self.refresh()
        self.activateWindow()
        self.search.setFocus()

    def _confirm(self, title: str, text: str, **kw: Any) -> tuple[bool, list[bool]]:
        res = self._run_modal(lambda: W.confirm(self, title, text, **kw))
        return res if res is not None else (False, [])

    def _run_modal(self, fn: Callable[[], Any]) -> Any:
        self._modal = True
        try:
            return fn()
        finally:
            self._modal = False
            if self.isVisible():
                self.activateWindow()
                if self.sheets.currentIndex() == SHEET_LIST:  # フォームを開いたときは最初の欄のフォーカスを奪わない
                    self.search.setFocus()

    # ------------------------------------------------------------ 表示の更新
    def _on_module_changed(self) -> None:
        try:
            if self.isVisible() and not self._modal:
                cur = self.current_item()
                self.refresh(keep_id=cur.id if cur else None)
        except Exception:  # noqa: BLE001
            _log.exception("palette refresh failed")

    def _on_tab_changed(self) -> None:
        if self.sheets.currentIndex() != SHEET_LIST:
            self._reset_sheets()  # タブをクリックしたらフォーム・変換の途中は捨てて一覧に戻る
        self._update_hints()
        self.refresh()

    def refresh(self, keep_id: int | None = None) -> None:
        q = self.search.text()
        hist = self.m.search_history(q)
        snips = self.m.search_snippets(q)
        self.tabs.set_counts([len(hist), len(snips)])
        items = hist if self.tabs.index() == TAB_HISTORY else snips
        self.model.set_items(items)
        self.hits.setText(f"{len(items)} 件" if q.strip() else "")
        self._update_state_pill()
        if not items:
            self._show_empty(bool(q.strip()))
            self._update_preview()
            return
        self.list_stack.setCurrentWidget(self.view)
        row = self.model.row_of(keep_id) if keep_id is not None else -1
        self._select_row(row if row >= 0 else 0)

    def _show_empty(self, searching: bool) -> None:
        self.list_stack.removeWidget(self.empty)
        self.empty.deleteLater()
        if searching:
            self.empty = W.EmptyState(G.SEARCH, "一致する項目はありません", "語を減らすか、別の語で探してください。")
        elif self.tabs.index() == TAB_SNIPPETS:
            self.empty = W.EmptyState(G.SPARKLE, "定型文がありません", "Ctrl+N で作れます。{date} {time} {clipboard} と、貼り付け時に聞く {input:ラベル} {select:A|B|C} が使えます。")
        elif self.m.store is None:
            self.empty = W.EmptyState(G.ERROR, "履歴 DB を開けません", self.m.store_error or "")
        elif self.m.config.mode == "observe":
            self.empty = W.EmptyState(G.EYE, "観察モードです", "今は判定理由をログに出すだけで、履歴は記録しません。"
                                      "Control Center の ClipShelf で「記録」に切り替えると記録を始めます。")
        else:
            self.empty = W.EmptyState(G.CLIPBOARD, "まだ履歴がありません", "テキストをコピーするとここに並びます。")
        self.list_stack.addWidget(self.empty)
        self.list_stack.setCurrentWidget(self.empty)

    def _update_state_pill(self) -> None:
        if self.m.store is None:
            self.state_pill.set_state("error", "DB エラー")
        elif self.m.pause_reasons():
            self.state_pill.set_state("off", self.m.status_text())
        elif self.m.config.mode == "observe":
            self.state_pill.set_state("warn", "観察モード(記録しない)")
        else:
            self.state_pill.set_state("ok", "記録中")

    def _update_hints(self) -> None:
        while self.hints_box.count():
            it = self.hints_box.takeAt(0)
            wdg = it.widget() if it is not None else None
            if wdg is not None:
                wdg.hide()
                wdg.setParent(None)  # 次のイベントループを待たずに見えなくする
                wdg.deleteLater()
        sheet = self.sheets.currentIndex()
        if sheet == SHEET_FORM:
            hints = [("Tab", "次の欄"), ("Enter", "貼り付け"), ("Esc", "戻る")]
        elif sheet == SHEET_TRANSFORM:
            hints = [("↑↓", "選ぶ"), ("1〜6", "すぐ変換"), ("Enter", "変換して貼り付け"), ("Esc", "戻る")]
        else:
            hints = [("↑↓", "移動"), ("Enter", "貼り付け"), ("Shift+Enter", "変換"), ("Tab", "切替"), ("Del", "削除"),
                     ("Esc", "閉じる")]
            if self.tabs.index() == TAB_HISTORY:
                hints.insert(3, ("Ctrl+P", "ピン"))
            else:
                hints.insert(3, ("Ctrl+N", "新規"))
                hints.insert(4, ("Ctrl+E", "編集"))
        for k, t in hints:
            self.hints_box.addWidget(_kbd(k, t))
        is_snip = self.tabs.index() == TAB_SNIPPETS
        self.new_btn.setVisible(is_snip and sheet == SHEET_LIST)
        self.clear_btn.setVisible(not is_snip and sheet == SHEET_LIST)

    def _update_preview(self) -> None:
        item = self.current_item()
        if item is None:
            self.pv_kind.set_state("off", "—")
            self.pv_title.setText("")
            self.pv_text.setPlainText("")
            self.pv_warn.setVisible(False)
            self.pv_meta.setText("")
            return
        now = self.m._now()
        if item.kind == KIND_SNIPPET:
            self.pv_kind.set_state(self.accent, "定型文")
            self.pv_title.setText(item.name or "無題の定型文")
            self.pv_title.setVisible(True)
            exp = self.m.snippet_preview(item.text)
            self.pv_text.setPlainText(exp.text)
            self.pv_warn.setText("\n".join("⚠ " + w for w in exp.warnings))
            self.pv_warn.setVisible(bool(exp.warnings))
            how = ("Enter で値を入れる欄を開き、展開した結果をクリップボードに置きます" if exp.fields
                   else "Enter で展開した結果をクリップボードに置きます")
            self.pv_meta.setText(f"最後に使用: {relative_time(item.last_used_at, now)}\n{how}")
            return
        self.pv_kind.set_state("accent" if item.pinned else "info", "ピン留め" if item.pinned else "履歴")
        self.pv_title.setText("")
        self.pv_title.setVisible(False)
        self.pv_text.setPlainText(item.text if len(item.text) <= 20000 else item.text[:20000] + "\n…(以下省略)")
        self.pv_warn.setVisible(False)
        n_lines = item.text.count("\n") + 1
        expiry = ""
        if item.expires_at is not None and not item.pinned:
            expiry = f"\n⏱ {item.expires_at.strftime('%H:%M')} に自動で消えます(短命記録のアプリ。ピン留めで残せます)"
        self.pv_meta.setText(
            f"コピー元: {item.source_exe or '不明'}\n"
            f"作成: {item.created_at.strftime('%Y-%m-%d %H:%M')}  ·  最後に使用: {relative_time(item.last_used_at, now)}\n"
            f"{len(item.text):,} 文字 · {n_lines} 行{expiry}"
        )
