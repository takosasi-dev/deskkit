# Control Center の PagePress 画面。4つのタブ(まとめる・分ける・ページの整理・軽くする)・投入エリア・一覧・進捗と中止・結果(FR-1〜FR-20)。
# ファイル名は画面にだけ出す(V-8)。シグナルから呼ぶ処理はすべて guard で例外を握り、ログには型名だけを書く(INV-5)。
# 整理の格子は organize_view の QListView(1ページ1ウィジェットにしない)。書く前の確認(P-6)と、保存していない変更の確認(FR-9)はここで出す。
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QFont,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from deskkit.modules.pagepress import jobs as J
from deskkit.modules.pagepress import ranges
from deskkit.modules.pagepress import sendto as sendto_mod
from deskkit.modules.pagepress.organize_view import OrganizeView, PageDelegate, PageModel
from deskkit.modules.pagepress.reader import Entry
from deskkit.ui import theme as T
from deskkit.ui import widgets as W
from deskkit.ui.theme import G

if TYPE_CHECKING:
    from deskkit.modules.pagepress.module import PagePressModule

_log = logging.getLogger("deskkit.pagepress")

TAB_OPTIONS = [("merge", "まとめる"), ("split", "分ける"), ("organize", "ページの整理"), ("compress", "軽くする")]
FILTER_ALL = "PDF と写真 (*.pdf *.jpg *.jpeg *.jpe *.jfif *.png *.heic *.heif);;すべてのファイル (*.*)"
FILTER_PDF = "PDF (*.pdf);;すべてのファイル (*.*)"
G_PDF = ""
G_ROTATE_L = ""
G_ROTATE_R = ""
G_REDO = ""
LEVEL_TEXT = {"light": ("少し", "200dpi・画質 85"), "normal": ("ふつう", "150dpi・画質 75"), "strong": ("うんと", "100dpi・画質 60")}
NOTE_LINES = 8


def guard(fn: Callable[..., Any], label: str = "ui") -> Callable[..., Any]:
    """Qt のシグナルから呼ぶ処理の例外を握る。ログには例外の型名だけ(パスを含む文を書かない。INV-5)。"""

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


def _placeholder(kind: str, size: QSize) -> QPixmap:
    pm = QPixmap(size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(4, 2, size.width() - 8, size.height() - 4), 6, 6)
    p.fillPath(path, QColor(T.SURFACE3))
    p.setPen(QColor(T.TEXT_MUTE))
    f = QFont()
    f.setPixelSize(10)
    f.setBold(True)
    p.setFont(f)
    p.drawText(QRectF(0, 0, size.width(), size.height()), Qt.AlignmentFlag.AlignCenter, "PDF" if kind == "pdf" else "IMG")
    p.end()
    return pm


def _thumb_pixmap(img: QImage, size: QSize) -> QPixmap:
    pm = QPixmap(size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    k = min((size.width() - 8) / max(1, img.width()), (size.height() - 4) / max(1, img.height()))
    w, h = img.width() * k, img.height() * k
    r = QRectF((size.width() - w) / 2, (size.height() - h) / 2, w, h)
    p.fillRect(r.translated(0, 1.5), QColor(0, 0, 0, 50))
    p.drawImage(r, img)
    p.setPen(QPen(QColor(T.BORDER_HI), 1))
    p.drawRect(r)
    p.end()
    return pm


class _Note(QFrame):
    """色付きの注意書き(足さなかった物・失敗)。"""

    def __init__(self, color: str, glyph: str) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(10)
        self.glyph = W.Glyph(glyph, 14, color)
        lay.addWidget(self.glyph, 0, Qt.AlignmentFlag.AlignTop)
        self.label = W.label("", None, wrap=True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.label, 1)
        self.set_color(color)
        self.setVisible(False)

    def set_color(self, color: str) -> None:
        self.setStyleSheet(f"QFrame {{ background: {T.alpha(color, 0.08)}; border: 1px solid {T.alpha(color, 0.30)};"
                           f" border-radius: 10px; }}")
        self.glyph.setStyleSheet(f"color: {color}; background: transparent; border: none;")
        self.label.setStyleSheet(f"color: {T.TEXT_DIM}; background: transparent; border: none; font-size: 12px;")

    def set_text(self, text: str) -> None:
        self.label.setText(text)
        self.setVisible(bool(text))


class _DropZone(QFrame):
    """点線の枠の投入エリア。ページ全体がドロップを受けるので、ここは見た目と「ファイルを選ぶ」だけ。"""

    def __init__(self, accent: str, title: str, sub: str, on_pick: Callable[[], Any], compact: bool = False) -> None:
        super().__init__()
        self._accent = accent
        self._hot = False
        self.setMinimumHeight(120 if compact else 170)
        self.setMaximumHeight(150 if compact else 200)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 14, 24, 14)
        lay.setSpacing(6)
        lay.addStretch(1)
        g = W.Glyph(G.DOWNLOAD, 22, accent)
        g.setFixedSize(46, 46)
        g.setStyleSheet(f"color: {accent}; background: {T.alpha(accent, 0.12)}; border-radius: 23px;")
        lay.addWidget(g, 0, Qt.AlignmentFlag.AlignHCenter)
        t = W.label(title, "H3")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(t)
        s = W.label(sub, "Mute", wrap=True)
        s.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(s)  # 横いっぱいに広げて折り返す(真ん中寄せにすると幅が縮んで切れる)
        lay.addWidget(W.button("ファイルを選ぶ", "secondary", G.FOLDER, on_click=on_pick), 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addStretch(1)

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


class _Progress(QFrame):
    """段階・進捗バー・中止(FR-14)。"""

    def __init__(self, on_cancel: Callable[[], Any]) -> None:
        super().__init__()
        self.setObjectName("Inset")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(6)
        row = QHBoxLayout()
        self.phase = W.label("", None)
        row.addWidget(self.phase, 1)
        self.cancel = W.button("中止", "danger", G.CLOSE, on_click=on_cancel)
        row.addWidget(self.cancel)
        lay.addLayout(row)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        lay.addWidget(self.bar)
        self.setVisible(False)

    def show_status(self, text: str, done: int, total: int, cancelling: bool) -> None:
        self.setVisible(True)
        self.phase.setText(text)
        if total > 0:
            self.bar.setRange(0, total)
            self.bar.setValue(min(done, total))
        else:
            self.bar.setRange(0, 0)  # 動いている印
        self.cancel.setEnabled(not cancelling)


class _ResultRow(QFrame):
    """結果の1行(FR-20): 名前・ページ数と大きさ・「開く」「フォルダを開く」。"""

    def __init__(self, page: PagePressPage, title: str, row: J.Row) -> None:
        super().__init__()
        self.setObjectName("Inset")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 10, 8)
        lay.setSpacing(10)
        color = T.WARN if row.warn else (T.SUCCESS if row.ok else T.DANGER)
        ic = W.Glyph(G.CHECK if row.ok and not row.warn else (G.WARNING if row.warn else G.ERROR), 14, color)
        ic.setFixedSize(30, 30)
        ic.setStyleSheet(f"color: {color}; background: {T.alpha(color, 0.12)}; border-radius: 9px;")
        lay.addWidget(ic, 0, Qt.AlignmentFlag.AlignTop)
        mid = QVBoxLayout()
        mid.setSpacing(2)
        name = QLabel(title)
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        if row.path is not None:
            name.setToolTip(str(row.path))
        mid.addWidget(name)
        text = row.text
        if row.fallback and row.path is not None:
            text += f"・保存先: {row.path.parent}"
        st = W.label(text, "Mute", wrap=True)
        st.setStyleSheet(f"color: {color}; font-size: 12px;")
        st.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        mid.addWidget(st)
        lay.addLayout(mid, 1)
        if row.ok and row.path is not None and row.path.is_file():
            lay.addWidget(W.button("開く", "secondary", G.OPEN, on_click=guard(lambda: page.m.open_path(row.path), "res:open")))
        if row.ok and row.folder is not None:
            lay.addWidget(W.button("フォルダを開く", "ghost", G.FOLDER,
                                   on_click=guard(lambda: page.m.open_path(row.folder), "res:folder")))


