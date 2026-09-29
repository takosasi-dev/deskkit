# Control Center の MojiFix 画面。切り替え3つ(文字化けしたファイル / zip の名前 / 名前の濁点)と、共通の進捗・中止。
# 候補は中身を見比べて利用者が選ぶ(M-1)。ファイル名・中身は画面にだけ出す(V-8)。重い処理はモジュールのワーカーが行う(FR-3)。
# シグナルから呼ぶ処理は例外を握り、ログには型名だけを書く(INV-5)。
from __future__ import annotations

import functools
import html
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, QRectF, Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QFont,
    QMouseEvent,
    QPainter,
    QPen,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from deskkit.modules.mojifix import config as cfgmod
from deskkit.modules.mojifix import renamer as R
from deskkit.modules.mojifix import sniff as S
from deskkit.modules.mojifix import textfix as TF
from deskkit.modules.mojifix import zipnames as Z
from deskkit.modules.mojifix.compose import separated_positions
from deskkit.modules.mojifix.module import AREA_RENAME, AREA_TEXT, AREA_ZIP, MSG_QUEUE_FULL, Notice
from deskkit.ui import theme as T
from deskkit.ui import widgets as W
from deskkit.ui.theme import G

if TYPE_CHECKING:
    from deskkit.modules.mojifix.module import MojiFixModule

_log = logging.getLogger("deskkit.mojifix")
F = TypeVar("F", bound=Callable[..., Any])
MONO = "Consolas, 'Cascadia Mono', 'BIZ UDGothic', 'MS Gothic', monospace"
G_TEXT = G.TEXT
G_ZIP = G.ARCHIVE
G_NAME = G.EDIT
STAGES = {
    "start": "始めています…", "sniff": "読み込んでいます", "check": "確かめています", "write": "書き出しています",
    "verify": "書き出しを確かめています", "zipload": "zip を読んでいます", "plan": "名前の一覧を作っています",
    "extract": "展開しています", "scan": "調べています", "rename": "名前を直しています", "undo": "元に戻しています",
}
KIND_STAGE = {"sniff": "sniff", "zipload": "zipload", "plan": "plan", "scan": "scan", "undo": "undo", "text": "check",
              "zip": "extract", "rename": "rename"}


def safe(label: str) -> Callable[[F], F]:
    """Qt のシグナルから呼ぶメソッドの例外を握る。ログには例外の型名だけを書く(INV-5)。"""

    def deco(fn: F) -> F:
        @functools.wraps(fn)
        def w(*a: Any, **k: Any) -> Any:
            try:
                return fn(*a, **k)
            except RuntimeError:
                return None  # 画面が先に閉じられた
            except Exception as e:  # noqa: BLE001
                _log.error("handler %s failed: %s", label, type(e).__name__)
                return None

        return w  # type: ignore[return-value]

    return deco


def _local_paths(md: Any) -> list[str]:
    if md is None or not md.hasUrls():
        return []
    return [u.toLocalFile() for u in md.urls() if u.isLocalFile() and u.toLocalFile()]


def _mono_font(px: int = 12) -> QFont:
    f = QFont()
    f.setFamilies(["Consolas", "Cascadia Mono", "BIZ UDGothic", "MS Gothic"])
    f.setPixelSize(px)
    return f


def _open_path(p: Path) -> bool:
    return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(p))))


class _Note(QFrame):
    """色付きの注意書き。"""

    COLORS = {"ok": (T.SUCCESS, G.CHECK), "info": (T.INFO, G.INFO), "warn": (T.WARN, G.WARNING), "error": (T.DANGER, G.ERROR)}

    def __init__(self) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(10)
        self.glyph = W.Glyph(G.INFO, 14, T.INFO)
        lay.addWidget(self.glyph, 0, Qt.AlignmentFlag.AlignTop)
        self.label = W.label("", None, wrap=True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.label, 1)
        self.set_notice(None)

    def set_notice(self, n: Notice | None) -> None:
        if n is None:
            self.setVisible(False)
            return
        color, glyph = self.COLORS.get(n.kind, self.COLORS["info"])
        self.setStyleSheet(f"QFrame {{ background: {T.alpha(color, 0.08)}; border: 1px solid {T.alpha(color, 0.30)}; border-radius: 10px; }}")
        self.glyph.setText(glyph)
        self.glyph.setStyleSheet(f"color: {color}; background: transparent; border: none;")
        self.label.setStyleSheet(f"color: {T.TEXT}; background: transparent; border: none; font-size: 12px;")
        self.label.setText(n.text)
        self.setVisible(True)


class _DropZone(QFrame):
    """点線の枠。ページ全体がドロップを受けるので、ここは見た目とボタンだけ。"""

    def __init__(self, accent: str, glyph: str, title: str, sub: str, button: QWidget) -> None:
        super().__init__()
        self._accent = accent
        self._hot = False
        self.setMinimumHeight(150)
        self.setMaximumHeight(200)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(6)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        g = W.Glyph(glyph, 22, accent)
        g.setFixedSize(44, 44)
        g.setStyleSheet(f"color: {accent}; background: {T.alpha(accent, 0.12)}; border-radius: 22px;")
        lay.addWidget(g, 0, Qt.AlignmentFlag.AlignCenter)
        t = W.label(title, "H3")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(t)
        s = W.label(sub, "Mute", wrap=True)
        s.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(s)
        lay.addWidget(button, 0, Qt.AlignmentFlag.AlignCenter)

    def set_hot(self, hot: bool) -> None:
        self._hot = hot
        self.update()

    def paintEvent(self, _e: object) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        c = QColor(self._accent)
        bg = QColor(self._accent)
        bg.setAlpha(34 if self._hot else 12)
        p.setBrush(bg)
        c.setAlpha(230 if self._hot else 140)
        p.setPen(QPen(c, 2 if self._hot else 1.5, Qt.PenStyle.DashLine))
        p.drawRoundedRect(r, 14, 14)
        p.end()