class _Tab(QWidget):
    """1つのタブの共通部品(投入エリア・注意書き・実行の行・進捗・結果)。"""

    def __init__(self, page: PagePressPage, tab: str) -> None:
        super().__init__()
        self.page = page
        self.tab = tab
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(14)
        self.notes = _Note(T.WARN, G.WARNING)
        self.probing = W.label("", "Mute")
        self.probing.setVisible(False)
        self.run_hint = W.label("", "Mute", wrap=True)
        self.run_btn: QPushButton | None = None
        self.busy_note = W.label(J.MSG_BUSY, "Mute")
        self.busy_note.setStyleSheet(f"color: {T.WARN}; font-size: 12px;")
        self.busy_note.setVisible(False)
        self.progress = _Progress(guard(page.m.cancel_job, f"{tab}:cancel"))
        self.error = _Note(T.DANGER, G.ERROR)
        self.results_box = QWidget()
        self.results_lay = QVBoxLayout(self.results_box)
        self.results_lay.setContentsMargins(0, 0, 0, 0)
        self.results_lay.setSpacing(6)

    def add_intake(self, card: W.Card, zone: _DropZone) -> None:
        card.add(zone)
        card.add(self.probing)
        card.add(self.notes)

    def add_run_row(self, text: str, glyph: str, on_click: Callable[[], Any]) -> None:
        """実行の行をタブの最後に置く。"""
        self.lay.addWidget(self.run_row(text, glyph, on_click))

    def run_row(self, text: str, glyph: str, on_click: Callable[[], Any]) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        row = QHBoxLayout()
        row.addWidget(self.run_hint, 1)
        btn = W.button(text, "primary", glyph, on_click=on_click)
        btn.setMinimumWidth(170)
        self.run_btn = btn
        row.addWidget(btn)
        v.addLayout(row)
        v.addWidget(self.busy_note)
        v.addWidget(self.progress)
        v.addWidget(self.error)
        v.addWidget(self.results_box)
        return box

    def refresh_notes(self) -> None:
        m = self.page.m
        n = m.notes[self.tab]
        lines: list[str] = []
        if n.rejected:
            lines.append(f"追加しなかったファイル: {len(n.rejected)} 件")
            for r in n.rejected[:NOTE_LINES]:
                lines.append(f"・{r.path.name} — {r.message}")
            if len(n.rejected) > NOTE_LINES:
                lines.append(f"・ほか {len(n.rejected) - NOTE_LINES} 件")
        if n.unsupported:
            lines.append(f"対応していない形式: {n.unsupported} 件")
        self.notes.set_text("\n".join(lines))
        k = m.probing[self.tab]
        self.probing.setText(f"確かめています…({k} 件)" if k else "")
        self.probing.setVisible(bool(k))

    def refresh_job(self) -> None:
        m = self.page.m
        st = m.job
        mine = st is not None and m.job_tab == self.tab
        running = st is not None and not st.finished
        self.busy_note.setVisible(running and not mine)
        if self.run_btn is not None:
            self.run_btn.setEnabled(not running and self.can_run())
        if mine and st is not None and running:
            self.progress.show_status(m.job_text(), st.done, st.total, m.cancel_slow or st.cancel.is_set())
        else:
            self.progress.setVisible(False)
        failed = mine and st is not None and st.finished and st.state != J.STATE_DONE and bool(st.message)
        text = ""
        if failed and st is not None:
            text = st.message
            if st.failed_index is not None and self.tab == "merge":
                lst = self.page.job_names
                if 0 <= st.failed_index < len(lst):
                    text = f"{st.failed_index + 1} 番目の「{lst[st.failed_index]}」: {st.message}"
        self.error.set_text(text)
        if st is not None and st.state == J.STATE_CANCELLED and mine:
            self.error.set_color(T.WARN)
        else:
            self.error.set_color(T.DANGER)
        self.refresh_results()

    def refresh_results(self) -> None:
        while self.results_lay.count():
            it = self.results_lay.takeAt(0)
            w = it.widget() if it is not None else None
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        m = self.page.m
        names = self.page.result_names
        for row in m.results.get(self.tab, []):
            fallback_title = row.path.name if row.path is not None else ""
            title = names.get(row.entry_id, "") if row.entry_id is not None else fallback_title
            self.results_lay.addWidget(_ResultRow(self.page, title, row))

    def can_run(self) -> bool:
        return True


class _ListTab(_Tab):
    """まとめる・分ける・軽くするの、ファイルの一覧を持つタブ。"""

    def build_list(self, card: W.Card, *, reorder: bool) -> None:
        self.list = QListWidget()
        self.list.setIconSize(QSize(46, 58))
        self.list.setFixedHeight(150)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        if reorder:
            self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
            self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
            self.list.model().rowsMoved.connect(guard(lambda *_a: self._dragged(), f"{self.tab}:moved"))
        self.list.itemSelectionChanged.connect(guard(self._sel_changed, f"{self.tab}:sel"))
        card.add(self.list)
        self.empty = W.EmptyState(G_PDF, "まだ何もありません", "上の枠にファイルをドロップするか、「ファイルを選ぶ」を押してください。")
        card.add(self.empty)
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.btn_up = W.button("上へ", "ghost", G.UP, on_click=guard(lambda: self._move(-1), f"{self.tab}:up"))
        self.btn_down = W.button("下へ", "ghost", G.DOWN, on_click=guard(lambda: self._move(1), f"{self.tab}:down"))
        self.btn_remove = W.button("外す", "ghost", G.CLOSE, on_click=guard(self._remove, f"{self.tab}:remove"),
                                   tooltip="一覧から外します(ファイルは消しません)")
        self.btn_sort = W.button("名前順", "ghost", G.FILTER, on_click=guard(lambda: self.page.m.sort_by_name(self.tab),
                                                                              f"{self.tab}:sort"))
        self.btn_clear = W.button("一覧を空にする", "ghost", G.CLEAR, on_click=guard(lambda: self.page.m.clear(self.tab),
                                                                                  f"{self.tab}:clear"))
        for b in ((self.btn_up, self.btn_down) if reorder else ()):
            bar.addWidget(b)
        bar.addWidget(self.btn_remove)
        if reorder:
            bar.addWidget(self.btn_sort)
        bar.addStretch(1)
        self.total = W.label("", "Mute")
        bar.addWidget(self.total)
        bar.addWidget(self.btn_clear)
        card.add_layout(bar)

    def entries(self) -> list[Entry]:
        return self.page.m.lists[self.tab]

    def row_text(self, e: Entry) -> str:
        size = J.fmt_size(e.size)
        if e.kind == "image":
            return f"{e.name}\n写真・1 ページ・{size}"
        return f"{e.name}\n{e.pages} ページ・{size}"

    def rebuild(self) -> None:
        sel = self._selected_id()
        self.list.blockSignals(True)
        self.list.clear()
        for e in self.entries():
            it = QListWidgetItem(self.row_text(e))
            it.setData(Qt.ItemDataRole.UserRole, e.id)
            it.setToolTip(str(e.path))
            it.setSizeHint(QSize(0, 66))
            self._set_icon(it, e)
            self.list.addItem(it)
            if e.id == sel:
                it.setSelected(True)
        self.list.blockSignals(False)
        self.list.setFixedHeight(max(150, min(460, self.list.count() * 68 + 14)))  # 行の数に合わせ、多ければ中でスクロール
        has = bool(self.entries())
        self.list.setVisible(has)
        self.empty.setVisible(not has)
        pages = sum(e.pages for e in self.entries())
        size = sum(e.size for e in self.entries())
        self.total.setText(f"{len(self.entries())} 件・{pages:,} ページ・{J.fmt_size(size) or '0KB'}" if has else "")
        self._sel_changed()
        self.refresh_run()

    def _set_icon(self, it: QListWidgetItem, e: Entry) -> None:
        size = self.list.iconSize()
        hit, img = self.page.m.row_thumb(e)
        pm = _thumb_pixmap(img, size) if hit and img is not None and not img.isNull() else _placeholder(e.kind, size)
        it.setIcon(QIcon(pm))

    def thumb_ready(self, key: Any) -> None:
        by_id = {e.id: e for e in self.entries()}
        for i in range(self.list.count()):
            it = self.list.item(i)
            e = by_id.get(int(it.data(Qt.ItemDataRole.UserRole)))
            if e is not None and ("row", str(e.path), e.mtime_ns) == key:
                self._set_icon(it, e)

    def _selected_id(self) -> int | None:
        items = self.list.selectedItems() if hasattr(self, "list") else []
        return int(items[0].data(Qt.ItemDataRole.UserRole)) if items else None

    def _selected_index(self) -> int:
        sid = self._selected_id()
        for i, e in enumerate(self.entries()):
            if e.id == sid:
                return i
        return -1

    def _sel_changed(self) -> None:
        i = self._selected_index()
        n = len(self.entries())
        self.btn_up.setEnabled(i > 0)
        self.btn_down.setEnabled(0 <= i < n - 1)
        self.btn_remove.setEnabled(i >= 0)
        self.btn_sort.setEnabled(n > 1)
        self.btn_clear.setEnabled(n > 0)

    def _move(self, d: int) -> None:
        i = self._selected_index()
        if i >= 0:
            self.page.m.move(self.tab, i, d)

    def _remove(self) -> None:
        i = self._selected_index()
        if i >= 0:
            self.page.m.remove(self.tab, i)

    def _dragged(self) -> None:
        ids = [int(self.list.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self.list.count())]
        QTimer.singleShot(0, guard(lambda: self.page.m.reorder(self.tab, ids), f"{self.tab}:reorder"))

    def can_run(self) -> bool:
        return bool(self.entries())

    def refresh_run(self) -> None:
        self.refresh_job()