class _CandCard(QFrame):
    """読み方の候補1つ。クリックで選ぶ。中身の先頭を等幅で見せる。"""

    clicked = Signal(str)

    def __init__(self, key: str, title: str, state: str, ok: bool, lines: Sequence[str], accent: str,
                 badge: str | None = None, visible_lines: int = 8) -> None:
        super().__init__()
        self.key = key
        self._accent = accent
        self._selected = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 12)
        lay.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.radio = W.Glyph(G.CHECK, 13, accent)
        self.radio.setFixedSize(22, 22)
        head.addWidget(self.radio)
        t = W.label(title, "H3")
        t.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        t.setMinimumWidth(60)
        head.addWidget(t, 1)
        if badge:
            head.addWidget(W.StatusPill(badge, "accent"))
        head.addWidget(W.StatusPill(state, "ok" if ok else "warn"))
        lay.addLayout(head)
        self.body = QPlainTextEdit()
        self.body.setReadOnly(True)
        self.body.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.body.setFont(_mono_font(12))
        self.body.setPlainText("\n".join(lines) if lines else "(中身がありません)")
        self.body.setFrameShape(QFrame.Shape.NoFrame)
        self.body.setFixedHeight(min(visible_lines, max(2, len(lines))) * 18 + 16)
        self.body.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.body.viewport().installEventFilter(self)
        self.body.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        lay.addWidget(self.body)
        self.set_selected(False)

    def set_selected(self, on: bool) -> None:
        self._selected = on
        a = self._accent
        if on:
            self.setStyleSheet(f"QFrame {{ background: {T.alpha(a, 0.10)}; border: 1px solid {T.alpha(a, 0.75)};"
                               f" border-radius: 12px; }} QPlainTextEdit {{ background: {T.SURFACE2}; border: none; border-radius: 8px; }}"
                               f" QLabel {{ background: transparent; border: none; }}")
            self.radio.setText(G.CHECK)
            self.radio.setStyleSheet(f"color: {T.ON_ACCENT}; background: {a}; border-radius: 11px;")
        else:
            self.setStyleSheet(f"QFrame {{ background: {T.SURFACE2}; border: 1px solid {T.BORDER}; border-radius: 12px; }}"
                               f" QPlainTextEdit {{ background: {T.SURFACE}; border: none; border-radius: 8px; }}"
                               f" QLabel {{ background: transparent; border: none; }}")
            self.radio.setText("")
            self.radio.setStyleSheet(f"color: {a}; background: transparent; border: 1px solid {T.BORDER_HI}; border-radius: 11px;")

    def eventFilter(self, obj: Any, e: Any) -> bool:  # noqa: N802
        if e.type() == e.Type.MouseButtonRelease and e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.key)
        return False

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.key)
        super().mouseReleaseEvent(e)


# ------------------------------------------------------------------ 表のモデル(5 万件・20 万件でも軽く)
class _PlanModel(QAbstractTableModel):
    HEAD = ("zip の中の名前", "展開する名前", "状態")

    def __init__(self) -> None:
        super().__init__()
        self.items: list[Z.PlanItem] = []

    def set_items(self, items: list[Z.PlanItem]) -> None:
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008, N802
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008, N802
        return 3

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEAD[section]
        return None

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        it = self.items[index.row()]
        c = index.column()
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            if c == 0:
                return it.original
            if c == 1:
                return it.rel or "—"
            return it.status_text()
        if role == Qt.ItemDataRole.ForegroundRole and c == 2:
            return QColor({"extract": T.SUCCESS, "rename": T.WARN}.get(it.status, T.TEXT_MUTE))
        return None


class _RenameModel(QAbstractTableModel):
    HEAD = ("", "今の名前", "直した名前", "場所", "状態")

    def __init__(self, page: MojiFixPage) -> None:
        super().__init__()
        self.page = page
        self.items: list[R.Item] = []

    def set_items(self, items: list[R.Item]) -> None:
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def refresh(self) -> None:
        if self.items:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.items) - 1, 4))

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008, N802
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008, N802
        return 5

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEAD[section]
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        f = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.isValid() and index.column() == 0:
            x = self.items[index.row()]
            if x.fixable and not x.result:
                f |= Qt.ItemFlag.ItemIsUserCheckable
        return f

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        x = self.items[index.row()]
        c = index.column()
        if role == Qt.ItemDataRole.CheckStateRole and c == 0:
            if not x.fixable or x.result:
                return None
            on = index.row() in self.page.m.rn_selected
            return Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            if c == 1:
                return x.old
            if c == 2:
                return x.new
            if c == 3:
                return x.rel_parent or "(選んだフォルダ)"
            if c == 4:
                return x.result or x.status
        if role == Qt.ItemDataRole.UserRole and c == 1:
            return _highlight_html(x.old)
        if role == Qt.ItemDataRole.ForegroundRole and c == 4:
            if x.result:
                return QColor(T.SUCCESS if x.result in ("直しました", "元に戻しました") else T.DANGER)
            return QColor(T.SUCCESS if x.fixable else T.WARN)
        return None

    def setData(self, index: QModelIndex | QPersistentModelIndex, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:  # noqa: N802
        if role == Qt.ItemDataRole.CheckStateRole and index.isValid() and index.column() == 0:
            on = value in (Qt.CheckState.Checked, Qt.CheckState.Checked.value, 2)
            self.page.m.set_selected(index.row(), bool(on))
            self.dataChanged.emit(index, index)
            return True
        return False


def _highlight_html(name: str) -> str:
    """分かれた文字(結合する濁点など)を強調した HTML。濁点は前の字の上に重なるので、目印の色を付けて囲む。"""
    pos = set(separated_positions(name))
    out: list[str] = []
    for i, ch in enumerate(name):
        if i in pos:
            out.append(f'<span style="background:{T.alpha(T.WARN, 0.35)}; color:{T.TEXT};">&#9676;{html.escape(ch)}</span>')
        else:
            out.append(html.escape(ch))
    return "".join(out)


class _HtmlDelegate(QStyledItemDelegate):
    """UserRole の HTML で描く列(今の名前の強調)。"""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex) -> None:
        text = index.data(Qt.ItemDataRole.UserRole)
        if not text:
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget is not None else None
        if style is not None:
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        doc = QTextDocument()
        doc.setDefaultFont(opt.font)
        doc.setDocumentMargin(0)
        doc.setHtml(f'<span style="color:{T.TEXT};">{text}</span>')
        painter.save()
        r = option.rect.adjusted(6, 0, -4, 0)
        painter.translate(r.left(), r.top() + max(0.0, (r.height() - doc.size().height()) / 2))
        painter.setClipRect(QRectF(0, 0, r.width(), r.height()))
        doc.drawContents(painter, QRectF(0, 0, r.width(), r.height()))
        painter.restore()