# ------------------------------------------------------------------ まとめる
class _MergeTab(_ListTab):
    def __init__(self, page: PagePressPage) -> None:
        super().__init__(page, "merge")
        acc = page.accent
        card = W.Card("まとめる", "並べた順に1つの PDF にします。写真は1枚1ページになります。元のファイルは変わりません。", G.ADD, acc)
        self.add_intake(card, _DropZone(acc, "PDF や写真を、ここにドロップ",
                                        "PDF・JPEG・PNG・HEIC。フォルダを落とすと、その中のファイルを名前の順に入れます(中のフォルダは見ません)。",
                                        guard(page.pick, "merge:pick")))
        self.lay.addWidget(card)
        lc = W.Card("並べる順", "ドラッグか「上へ」「下へ」で並べ替えます。同じファイルを2回入れてもかまいません。", G.LIST, acc)
        self.build_list(lc, reorder=True)
        self.lay.addWidget(lc)
        self.img_card = W.Card("写真のページ", "一覧に写真があるときの設定です。変えるとすぐ保存します。", G_PDF, acc)
        c = page.m.config
        self.seg_fit = W.Segmented([("paper", "用紙に合わせる"), ("original", "写真の大きさのまま")], c.image_fit, acc)
        self.seg_paper = W.Segmented([("a4", "A4"), ("letter", "Letter")], c.paper, acc)
        self.seg_margin = W.Segmented([("10", "余白 10mm"), ("0", "余白なし")], str(c.margin_mm), acc)
        self.seg_fit.changed.connect(guard(lambda v: self._opt("image_fit", v), "merge:fit"))
        self.seg_paper.changed.connect(guard(lambda v: self._opt("paper", v), "merge:paper"))
        self.seg_margin.changed.connect(guard(lambda v: self._opt("margin_mm", int(v)), "merge:margin"))
        self.img_card.add(W.SettingRow("ページの大きさ", "用紙に合わせると、縦長の写真は縦向き、横長の写真は横向きの用紙いっぱいに置きます。",
                                       self.seg_fit))
        self.row_paper = W.SettingRow("用紙", None, self.seg_paper)
        self.row_margin = W.SettingRow("余白", None, self.seg_margin)
        self.img_card.add(self.row_paper)
        self.img_card.add(self.row_margin)
        self.lay.addWidget(self.img_card)
        self.add_run_row("まとめる", G.ADD, guard(page.run_merge, "merge:run"))

    def _opt(self, key: str, v: Any) -> None:
        err = self.page.m.set_option(key, v)
        if err:
            W.message(self.page.window(), "PagePress", err, kind="error")
        self._sync_img()

    def _sync_img(self) -> None:
        orig = self.page.m.config.image_fit == "original"
        self.row_paper.setEnabled(not orig)
        self.row_margin.setEnabled(not orig)

    def refresh_run(self) -> None:
        self.img_card.setVisible(self.page.m.has_images())
        self._sync_img()
        pages = self.page.m.total_pages("merge")
        n = len(self.entries())
        if n == 0:
            self.run_hint.setText("ファイルを入れると、ここからまとめられます。")
            self.run_hint.setStyleSheet("")
        elif pages > J.MAX_PAGES:
            self.run_hint.setText(J.MSG_TOO_MANY_PAGES)
            self.run_hint.setStyleSheet(f"color: {T.DANGER}; font-size: 12px;")
        else:
            first = self.entries()[0].path.stem
            self.run_hint.setText(f"{n} 件・{pages:,} ページを「{first}_まとめ.pdf」にします。")
            self.run_hint.setStyleSheet("")
        self.refresh_job()

    def can_run(self) -> bool:
        return bool(self.entries()) and self.page.m.total_pages("merge") <= J.MAX_PAGES