def _table(model: QAbstractTableModel, min_height: int = 260) -> QTableView:
    t = QTableView()
    t.setModel(model)
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setAlternatingRowColors(True)
    t.setShowGrid(False)
    t.setWordWrap(False)
    t.setMinimumHeight(min_height)
    t.setMaximumHeight(min_height + 160)
    t.verticalHeader().setDefaultSectionSize(28)
    h = t.horizontalHeader()
    h.setHighlightSections(False)
    h.setStretchLastSection(True)
    return t


# ================================================================== 画面
class MojiFixPage(W.ScrollPage):
    def __init__(self, module: MojiFixModule) -> None:
        super().__init__()
        self.m = module
        self.accent = module.accent
        self._area = AREA_TEXT
        self._shown_others = False
        self._cards: list[_CandCard] = []
        self._zip_cards: list[_CandCard] = []
        self._text_sig: tuple[Any, ...] | None = None
        self._zip_sig: tuple[Any, ...] | None = None
        self._asking = False
        # 試験で差し替える口(本物はダイアログを出す)
        self.ask_choice: Callable[[str, TF.CheckResult], str] = self._ask_dialog
        self.confirm: Callable[[str, str, str], bool] = self._confirm_dialog
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self._build_hero()
        self._build_tiles()
        self._build_switch()
        self._build_progress()
        self.stack = W.FadeStack()
        self.p_text = self._build_text()
        self.p_zip = self._build_zip()
        self.p_rename = self._build_rename()
        for p in (self.p_text, self.p_zip, self.p_rename):
            self.stack.addWidget(p)
        self.add(self.stack)
        self._fit_stack(self.p_text)
        self.finish()
        s = module.signals
        s.changed.connect(self._on_changed)
        s.progress.connect(self._on_progress)
        s.area.connect(self._on_area)
        self._refresh_all()

    # ================================================================ 共通
    def _parent(self) -> QWidget | None:
        return self.window()

    def _build_hero(self) -> None:
        from deskkit.catalog import info

        hero = W.Hero("MojiFix", "化けた CSV、Mac から届いた zip の名前、分かれた濁点を、中身を見ながら直します。元のファイルは変えません。",
                      info("mojifix").glyph or G.TEXT, self.accent)
        self.state_pill = W.StatusPill("待機中", "off")
        hero.add_pill(self.state_pill)
        self.add(hero)

    def _build_tiles(self) -> None:
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        self.t_today = W.StatTile("今日直した", "0", G.SPARKLE, self.accent)
        self.t_queue = W.StatTile("積んだファイル", "0", G.LIST, T.INFO)
        self.t_names = W.StatTile("直せる名前", "—", G_NAME, T.WARN)
        for t in (self.t_today, self.t_queue, self.t_names):
            lay.addWidget(t, 1)
        self.add(box)

    def _build_switch(self) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.seg = W.Segmented([(AREA_TEXT, "文字化けしたファイル"), (AREA_ZIP, "zip の名前"), (AREA_RENAME, "名前の濁点")],
                               AREA_TEXT, self.accent)
        self.seg.changed.connect(self._switch)
        row.addWidget(self.seg)
        row.addStretch(1)
        holder = QWidget()
        holder.setLayout(row)
        self.add(holder)

    def _build_progress(self) -> None:
        self.prog = QFrame()
        self.prog.setObjectName("Card")
        lay = QHBoxLayout(self.prog)
        lay.setContentsMargins(16, 10, 12, 10)
        lay.setSpacing(12)
        mid = QVBoxLayout()
        mid.setSpacing(4)
        self.prog_label = W.label("", None)
        mid.addWidget(self.prog_label)
        self.prog_bar = QProgressBar()
        self.prog_bar.setTextVisible(False)
        self.prog_bar.setFixedHeight(6)
        mid.addWidget(self.prog_bar)
        lay.addLayout(mid, 1)
        self.cancel_btn = W.button("中止", "danger", G.CLOSE, on_click=self._cancel)
        lay.addWidget(self.cancel_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self.prog.setVisible(False)
        self.add(self.prog)

    @safe("cancel")
    def _cancel(self) -> None:
        self.m.cancel()
        self.prog_label.setText("中止しています…(今の1件を終えてから止まります)")

    @safe("switch")
    def _switch(self, area: str) -> None:
        self._area = area
        target = {AREA_TEXT: self.p_text, AREA_ZIP: self.p_zip, AREA_RENAME: self.p_rename}[area]
        self._fit_stack(target)
        self.stack.switch_to(target)

    def _fit_stack(self, current: QWidget) -> None:
        """切り替えの高さを今の画面に合わせる(ほかの画面の高さで間延びしない)。"""
        for p in (self.p_text, self.p_zip, self.p_rename):
            pol = QSizePolicy.Policy.Preferred if p is current else QSizePolicy.Policy.Ignored
            p.setSizePolicy(QSizePolicy.Policy.Preferred, pol)
        self.stack.updateGeometry()

    @safe("area")
    def _on_area(self, area: str) -> None:
        if area != self._area:
            self.seg.set_value(area, emit=False)
            self._switch(area)

    @safe("progress")
    def _on_progress(self, kind: str, stage: str, done: int, total: int) -> None:
        self._show_progress(kind, stage, done, total)

    def _show_progress(self, kind: str, stage: str, done: int, total: int) -> None:
        name = STAGES.get(stage if stage != "start" else KIND_STAGE.get(kind, "start"), "処理しています")
        if kind == "scan":
            self.prog_label.setText(f"{name}({done:,} 項目)")
            self.prog_bar.setRange(0, 0)
        elif total > 0:
            self.prog_bar.setRange(0, 1000)
            self.prog_bar.setValue(int(1000 * min(done, total) / total))
            self.prog_label.setText(f"{name}({int(100 * min(done, total) / total)}%)")
        else:
            self.prog_bar.setRange(0, 0)
            self.prog_label.setText(name)

    @safe("changed")
    def _on_changed(self, area: str) -> None:
        if area == "busy":
            self._refresh_busy()
            self._refresh_text()
            self._refresh_zip()
            self._refresh_rename()
        elif area == AREA_TEXT:
            self._refresh_text()
        elif area == AREA_ZIP:
            self._refresh_zip()
        elif area == AREA_RENAME:
            self._refresh_rename()
        self._refresh_tiles()

    def _refresh_all(self) -> None:
        self._refresh_busy()
        self._refresh_text()
        self._refresh_zip()
        self._refresh_rename()
        self._refresh_tiles()

    def _refresh_busy(self) -> None:
        busy = self.m.busy()
        self.prog.setVisible(busy)
        if busy:
            st = self.m.progress_state or ("", "start", 0, 0)
            self._show_progress(*st)
            self.state_pill.set_state("info", "処理中")
        else:
            self.state_pill.set_state("off", "待機中")

    def _refresh_tiles(self) -> None:
        try:
            self.t_today.set_value(str(self.m.today_fixed()))
        except Exception:  # noqa: BLE001 - 数え直しに失敗しても画面は止めない
            pass
        self.t_queue.set_value(str(len(self.m.text_queue)))
        sc = self.m.rn_scan
        self.t_names.set_value(str(sum(1 for x in sc.items if x.fixable and not x.result)) if sc is not None else "—")

    # ================================================================ ドロップ(FR-1)
    def _zones(self) -> list[_DropZone]:
        return [self.text_drop, self.zip_drop, self.rn_drop]

    def dragEnterEvent(self, e: QDragEnterEvent) -> None:  # noqa: N802
        try:
            if _local_paths(e.mimeData()):
                e.acceptProposedAction()
                for z in self._zones():
                    z.set_hot(True)
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
        for z in self._zones():
            z.set_hot(False)
        super().dragLeaveEvent(e)

    def dropEvent(self, e: QDropEvent) -> None:  # noqa: N802
        for z in self._zones():
            z.set_hot(False)
        try:
            paths = _local_paths(e.mimeData())
            if paths:
                e.acceptProposedAction()
                self.m.add_paths(paths)
        except Exception as ex:  # noqa: BLE001
            _log.error("drop failed: %s", type(ex).__name__)

    # ================================================================ 文字化けしたファイル
    def _build_text(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)
        card = W.Card("文字化けしたファイル", "CSV やテキストを積み、1件ずつ読み方を選んで、正しい形の別のファイルを作ります。",
                      G_TEXT, self.accent)
        self.text_drop = _DropZone(self.accent, G.DOWNLOAD, "化けたファイルを、ここにドロップ",
                                   "zip はそのまま「zip の名前」へ、フォルダは「名前の濁点」へ振り分けます。",
                                   W.button("ファイルを選ぶ", "secondary", G.FOLDER, on_click=self._pick_text))
        card.add(self.text_drop)
        self.text_full = _Note()
        card.add(self.text_full)
        self.text_list = QListWidget()
        self.text_list.setMaximumHeight(170)
        self.text_list.itemClicked.connect(self._on_text_item)
        card.add(self.text_list)
        row = QHBoxLayout()
        row.addStretch(1)
        self.text_remove = W.button("一覧から外す", "ghost", G.CLOSE, on_click=self._remove_text)
        row.addWidget(self.text_remove)
        card.add_layout(row)
        lay.addWidget(card)

        card2 = W.Card("読み方の候補", "読める候補を選んでください。選ぶまで書き出せません。", G.EYE, self.accent)
        self.text_note = _Note()
        card2.add(self.text_note)
        self.text_extra = _Note()
        card2.add(self.text_extra)
        self.to_zip_btn = W.button("「zip の名前」で開く", "secondary", G_ZIP, on_click=self._text_to_zip)
        r2 = QHBoxLayout()
        r2.addWidget(self.to_zip_btn)
        r2.addStretch(1)
        card2.add_layout(r2)
        self.cand_box = QVBoxLayout()
        self.cand_box.setSpacing(8)
        card2.add_layout(self.cand_box)
        self.more_btn = W.button("ほかの候補", "ghost", G.CHEVRON, on_click=self._toggle_more)
        r3 = QHBoxLayout()
        r3.addWidget(self.more_btn)
        r3.addStretch(1)
        card2.add_layout(r3)
        self.more_box = QVBoxLayout()
        self.more_box.setSpacing(8)
        card2.add_layout(self.more_box)
        card2.add(W.label("選んだ候補の先頭 40 行", "Dim"))
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.preview.setFont(_mono_font(13))
        self.preview.setMinimumHeight(260)
        self.preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.preview.setPlaceholderText("候補をクリックすると、ここに大きく出ます。")
        card2.add(self.preview)
        lay.addWidget(card2)

        card3 = W.Card("書き出し", "元のファイルの隣に「<元の名前>_文字直し」で作ります。元のファイルは1バイトも変えません。",
                       G.SAVE, self.accent)
        c = self.m.config
        self.out_seg = W.Segmented([("excel", "Excel で開ける形"), ("sjis", "古いソフト用"), ("utf8", "UTF-8(印なし)")],
                                   c.text_output, self.accent)
        self.out_seg.changed.connect(lambda v: self._set_opt("text_output", v))
        card3.add(W.SettingRow("形", "Excel で開くなら「Excel で開ける形」(UTF-8・印あり)。古い業務ソフトなら Shift_JIS。", self.out_seg))
        card3.add(W.divider())
        self.nl_seg = W.Segmented([("keep", "そのまま"), ("crlf", "Windows の形(CRLF)"), ("lf", "LF")], c.newline, self.accent)
        self.nl_seg.changed.connect(lambda v: self._set_opt("newline", v))
        card3.add(W.SettingRow("改行", None, self.nl_seg))
        card3.add(W.divider())
        self.compose_toggle = W.ToggleSwitch(c.text_compose, self.accent)
        self.compose_toggle.toggled.connect(lambda v: self._set_opt("text_compose", bool(v)))
        card3.add(W.SettingRow("分かれた濁点をくっつける", "Mac で作った文字の「か゛」のような形を「が」にします。", self.compose_toggle))
        row4 = QHBoxLayout()
        self.export_hint = W.label("", "Mute", wrap=True)
        row4.addWidget(self.export_hint, 1)
        self.export_btn = W.button("書き出す", "primary", G.SAVE, on_click=self._export)
        self.export_btn.setMinimumWidth(150)
        row4.addWidget(self.export_btn)
        card3.add_layout(row4)
        self.text_result = _Note()
        card3.add(self.text_result)
        r5 = QHBoxLayout()
        self.open_out_btn = W.button("開く", "secondary", G.OPEN, on_click=self._open_out)
        self.open_dir_btn = W.button("フォルダを開く", "secondary", G.FOLDER, on_click=self._open_out_dir)
        r5.addWidget(self.open_out_btn)
        r5.addWidget(self.open_dir_btn)
        r5.addStretch(1)
        card3.add_layout(r5)
        lay.addWidget(card3)
        lay.addStretch(1)
        return page

    @safe("text:pick")
    def _pick_text(self) -> None:
        files, _f = QFileDialog.getOpenFileNames(self._parent(), "直すファイルを選ぶ", "", "すべてのファイル (*.*)")
        if files:
            self.m.add_paths(files)

    @safe("text:item")
    def _on_text_item(self, item: QListWidgetItem) -> None:
        p = item.data(Qt.ItemDataRole.UserRole)
        if p and not self.m.busy() and Path(p) != self.m.text_current:
            self.m.select_text(Path(p))

    @safe("text:remove")
    def _remove_text(self) -> None:
        it = self.text_list.currentItem()
        if it is not None:
            self.m.remove_text(Path(it.data(Qt.ItemDataRole.UserRole)))

    @safe("text:tozip")
    def _text_to_zip(self) -> None:
        if self.m.text_current is not None:
            p = self.m.text_current
            self.m.remove_text(p)
            self.m.set_zip(p)
            self._on_area(AREA_ZIP)

    @safe("text:more")
    def _toggle_more(self) -> None:
        self._shown_others = not self._shown_others
        self._apply_more()

    def _apply_more(self) -> None:
        for i in range(self.more_box.count()):
            it = self.more_box.itemAt(i)
            w = it.widget() if it is not None else None
            if w is not None:
                w.setVisible(self._shown_others)
        n = self.more_box.count()
        self.more_btn.setVisible(n > 0)
        self.more_btn.setText(f"{G.CHEVRON}  ほかの候補({n})" + (" を隠す" if self._shown_others else ""))

    @safe("text:choose")
    def _choose(self, key: str) -> None:
        self.m.choose(key)

    @safe("opt")
    def _set_opt(self, key: str, value: Any) -> None:
        err = self.m.set_option(key, value)
        if err:
            W.message(self._parent(), "保存できませんでした", err, kind="error")
        self._refresh_text()
        self._refresh_zip()

    @safe("text:export")
    def _export(self) -> None:
        self.m.export()

    @safe("text:open")
    def _open_out(self) -> None:
        if self.m.text_out is not None:
            _open_path(self.m.text_out.path)

    @safe("text:opendir")
    def _open_out_dir(self) -> None:
        if self.m.text_out is not None:
            _open_path(self.m.text_out.path.parent)

    def _clear_box(self, box: QVBoxLayout) -> None:
        while box.count():
            it = box.takeAt(0)
            w = it.widget() if it is not None else None
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _refresh_text(self) -> None:
        m = self.m
        busy = m.busy()
        # 一覧
        self.text_list.blockSignals(True)
        self.text_list.clear()
        for p in m.text_queue:
            it = QListWidgetItem(("▶  " if p == m.text_current else "      ") + p.name)
            it.setData(Qt.ItemDataRole.UserRole, str(p))
            it.setToolTip(str(p))
            self.text_list.addItem(it)
            if p == m.text_current:
                it.setSelected(True)
                self.text_list.setCurrentItem(it)
        self.text_list.blockSignals(False)
        self.text_list.setFixedHeight(min(170, 12 + 34 * max(1, len(m.text_queue))))
        self.text_list.setVisible(bool(m.text_queue))
        self.text_list.setEnabled(not busy)
        self.text_remove.setVisible(bool(m.text_queue))
        self.text_remove.setEnabled(not busy)
        self.text_full.set_notice(Notice("warn", MSG_QUEUE_FULL) if m.queue_full else None)
        # 候補(中身が変わったときだけ作り直す)
        sn = m.sniff
        sig = (id(sn), m.text_current)
        if sig != self._text_sig:
            self._text_sig = sig
            self._rebuild_cands()
        for card in self._cards:
            card.set_selected(card.key == m.chosen)
            card.setEnabled(not busy)
        chosen = sn.candidate(m.chosen) if (sn is not None and m.chosen) else None
        self.preview.setPlainText("\n".join(chosen.lines) if chosen is not None else "")
        # 注意書き
        notice = m.text_notice if m.text_notice is not None and m.text_notice.kind != "ok" else None
        self.text_note.set_notice(notice)
        extra: Notice | None = None
        if sn is not None and sn.kind == "text":
            if sn.bom_mismatch:
                extra = Notice("warn", S.MSG_BOM_MISMATCH)
            elif sn.all_bad:
                extra = Notice("warn", S.MSG_ALL_BAD)
        self.text_extra.set_notice(extra)
        self.to_zip_btn.setVisible(m.text_is_zip)
        # 書き出し
        c = m.config
        if self.out_seg.value() != c.text_output:
            self.out_seg.set_value(c.text_output, animate=False)
        if self.nl_seg.value() != c.newline:
            self.nl_seg.set_value(c.newline, animate=False)
        self.compose_toggle.set_checked_silent(c.text_compose)
        self.export_btn.setEnabled(m.can_export())
        if m.text_current is None:
            self.export_hint.setText("ファイルを積むと、ここから書き出せます。")
        elif m.chosen is None and sn is not None and sn.kind == "text":
            self.export_hint.setText("読める候補を1つクリックすると、書き出せます。")
        else:
            self.export_hint.setText("書く前に全体を確かめます。読めない所や書けない文字があれば、書かずに止まります。")
        ok = m.text_notice if (m.text_notice is not None and m.text_notice.kind == "ok") else None
        self.text_result.set_notice(ok)
        has_out = m.text_out is not None and ok is not None
        self.open_out_btn.setVisible(has_out)
        self.open_dir_btn.setVisible(has_out)
        # FR-11・FR-12 の問い
        if m.text_ask and m.text_check is not None and not self._asking and not busy:
            self._asking = True
            try:
                self._handle_ask(m.text_ask, m.text_check)
            finally:
                self._asking = False

    def _rebuild_cands(self) -> None:
        self._clear_box(self.cand_box)
        self._clear_box(self.more_box)
        self._cards = []
        self._shown_others = False
        sn = self.m.sniff
        if sn is not None and sn.kind in ("text", "ascii"):
            for i, c in enumerate(sn.candidates):
                badge = "印あり" if c.bom and c.key == "utf8" else ("先頭の印" if c.bom else None)
                card = _CandCard(c.key, c.label, c.state_text(), c.errors == 0, c.lines[:S.CARD_LINES], self.accent, badge)
                card.clicked.connect(self._choose)
                self._cards.append(card)
                (self.cand_box if i < 3 else self.more_box).addWidget(card)
        self._apply_more()

    def _handle_ask(self, kind: str, chk: TF.CheckResult) -> None:
        choice = self.ask_choice(kind, chk)
        m = self.m
        if kind == "undecodable":
            if choice == "geta":
                m.continue_export(geta_undecodable=True)
            else:
                m.refuse_export()
        else:
            if choice == "excel":
                m.continue_export(out="excel")
            elif choice == "geta":
                m.continue_export(geta_unencodable=True)
            else:
                m.refuse_export()

    def _issue_html(self, lines: Sequence[TF.IssueLine]) -> str:
        rows: list[str] = []
        for ln in lines:
            segs = "".join(
                f'<span style="background:{T.alpha(T.DANGER, 0.35)}; color:{T.TEXT}; font-weight:600;">{html.escape(t)}</span>'
                if hit else html.escape(t) for t, hit in ln.segments)
            rows.append(f'<div style="white-space:pre;"><span style="color:{T.TEXT_MUTE};">{ln.line_no:>7,} 行目 │ </span>{segs}</div>')
        return f'<div style="font-family:{MONO}; font-size:12px;">{"".join(rows)}</div>'

    def _ask_dialog(self, kind: str, chk: TF.CheckResult) -> str:
        from PySide6.QtWidgets import QDialog

        if kind == "undecodable":
            title = "読めない所があります"
            text = (f"{chk.first_undecodable_line:,} 行目から読めなくなります(途中から別の形かもしれません)。"
                    f"読めない所は全部で {chk.undecodable:,} か所です。")
            lines = chk.undecodable_lines
            buttons = [("reselect", "候補を選び直す", "ghost"), ("geta", "読めない所を〓にして続ける", "primary")]
        else:
            title = "古いソフト用の形で書けない文字があります"
            text = f"書けない文字が {chk.unencodable:,} 個あります(— や絵文字など)。どうしますか。"
            lines = chk.unencodable_lines
            buttons = [("stop", "やめる", "ghost"), ("geta", "書けない文字を〓にして書き出す", "secondary"),
                       ("excel", "Excel で開ける形に変える(おすすめ)", "primary")]
        dlg = W.StyledDialog(self._parent(), title, G.WARNING, T.WARN, width=620)
        dlg.body.addWidget(W.label(text, "Dim", wrap=True))
        view = QTextEdit()
        view.setReadOnly(True)
        view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        view.setHtml(self._issue_html(lines))
        view.setMinimumHeight(240)
        dlg.body.addWidget(view)
        if len(lines) >= TF.MAX_ISSUE_LINES:
            dlg.body.addWidget(W.label(f"最初の {TF.MAX_ISSUE_LINES} 行だけを出しています。", "Mute"))
        picked = {"v": buttons[0][0]}

        def pick(v: str) -> None:
            picked["v"] = v
            dlg.accept()

        for val, label, kind_ in buttons:
            b = W.button(label, kind_)
            b.clicked.connect(lambda _=False, v=val: pick(v))
            dlg.buttons.addWidget(b)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return buttons[0][0]
        return picked["v"]

    def _confirm_dialog(self, title: str, text: str, ok_text: str) -> bool:
        ok, _ = W.confirm(self._parent(), title, text, ok_text=ok_text)
        return ok

    # ================================================================ zip の名前
    def _build_zip(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)
        card = W.Card("zip の名前", "Mac などから届いた zip の、化けた名前を読み直して、新しいフォルダへ展開します。", G_ZIP, self.accent)
        self.zip_drop = _DropZone(self.accent, G_ZIP, "zip を、ここにドロップ", "zip と同じ場所に、zip の名前のフォルダを作って展開します。",
                                  W.button("zip を選ぶ", "secondary", G.FOLDER, on_click=self._pick_zip))
        card.add(self.zip_drop)
        self.zip_name = W.label("", "Dim", wrap=True)
        card.add(self.zip_name)
        self.zip_note = _Note()
        card.add(self.zip_note)
        self.zip_cand_box = QVBoxLayout()
        self.zip_cand_box.setSpacing(8)
        card.add_layout(self.zip_cand_box)
        lay.addWidget(card)

        card2 = W.Card("展開する名前", "候補を選ぶと、全部の項目の名前と、展開するか・飛ばすかが出ます。", G.LIST, self.accent)
        self.plan_summary = W.label("", "Dim", wrap=True)
        card2.add(self.plan_summary)
        self.plan_model = _PlanModel()
        self.plan_table = _table(self.plan_model, 280)
        h = self.plan_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        h.resizeSection(0, 260)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        h.resizeSection(1, 260)
        card2.add(self.plan_table)
        c = self.m.config
        self.zip_compose = W.ToggleSwitch(c.zip_compose, self.accent)
        self.zip_compose.toggled.connect(lambda v: self._set_opt("zip_compose", bool(v)))
        card2.add(W.SettingRow("分かれた濁点をくっつける", "Mac で作った名前の「か゛」のような形を「が」にします。", self.zip_compose))
        self.zip_mac = W.ToggleSwitch(c.zip_skip_mac_files, self.accent)
        self.zip_mac.toggled.connect(lambda v: self._set_opt("zip_skip_mac_files", bool(v)))
        card2.add(W.SettingRow("Mac の管理用ファイルを飛ばす", "__MACOSX と .DS_Store は Windows では使いません。", self.zip_mac))
        self.adv_btn = W.button("詳しい設定", "ghost", G.SETTINGS, on_click=self._toggle_adv)
        r = QHBoxLayout()
        r.addWidget(self.adv_btn)
        r.addStretch(1)
        card2.add_layout(r)
        self.max_gb = QSpinBox()
        self.max_gb.setRange(cfgmod.ZIP_GB_MIN, cfgmod.ZIP_GB_MAX)
        self.max_gb.setValue(c.zip_max_total_gb)
        self.max_gb.setSuffix(" GB")
        self.max_gb.setKeyboardTracking(False)
        self.max_gb.setMinimumWidth(110)
        self.max_gb.valueChanged.connect(lambda v: self._set_opt("zip_max_total_gb", int(v)))
        self.adv_row = W.SettingRow("展開する大きさの上限", "中身の合計がこれを超える zip は、安全のため展開しません"
                                    "(1〜200 GB)。1 項目が 1,000 倍・全体が 200 倍を超えて広がる zip も止めます。", self.max_gb)
        self.adv_row.setVisible(False)
        card2.add(self.adv_row)
        row = QHBoxLayout()
        self.zip_hint = W.label("", "Mute", wrap=True)
        row.addWidget(self.zip_hint, 1)
        self.extract_btn = W.button("展開する", "primary", G.DOWNLOAD, on_click=self._extract)
        self.extract_btn.setMinimumWidth(150)
        row.addWidget(self.extract_btn)
        card2.add_layout(row)
        self.zip_result = _Note()
        card2.add(self.zip_result)
        r2 = QHBoxLayout()
        self.zip_open_btn = W.button("フォルダを開く", "secondary", G.FOLDER, on_click=self._open_zip_dest)
        r2.addWidget(self.zip_open_btn)
        r2.addStretch(1)
        card2.add_layout(r2)
        lay.addWidget(card2)
        lay.addStretch(1)
        return page

    @safe("zip:pick")
    def _pick_zip(self) -> None:
        f, _flt = QFileDialog.getOpenFileName(self._parent(), "zip を選ぶ", "", "zip (*.zip);;すべてのファイル (*.*)")
        if f:
            self.m.set_zip(Path(f))

    @safe("zip:adv")
    def _toggle_adv(self) -> None:
        self.adv_row.setVisible(not self.adv_row.isVisible())

    @safe("zip:choose")
    def _choose_zip(self, key: str) -> None:
        self.m.choose_zip(key)

    @safe("zip:extract")
    def _extract(self) -> None:
        self.m.extract_zip()

    @safe("zip:open")
    def _open_zip_dest(self) -> None:
        if self.m.zip_result is not None:
            _open_path(self.m.zip_result.dest)

    def _refresh_zip(self) -> None:
        m = self.m
        busy = m.busy()
        self.zip_name.setText(f"{m.zip_path.name}" if m.zip_path is not None else "")
        self.zip_name.setToolTip(str(m.zip_path) if m.zip_path is not None else "")
        self.zip_name.setVisible(m.zip_path is not None)
        n = m.zip_notice
        self.zip_note.set_notice(n if (n is not None and n.kind != "ok") else None)
        sig = (id(m.zip_scan), tuple(c.key for c in m.zip_cands))
        if sig != self._zip_sig:
            self._zip_sig = sig
            self._clear_box(self.zip_cand_box)
            self._zip_cards = []
            for nc in m.zip_cands:
                state = "読めない名前: 0 件" if nc.bad_names == 0 else f"読めない名前: {nc.bad_names:,} 件"
                card = _CandCard(nc.key, nc.label, state, nc.bad_names == 0, nc.sample, self.accent, visible_lines=8)
                card.clicked.connect(self._choose_zip)
                self._zip_cards.append(card)
                self.zip_cand_box.addWidget(card)
        for card in self._zip_cards:
            card.set_selected(card.key == m.zip_key)
            card.setEnabled(not busy)
        if self.plan_model.items is not m.zip_plan:
            self.plan_model.set_items(m.zip_plan)
        plan = m.zip_plan
        if plan:
            ext = sum(1 for p in plan if p.status == "extract")
            ren = sum(1 for p in plan if p.status == "rename")
            skip = sum(1 for p in plan if p.status == "skip")
            dest = m.zip_dest_preview()
            self.plan_summary.setText(f"{len(plan):,} 項目: そのまま {ext:,}・名前を変える {ren:,}・飛ばす {skip:,}"
                                      + (f"\n展開先: {dest}(同じ名前があれば番号を付けます)" if dest is not None else ""))
        else:
            self.plan_summary.setText("zip を選び、読み方の候補を選ぶと、ここに一覧が出ます。" if m.zip_scan is None or m.zip_cands
                                      else "")
        c = m.config
        self.zip_compose.set_checked_silent(c.zip_compose)
        self.zip_mac.set_checked_silent(c.zip_skip_mac_files)
        if self.max_gb.value() != c.zip_max_total_gb:
            self.max_gb.blockSignals(True)
            self.max_gb.setValue(c.zip_max_total_gb)
            self.max_gb.blockSignals(False)
        self.extract_btn.setEnabled(m.can_extract())
        self.zip_hint.setText("" if m.can_extract() or busy else
                              ("読める候補を1つクリックすると、展開できます。" if m.zip_cands and m.zip_key is None else ""))
        ok = n if (n is not None and n.kind == "ok") else None
        self.zip_result.set_notice(ok)
        self.zip_open_btn.setVisible(ok is not None and m.zip_result is not None)

    # ================================================================ 名前の濁点
    def _build_rename(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)
        card = W.Card("名前の濁点", "Mac から来た名前は、濁点が分かれて検索で見つからないことがあります。フォルダの中を調べて直します。",
                      G_NAME, self.accent)
        self.rn_drop = _DropZone(self.accent, G.FOLDER, "フォルダを、ここにドロップ", "調べるだけでは何も変えません。",
                                 W.button("フォルダを選ぶ", "secondary", G.FOLDER, on_click=self._pick_folder))
        card.add(self.rn_drop)
        self.rn_name = W.label("", "Dim", wrap=True)
        card.add(self.rn_name)
        self.rn_recursive = W.ToggleSwitch(self.m.config.rename_recursive, self.accent)
        self.rn_recursive.toggled.connect(lambda v: self._set_opt("rename_recursive", bool(v)))
        card.add(W.SettingRow("サブフォルダも調べる", None, self.rn_recursive))
        row = QHBoxLayout()
        row.addStretch(1)
        self.scan_btn = W.button("調べる", "primary", G.SEARCH, on_click=self._scan)
        self.scan_btn.setMinimumWidth(130)
        row.addWidget(self.scan_btn)
        card.add_layout(row)
        self.rn_note = _Note()
        card.add(self.rn_note)
        lay.addWidget(card)

        card2 = W.Card("見つかった名前", "直せるものだけを選べます。同じ名前がすでにあるものは、どちらを残すか分からないので変えません。",
                       G.LIST, self.accent)
        self.rn_model = _RenameModel(self)
        self.rn_table = _table(self.rn_model, 300)
        self.rn_table.setItemDelegateForColumn(1, _HtmlDelegate(self.rn_table))
        h = self.rn_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        h.resizeSection(0, 34)
        for col, w in ((1, 200), (2, 200), (3, 180)):
            h.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            h.resizeSection(col, w)
        self.rn_table.clicked.connect(self._rn_clicked)
        card2.add(self.rn_table)
        row2 = QHBoxLayout()
        self.undo_btn = W.button("元に戻す", "secondary", G.UNDO, on_click=self._undo)
        row2.addWidget(self.undo_btn)
        self.undo_note = W.label(R.MSG_UNDO_NOTE, "Mute")
        row2.addWidget(self.undo_note)
        row2.addStretch(1)
        self.rename_btn = W.button("名前を直す", "primary", G.CHECK, on_click=self._rename)
        self.rename_btn.setMinimumWidth(180)
        row2.addWidget(self.rename_btn)
        card2.add_layout(row2)
        lay.addWidget(card2)
        lay.addStretch(1)
        return page

    @safe("rn:pick")
    def _pick_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self._parent(), "調べるフォルダを選ぶ")
        if d:
            self.m.set_folder(Path(d))

    @safe("rn:scan")
    def _scan(self) -> None:
        if self.m.folder_needs_confirm() and not self.confirm("ドライブ全体を調べる", R.MSG_ROOT, "調べる"):
            return
        self.m.scan_folder()

    @safe("rn:click")
    def _rn_clicked(self, index: QModelIndex) -> None:
        if index.column() != 0 and self.rn_model.flags(self.rn_model.index(index.row(), 0)) & Qt.ItemFlag.ItemIsUserCheckable:
            on = index.row() not in self.m.rn_selected
            self.m.set_selected(index.row(), on)

    @safe("rn:rename")
    def _rename(self) -> None:
        n = len(self.m.selected_items())
        if n == 0:
            return
        if not self.confirm("名前を直す", f"{n:,} 件の名前を直します。\n{R.MSG_WARN}。", "直す"):
            return
        self.m.rename_selected()

    @safe("rn:undo")
    def _undo(self) -> None:
        self.m.undo()

    def _refresh_rename(self) -> None:
        m = self.m
        busy = m.busy()
        f = m.rn_folder
        self.rn_name.setText(str(f) if f is not None else "")
        self.rn_name.setVisible(f is not None)
        self.rn_recursive.set_checked_silent(m.config.rename_recursive)
        self.scan_btn.setEnabled(f is not None and not busy and not R.is_forbidden(f, m._env))
        self.rn_note.set_notice(m.rn_notice)
        items = m.rn_scan.items if m.rn_scan is not None else []
        if self.rn_model.items is not items:
            self.rn_model.set_items(items)
        else:
            self.rn_model.refresh()
        n = len(m.selected_items())
        self.rename_btn.setText(f"{G.CHECK}  選んだ {n:,} 件の名前を直す")
        self.rename_btn.setEnabled(n > 0 and not busy)
        self.undo_btn.setVisible(bool(m.rn_done))
        self.undo_note.setVisible(bool(m.rn_done))
        self.undo_btn.setEnabled(m.can_undo())
        self.rn_table.setEnabled(not busy)