# ------------------------------------------------------------------ 分ける
class _SplitTab(_ListTab):
    def __init__(self, page: PagePressPage) -> None:
        super().__init__(page, "split")
        acc = page.accent
        card = W.Card("分ける", "範囲ごと・1ページずつ・N ページごとに、別の PDF にします。できたファイルは新しいフォルダに入ります。",
                      G.COPY, acc)
        self.add_intake(card, _DropZone(acc, "PDF を、ここにドロップ", "いくつ入れても、同じ分け方で1つずつ分けます。",
                                        guard(page.pick, "split:pick")))
        self.lay.addWidget(card)
        lc = W.Card("分ける PDF", None, G.LIST, acc)
        self.build_list(lc, reorder=False)
        self.lay.addWidget(lc)
        oc = W.Card("分け方", None, G.SETTINGS, acc)
        c = page.m.config
        self.seg_mode = W.Segmented([("ranges", "範囲で分ける"), ("single", "1ページずつ"), ("every", "N ページごと")],
                                    c.split_mode, acc)
        self.seg_mode.changed.connect(guard(self._mode, "split:mode"))
        oc.add(W.SettingRow("分け方", None, self.seg_mode))
        self.range_edit = QLineEdit()
        self.range_edit.setPlaceholderText("例: 1-3, 5, 8-")
        self.range_edit.setMinimumWidth(260)
        self.range_edit.textChanged.connect(guard(lambda _t: self.refresh_run(), "split:range"))
        self.row_range = W.SettingRow("範囲", "カンマで区切った範囲ごとに1つのファイルになります。「8-」は最後まで、「-3」は最初からです。",
                                      self.range_edit)
        oc.add(self.row_range)
        self.range_err = W.label("", "Mute")
        self.range_err.setStyleSheet(f"color: {T.DANGER}; font-size: 12px;")
        oc.add(self.range_err)
        self.spin = QSpinBox()
        self.spin.setRange(1, 999)
        self.spin.setValue(c.split_every)
        self.spin.setSuffix(" ページごと")
        self.spin.valueChanged.connect(guard(lambda v: self._every(int(v)), "split:every"))
        self.row_every = W.SettingRow("N", None, self.spin)
        oc.add(self.row_every)
        self.lay.addWidget(oc)
        self.add_run_row("分ける", G.COPY, guard(page.run_split, "split:run"))
        self._sync_mode()

    def _mode(self, v: str) -> None:
        self.page.m.set_option("split_mode", v)
        self._sync_mode()
        self.refresh_run()

    def _every(self, v: int) -> None:
        self.page.m.set_option("split_every", v)
        self.refresh_run()

    def _sync_mode(self) -> None:
        mode = self.page.m.config.split_mode
        self.row_range.setVisible(mode == "ranges")
        self.range_err.setVisible(mode == "ranges" and bool(self.range_err.text()))
        self.row_every.setVisible(mode == "every")

    def spans(self) -> tuple[list[ranges.Span] | None, str]:
        """(範囲, 誤りの文)。ページ数は一覧のうち最大の PDF で確かめる。"""
        text = self.range_edit.text()
        if not text.strip():
            return None, ""
        pages = max((e.pages for e in self.entries()), default=None)
        try:
            return ranges.parse(text, pages), ""
        except ranges.RangeError as e:
            return None, e.message

    def refresh_run(self) -> None:
        mode = self.page.m.config.split_mode
        spans, err = self.spans() if mode == "ranges" else (None, "")
        self.range_err.setText(err)
        self.range_err.setVisible(bool(err))
        n = self.page.m.split_count(spans)
        if not self.entries():
            self.run_hint.setText("PDF を入れると、ここから分けられます。")
        elif mode == "ranges" and spans is None:
            self.run_hint.setText("範囲を書いてください。" if not err else "範囲を直してください。")
        else:
            self.run_hint.setText(f"{n} 個のファイルができます。")
        self.refresh_job()

    def can_run(self) -> bool:
        if not self.entries():
            return False
        if self.page.m.config.split_mode == "ranges":
            spans, _ = self.spans()
            return spans is not None
        return True


# ------------------------------------------------------------------ 軽くする
class _CompressTab(_ListTab):
    def __init__(self, page: PagePressPage) -> None:
        super().__init__(page, "compress")
        acc = page.accent
        card = W.Card("軽くする", "中の画像を縮めて、ファイルを小さくします。文字や線はそのままです。", G.ARCHIVE, acc)
        self.add_intake(card, _DropZone(acc, "PDF を、ここにドロップ", "1つずつ軽くして、元の名前に「_軽量」を付けて保存します。",
                                        guard(page.pick, "compress:pick")))
        self.lay.addWidget(card)
        lc = W.Card("軽くする PDF", None, G.LIST, acc)
        self.build_list(lc, reorder=False)
        self.lay.addWidget(lc)
        oc = W.Card("どれくらい軽くするか", None, G.SETTINGS, acc)
        self.seg = W.Segmented([(k, v[0]) for k, v in LEVEL_TEXT.items()], page.m.config.compress_level, acc)
        self.seg.changed.connect(guard(self._level, "compress:level"))
        self.level_desc = W.label("", "Mute")
        oc.add(W.SettingRow("段階", "うんと軽くすると、写真や細かい図が粗くなります。", self.seg))
        oc.add(self.level_desc)
        self.lay.addWidget(oc)
        self.add_run_row("軽くする", G.ARCHIVE, guard(page.run_compress, "compress:run"))
        self._desc()

    def _level(self, v: str) -> None:
        self.page.m.set_option("compress_level", v)
        self._desc()

    def _desc(self) -> None:
        lv = self.page.m.config.compress_level
        self.level_desc.setText(f"画像を {LEVEL_TEXT[lv][1]} にします。元より小さくならないときは、ファイルを作りません。")

    def refresh_run(self) -> None:
        n = len(self.entries())
        self.run_hint.setText(f"{n} 件を1つずつ軽くします。" if n else "PDF を入れると、ここから軽くできます。")
        self.refresh_job()


# ------------------------------------------------------------------ ページの整理
class _OrganizeTab(_Tab):
    def __init__(self, page: PagePressPage) -> None:
        super().__init__(page, "organize")
        acc = page.accent
        self.card = W.Card("ページの整理", "並べ替え・回転・ページを抜く。保存すると新しい PDF を作ります(元の PDF は変わりません)。",
                           G.LAYOUT, acc)
        self.zone = _DropZone(acc, "PDF を1つ、ここにドロップ", "開いたページを格子に並べます。", guard(page.pick, "org:pick"),
                              compact=True)
        self.add_intake(self.card, self.zone)
        head = QHBoxLayout()
        self.doc_label = W.label("", "H3")
        self.doc_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        head.addWidget(self.doc_label, 1)
        self.dirty_pill = W.StatusPill("保存していない変更", "warn")
        head.addWidget(self.dirty_pill)
        self.btn_other = W.button("別の PDF を開く", "ghost", G.FOLDER, on_click=guard(page.pick, "org:other"))
        self.btn_close = W.button("閉じる", "ghost", G.CLOSE, on_click=guard(self._close, "org:close"))
        head.addWidget(self.btn_other)
        head.addWidget(self.btn_close)
        self.card.add_layout(head)
        tools = QHBoxLayout()
        tools.setSpacing(6)
        self.b_left = W.button("左に回す", "secondary", G_ROTATE_L, on_click=guard(lambda: self._rotate(270), "org:left"))
        self.b_right = W.button("右に回す", "secondary", G_ROTATE_R, on_click=guard(lambda: self._rotate(90), "org:right"))
        self.b_del = W.button("抜く", "danger", G.DELETE, on_click=guard(self._delete, "org:del"), tooltip="Delete キーでも抜けます")
        self.b_undo = W.icon_button(G.UNDO, "取り消す(Ctrl+Z)", guard(self._undo, "org:undo"))
        self.b_redo = W.icon_button(G_REDO, "やり直す(Ctrl+Y)", guard(self._redo, "org:redo"))
        for b in (self.b_left, self.b_right, self.b_del):
            tools.addWidget(b)
        tools.addSpacing(8)
        tools.addWidget(self.b_undo)
        tools.addWidget(self.b_redo)
        tools.addStretch(1)
        self.sel_label = W.label("", "Mute")
        tools.addWidget(self.sel_label)
        self.card.add_layout(tools)
        self.model = PageModel()
        self.view = OrganizeView()
        self.view.setModel(self.model)
        self.view.setItemDelegate(PageDelegate(page.m.page_thumb, lambda: page.accent, self.view))
        self.view.setMinimumHeight(520)
        self.view.window_changed.connect(guard(lambda f, last: page.m.request_pages(f, last), "org:window"))
        self.view.moved.connect(guard(self._moved, "org:moved"))
        self.view.delete_pressed.connect(guard(self._delete, "org:delkey"))
        self.view.undo_pressed.connect(guard(self._undo, "org:undokey"))
        self.view.redo_pressed.connect(guard(self._redo, "org:redokey"))
        self.view.selectionModel().selectionChanged.connect(guard(lambda *_a: self._sync(), "org:sel"))
        self.card.add(self.view)
        self.hint = W.label("ドラッグで並べ替え。クリック・Ctrl・Shift で複数を選べます。Delete で抜く、Ctrl+Z で取り消し、Ctrl+Y でやり直し。",
                            "Mute", wrap=True)
        self.card.add(self.hint)
        self.lay.addWidget(self.card)
        self.add_run_row("保存", G.SAVE, guard(page.run_organize, "org:save"))
        self._repaint = QTimer(self)
        self._repaint.setSingleShot(True)
        self._repaint.setInterval(40)
        self._repaint.timeout.connect(guard(lambda: self.view.viewport().update(), "org:repaint"))
        self.reload()

    def reload(self) -> None:
        m = self.page.m
        self.model.set_state(m.organize_state)
        e = m.organize_entry
        opened = e is not None
        self.zone.setVisible(not opened)
        for w in (self.doc_label, self.btn_other, self.btn_close, self.view, self.hint, self.b_left, self.b_right, self.b_del,
                  self.b_undo, self.b_redo, self.sel_label):
            w.setVisible(opened)
        if e is not None:
            self.doc_label.setText(f"{e.name}(元は {e.pages:,} ページ)")
            self.doc_label.setToolTip(str(e.path))
            self.view.scrollToTop()
            self.view.schedule()
        self._sync()

    def page_thumb(self, _orig: int) -> None:
        if not self._repaint.isActive():
            self._repaint.start()

    def _rows(self) -> list[int]:
        return sorted({i.row() for i in self.view.selectionModel().selectedIndexes()})

    def _select(self, rows: list[int]) -> None:
        from PySide6.QtCore import QItemSelection, QItemSelectionModel

        sm = self.view.selectionModel()
        sel = QItemSelection()
        for r in rows:
            idx = self.model.index(r, 0)
            if idx.isValid():
                sel.select(idx, idx)
        sm.select(sel, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        if rows:
            self.view.scrollTo(self.model.index(rows[0], 0))

    def _after(self, rows: list[int] | None = None) -> None:
        keep = self._rows() if rows is None else rows
        self.model.refresh()
        n = self.model.rowCount()
        self._select([r for r in keep if r < n])
        self.view.schedule()
        self._sync()

    def _rotate(self, delta: int) -> None:
        st = self.page.m.organize_state
        rows = self._rows()
        if st is None or not rows:
            return
        st.rotate(rows, delta)
        self.model.dataChanged.emit(self.model.index(rows[0], 0), self.model.index(rows[-1], 0))
        self._sync()

    def _delete(self) -> None:
        st = self.page.m.organize_state
        rows = self._rows()
        if st is None or not rows:
            return
        st.remove(rows)
        self._after([min(rows[0], max(0, len(st.items) - 1))] if st.items else [])

    def _moved(self, rows: list[int], dest: int) -> None:
        st = self.page.m.organize_state
        if st is None:
            return
        new_rows = st.move(rows, dest)
        self._after(new_rows)

    def _undo(self) -> None:
        st = self.page.m.organize_state
        if st is not None and st.undo():
            self._after([])

    def _redo(self) -> None:
        st = self.page.m.organize_state
        if st is not None and st.redo():
            self._after([])

    def _close(self) -> None:
        if self.page.confirm_discard():
            self.page.m.close_organize()

    def _sync(self) -> None:
        m = self.page.m
        st = m.organize_state
        rows = self._rows() if st is not None else []
        for b in (self.b_left, self.b_right, self.b_del):
            b.setEnabled(bool(rows))
        self.b_undo.setEnabled(st is not None and st.can_undo())
        self.b_redo.setEnabled(st is not None and st.can_redo())
        n = len(st.items) if st is not None else 0
        self.sel_label.setText(f"{len(rows)} ページを選択中・全部で {n:,} ページ" if rows else f"全部で {n:,} ページ")
        self.dirty_pill.setVisible(m.organize_dirty())
        if st is None:
            self.run_hint.setText("PDF を開くと、ここから保存できます。")
        elif n == 0:
            self.run_hint.setText("ページが1つもないため保存できません。")
        else:
            self.run_hint.setText(f"今の順番と向きで「{m.organize_entry.path.stem if m.organize_entry else ''}_整理.pdf」を作ります。")
        self.refresh_job()

    def can_run(self) -> bool:
        st = self.page.m.organize_state
        return st is not None and bool(st.items)


# ------------------------------------------------------------------ ページ本体
class PagePressPage(W.ScrollPage):
    def __init__(self, module: PagePressModule) -> None:
        super().__init__()
        self.m = module
        self.accent = module.accent
        self.job_names: list[str] = []
        self.result_names: dict[int, str] = {}
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self._build_hero()
        self._build_tabs()
        self._build_settings()
        self.finish()
        s = module.signals
        s.lists_changed.connect(guard(self._lists_changed, "page:lists"))
        s.rejected_changed.connect(guard(self._notes_changed, "page:notes"))
        s.probing_changed.connect(guard(self._notes_changed, "page:probing"))
        s.job_changed.connect(guard(self._job_changed, "page:job"))
        s.organize_changed.connect(guard(self._organize_changed, "page:org"))
        s.row_thumb.connect(guard(self._row_thumb, "page:rowthumb"))
        s.page_thumb.connect(guard(self.t_organize.page_thumb, "page:pagethumb"))
        for t in self.tabs.values():
            t.refresh_notes()
            if isinstance(t, _ListTab):
                t.rebuild()
        self._job_changed()
        self.switch(module.config.last_tab, save=False)

    # ================================================================ 骨組み
    def _build_hero(self) -> None:
        from deskkit.catalog import info

        hero = W.Hero("PagePress", "PDF と写真を、ネットに上げずに PC の中でまとめる・分ける・並べ替える・軽くします。元のファイルは変えません。",
                      info("pagepress").glyph or G_PDF, self.accent)
        hero.add_pill(W.StatusPill("PC の中だけで処理", "ok"))
        self.state_pill = W.StatusPill("待機中", "off")
        hero.add_pill(self.state_pill)
        hero.add_action(W.button("ファイルを選ぶ", "primary", G.FOLDER, on_click=guard(self.pick, "hero:pick")))
        self.add(hero)

    def _build_tabs(self) -> None:
        self.seg = W.Segmented(TAB_OPTIONS, self.m.config.last_tab, self.accent)
        self.seg.changed.connect(guard(self._seg_changed, "page:tab"))
        bar = QHBoxLayout()
        bar.addWidget(self.seg)
        bar.addStretch(1)
        holder = QWidget()
        holder.setLayout(bar)
        bar.setContentsMargins(0, 0, 0, 0)
        self.add(holder)
        # QStackedWidget は隠れたタブの折り返しの高さまで最小の高さに入れてカードが縦に伸びるので、縦に並べて今のタブだけを見せる
        self.stack = QWidget()
        stack_lay = QVBoxLayout(self.stack)
        stack_lay.setContentsMargins(0, 0, 0, 0)
        self.t_merge = _MergeTab(self)
        self.t_split = _SplitTab(self)
        self.t_organize = _OrganizeTab(self)
        self.t_compress = _CompressTab(self)
        self.tabs: dict[str, _Tab] = {"merge": self.t_merge, "split": self.t_split, "organize": self.t_organize,
                                      "compress": self.t_compress}
        for t in self.tabs.values():
            stack_lay.addWidget(t)
            t.setVisible(False)
        self.add(self.stack)
        self.current = self.m.config.last_tab

    def _build_settings(self) -> None:
        card = W.Card("エクスプローラーから使う", None, G.LINK, self.accent)
        self.sendto_toggle = W.ToggleSwitch(self.m.config.sendto_enabled, self.accent)
        self.sendto_toggle.toggled.connect(guard(self._set_sendto, "opt:sendto"))
        card.add(W.SettingRow("「送る」に「PagePress でまとめる」を追加",
                              "ファイルを右クリック →「送る」から、まとめる一覧に入れられます。オフにすると、DeskKit が作ったショートカットだけを消します。",
                              self.sendto_toggle))
        self.sendto_note = W.label("", "Mute", wrap=True)
        card.add(self.sendto_note)
        self.add(card)
        self._refresh_sendto()

    def _set_sendto(self, v: bool) -> None:
        err = self.m.set_sendto(bool(v))
        if err:
            self.sendto_toggle.set_checked_silent(not v)
            W.message(self.window(), "「送る」", err, kind="warn")
        self._refresh_sendto()

    def _refresh_sendto(self) -> None:
        st = self.m.sendto_status()
        text = {sendto_mod.STATUS_REGISTERED: "「送る」に登録されています。",
                sendto_mod.STATUS_OTHER: "「送る」に同じ名前の別のショートカットがあります(DeskKit は触りません)。"}.get(st, "")
        self.sendto_note.setText(text)
        self.sendto_note.setVisible(bool(text))

    # ================================================================ タブ(FR-9 の確認)
    def confirm_discard(self) -> bool:
        if not self.m.organize_dirty():
            return True
        ok, _ = W.confirm(self.window(), "保存していない変更", "保存していない変更があります。捨てますか", ok_text="捨てる",
                          cancel_text="戻る", danger=True)
        return ok

    def _seg_changed(self, v: str) -> None:
        if self.current == "organize" and v != "organize" and not self.confirm_discard():
            self.seg.set_value("organize")
            return
        self.switch(v)

    def switch(self, tab: str, save: bool = True) -> None:
        if tab not in self.tabs:
            tab = "merge"
        self.current = tab
        self.seg.set_value(tab)
        cur = self.tabs[tab]
        for w in self.tabs.values():
            if w is not cur:
                w.setVisible(False)
        if not cur.isVisible():
            cur.setVisible(True)
            self._fade_in(cur)
        if save:
            self.m.set_option("last_tab", tab)
        if tab == "organize":
            self.t_organize.view.schedule()

    def _fade_in(self, w: QWidget) -> None:
        """タブを替えたときのフェードイン(FadeStack と同じ 220ms)。"""
        from PySide6.QtCore import QEasingCurve, QPropertyAnimation
        from PySide6.QtWidgets import QGraphicsOpacityEffect

        eff = QGraphicsOpacityEffect(w)
        w.setGraphicsEffect(eff)
        a = QPropertyAnimation(eff, b"opacity", w)
        a.setDuration(220)
        a.setStartValue(0.0)
        a.setEndValue(1.0)
        a.setEasingCurve(QEasingCurve.Type.OutCubic)
        a.finished.connect(lambda: w.setGraphicsEffect(None))  # type: ignore[arg-type]
        a.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    # ================================================================ 投入(FR-1)
    def pick(self) -> None:
        tab = self.current
        if tab == "organize":
            f, _ = QFileDialog.getOpenFileName(self.window(), "PDF を開く", "", FILTER_PDF)
            if f:
                self.add_to(tab, [f])
            return
        files, _f = QFileDialog.getOpenFileNames(self.window(), "ファイルを選ぶ", "", FILTER_ALL if tab == "merge" else FILTER_PDF)
        if files:
            self.add_to(tab, files)

    def add_to(self, tab: str, paths: list[str]) -> None:
        if tab == "organize" and self.m.organize_entry is not None and not self.confirm_discard():
            return
        self.m.add_paths(tab, paths)

    def dragEnterEvent(self, e: QDragEnterEvent) -> None:  # noqa: N802
        try:
            if _local_files(e.mimeData()):
                e.acceptProposedAction()
                return
        except Exception:  # noqa: BLE001
            pass
        e.ignore()

    def dragMoveEvent(self, e: QDragMoveEvent) -> None:  # noqa: N802
        md = e.mimeData()
        if md is not None and md.hasUrls():
            e.acceptProposedAction()
        else:
            e.ignore()

    def dropEvent(self, e: QDropEvent) -> None:  # noqa: N802
        try:
            files = _local_files(e.mimeData())
            if files:
                e.acceptProposedAction()
                self.add_to(self.current, files)
        except Exception as ex:  # noqa: BLE001
            _log.error("drop failed: %s", type(ex).__name__)

    # ================================================================ 実行(FR-4・FR-5・FR-9・FR-10)
    def _confirm_p6(self, tab: str) -> bool:
        msgs = self.m.warnings(tab)
        if not msgs:
            return True
        ok, _ = W.confirm(self.window(), "書く前の確認", "\n\n".join(msgs), ok_text="続ける", cancel_text="やめる")
        return ok

    def _start(self, tab: str, **kw: Any) -> None:
        if not self._confirm_p6(tab):
            return
        if tab == "merge":
            self.job_names = [e.name for e in self.m.lists["merge"]]
        self.result_names = {e.id: e.name for e in self.m.lists.get(tab, [])} if tab != "organize" else {}
        err = self.m.start_job(tab, **kw)
        if err:
            W.message(self.window(), "PagePress", err, kind="warn")

    def run_merge(self) -> None:
        self._start("merge")

    def run_split(self) -> None:
        spans, err = self.t_split.spans() if self.m.config.split_mode == "ranges" else (None, "")
        if err:
            return
        self._start("split", spans=spans)

    def run_organize(self) -> None:
        self._start("organize")

    def run_compress(self) -> None:
        self._start("compress")

    # ================================================================ 変化の反映
    def _lists_changed(self, tab: str) -> None:
        t = self.tabs.get(tab)
        if isinstance(t, _ListTab):
            t.rebuild()

    def _notes_changed(self, tab: str) -> None:
        t = self.tabs.get(tab)
        if t is not None:
            t.refresh_notes()

    def _row_thumb(self, key: Any) -> None:
        for t in (self.t_merge, self.t_split, self.t_compress):
            t.thumb_ready(key)

    def _organize_changed(self) -> None:
        self.t_organize.reload()
        if self.m.organize_entry is not None and self.current != "organize":
            self.switch("organize")

    def _job_changed(self) -> None:
        busy = self.m.busy()
        self.state_pill.set_state("accent" if busy else "off", "作業中" if busy else "待機中")
        for t in self.tabs.values():
            t.refresh_job()
        if isinstance(self.tabs.get(self.m.job_tab), _OrganizeTab):
            self.t_organize._sync()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(980, 900)
