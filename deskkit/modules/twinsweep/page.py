# Control Center の TwinSweep 画面。フォルダの選択(ドロップ・追加)・設定・進捗と中止・お知らせ・グループの一覧(段階的に描く)・
# 写真ごとの「残す」「ごみ箱へ」・大きなプレビュー・画面の下に常に出す「ごみ箱へ」の帯。画面にはパスを出してよい(V-8)が、
# ログにはパスを書かない(例外は型名だけを書く。INV-4)。色は T.* と catalog のアクセント色を実行時に読む。
from __future__ import annotations

import functools
import logging
import os
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypeVar

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QListWidget,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from deskkit.catalog import info
from deskkit.modules.twinsweep.module import (
    GROUPING,
    MSG_FORBIDDEN,
    MSG_RESTORE,
    RECYCLING,
    SCANNING,
    Notice,
    human_bytes,
)
from deskkit.modules.twinsweep.results import KIND_EXACT, Group
from deskkit.modules.twinsweep.thumbs import THUMB_SIDE
from deskkit.ui import theme as T
from deskkit.ui import widgets as W
from deskkit.ui.theme import G

if TYPE_CHECKING:
    from deskkit.modules.twinsweep.grouping import Photo
    from deskkit.modules.twinsweep.module import TwinSweepModule

_log = logging.getLogger("deskkit.twinsweep.page")
F = TypeVar("F", bound=Callable[..., Any])

GROUP_BATCH = 50         # 一度に描き足すグループの数
RENDER_CHUNK = 10        # 1回のイベントループで描くグループの数(固まらないように)
TILES_PER_GROUP = 30     # 1グループで最初に並べる写真の数(残りは「残り N 枚を表示」)
TILE_W = 184
STAGE_TEXT = {"count": "写真を数えています", "features": "特徴を調べています", "group": "グループにまとめています"}


def guard(fn: F) -> F:
    """シグナル・仮想メソッドから呼ぶ処理の例外を握る。ログには型名だけを書く(例外の文にパスが入り得るため)。"""

    @functools.wraps(fn)
    def wrapper(*a: Any, **k: Any) -> Any:
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001 - Qt へ例外を漏らさない
            _log.error("handler %s failed: %s", getattr(fn, "__name__", "?"), type(e).__name__)
            return None

    return wrapper  # type: ignore[return-value]


def _date_text(p: Photo) -> str:
    if p.taken_at:
        try:
            return datetime.fromisoformat(p.taken_at).strftime("%Y/%m/%d %H:%M")
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(p.mtime_ns / 1e9).strftime("%Y/%m/%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "—"


def rel_folder(p: Photo) -> str:
    """調べたフォルダからの相対パス(先頭に調べたフォルダの名前を付ける)。"""
    root = p.root.rstrip("\\/")
    base = os.path.basename(root) or root
    try:
        rel = os.path.relpath(os.path.dirname(p.path), p.root)
    except ValueError:
        return os.path.dirname(p.path)
    return base if rel in (".", "") else os.path.join(base, rel)


# ------------------------------------------------------------------ 折り返して並べるレイアウト
class FlowLayout(QLayout):
    def __init__(self, parent: QWidget | None = None, spacing: int = 10) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i: int) -> QLayoutItem | None:  # noqa: N802
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i: int) -> QLayoutItem | None:  # noqa: N802
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        return s

    def _do_layout(self, rect: QRect, test: bool) -> int:
        x, y, line_h = rect.x(), rect.y(), 0
        for it in self._items:
            hint = it.sizeHint()
            nx = x + hint.width() + self._spacing
            if nx - self._spacing > rect.right() + 1 and line_h > 0:
                x = rect.x()
                y = y + line_h + self._spacing
                nx = x + hint.width() + self._spacing
                line_h = 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x = nx
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()


# ------------------------------------------------------------------ 色付きの注意書き
class _Note(QFrame):
    def __init__(self, n: Notice) -> None:
        super().__init__()
        color = {"ok": T.SUCCESS, "warn": T.WARN, "error": T.DANGER}.get(n.kind, T.INFO)
        glyph = {"ok": G.CHECK, "warn": G.WARNING, "error": G.ERROR}.get(n.kind, G.INFO)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 9)
        lay.setSpacing(10)
        g = W.Glyph(glyph, 15, color)
        g.setStyleSheet(f"color: {color}; background: transparent; border: none;")
        lay.addWidget(g, 0, Qt.AlignmentFlag.AlignTop)
        lb = W.label(n.text, None, wrap=True)
        lb.setStyleSheet(f"color: {T.TEXT}; background: transparent; border: none;")
        lay.addWidget(lb, 1)
        self.text = n.text
        self.setObjectName("TsNote")
        self.setStyleSheet(f"#TsNote {{ background: {T.alpha(color, 0.09)}; border: 1px solid {T.alpha(color, 0.32)}; border-radius: 10px; }}")


# ------------------------------------------------------------------ 「残す」「ごみ箱へ」の切り替え
class KeepToggle(QFrame):
    def __init__(self, on_change: Callable[[bool], None]) -> None:
        super().__init__()
        self.setObjectName("Inset")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        self.keep_btn = QPushButton("残す")
        self.trash_btn = QPushButton("ごみ箱へ")
        for b in (self.keep_btn, self.trash_btn):
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            lay.addWidget(b, 1)
        self.keep_btn.clicked.connect(guard(lambda _=False: on_change(True)))
        self.trash_btn.clicked.connect(guard(lambda _=False: on_change(False)))
        self._style()

    def _style(self) -> None:
        base = "QPushButton { border: none; border-radius: 7px; padding: 4px 6px; font-weight: 600; font-size: 12px;"
        self.keep_btn.setStyleSheet(
            f"{base} background: transparent; color: {T.TEXT_DIM}; }}"
            f"QPushButton:checked {{ background: {T.alpha(T.SUCCESS, 0.20)}; color: {T.SUCCESS}; }}"
            f"QPushButton:hover:!checked {{ background: {T.SURFACE3}; }}"
            f"QPushButton:disabled {{ color: {T.TEXT_MUTE}; }}")
        self.trash_btn.setStyleSheet(
            f"{base} background: transparent; color: {T.TEXT_DIM}; }}"
            f"QPushButton:checked {{ background: {T.alpha(T.DANGER, 0.18)}; color: {T.DANGER}; }}"
            f"QPushButton:hover:!checked {{ background: {T.SURFACE3}; }}"
            f"QPushButton:disabled {{ color: {T.TEXT_MUTE}; }}")

    def set_state(self, keep: bool, can_trash: bool, enabled: bool) -> None:
        self.keep_btn.setChecked(keep)
        self.trash_btn.setChecked(not keep)
        self.keep_btn.setEnabled(enabled)
        # G-5: グループの最後の「残す」は「ごみ箱へ」を押せない
        self.trash_btn.setEnabled(enabled and (not keep or can_trash))
        self.trash_btn.setToolTip("" if (not keep or can_trash) else "グループに「残す」写真が1枚は必要です")


# ------------------------------------------------------------------ 写真1枚
class PhotoTile(QFrame):
    def __init__(self, page: TwinSweepPage, photo: Photo, group: Group) -> None:
        super().__init__()
        self.page = page
        self.photo = photo
        self.setObjectName("Tile")
        self.setFixedWidth(TILE_W)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(5)
        self.thumb = QLabel()
        self.thumb.setFixedSize(TILE_W - 16, THUMB_SIDE)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setStyleSheet(f"background: {T.SURFACE3}; border-radius: 8px; color: {T.TEXT_MUTE}; font-size: 11px;")
        self.thumb.setText("読み込み中…")
        self._fx = QGraphicsOpacityEffect(self.thumb)
        self._fx.setOpacity(1.0)
        self.thumb.setGraphicsEffect(self._fx)
        lay.addWidget(self.thumb)
        recommended = photo.pid == group.recommended
        if recommended:
            badge = QLabel(f"{G.SPARKLE}  おすすめ")
            f = T.ui_font(11)
            f.setFamilies([*T.UI_FAMILIES[:2], T.icon_family(), *T.UI_FAMILIES[2:]])
            badge.setFont(f)
            accent = page.accent
            badge.setStyleSheet(f"color: {accent}; background: {T.alpha(accent, 0.14)}; border: 1px solid {T.alpha(accent, 0.4)};"
                                " border-radius: 9px; padding: 1px 8px; font-weight: 700;")
            badge.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            lay.addWidget(badge, 0, Qt.AlignmentFlag.AlignLeft)
            why = W.label(group.reason, "Mute", wrap=True)
            lay.addWidget(why)
        res = W.label(f"{photo.width:,} × {photo.height:,}", None)
        res.setStyleSheet("font-weight: 600;")
        lay.addWidget(res)
        lay.addWidget(W.label(f"{human_bytes(photo.size)} · {_date_text(photo)}", "Dim"))
        folder = rel_folder(photo)
        fl = W.label("", "Mute")
        fl.setText(QFontMetrics(fl.font()).elidedText(folder, Qt.TextElideMode.ElideMiddle, TILE_W - 18))
        fl.setToolTip(photo.path)
        lay.addWidget(fl)
        self.toggle = KeepToggle(self._on_toggle)
        lay.addWidget(self.toggle)
        self.setToolTip(photo.path)
        self.refresh()
        page.m.thumbs().request(photo.path, photo.mtime_ns, THUMB_SIDE, self.set_thumb)

    def set_thumb(self, pm: Any) -> None:
        if pm is None:
            self.thumb.setText("表示できません")
            return
        self.thumb.setText("")
        self.thumb.setPixmap(pm)

    def _on_toggle(self, keep: bool) -> None:
        if not self.page.m.set_keep(self.photo.pid, keep):
            self.refresh()  # 切り替わらなかった(G-5)ので見た目を戻す

    def refresh(self) -> None:
        m = self.page.m
        keep = m.model.is_keep(self.photo.pid) if m.model is not None else True
        idle = not m.busy
        self.toggle.set_state(keep, m.can_trash(self.photo.pid) if keep else True, idle)
        color = T.SUCCESS if keep else T.DANGER
        self.setStyleSheet(f"#Tile {{ background: {T.SURFACE2}; border: 1px solid {T.alpha(color, 0.55 if keep else 0.45)};"
                           " border-radius: 12px; }}")
        self._fx.setOpacity(1.0 if keep else 0.5)

    def mouseDoubleClickEvent(self, e: Any) -> None:  # noqa: N802
        guard(self.page.open_preview)(self.photo)


# ------------------------------------------------------------------ グループ1つ
class GroupCard(QFrame):
    def __init__(self, page: TwinSweepPage, group: Group, index: int) -> None:
        super().__init__()
        self.page = page
        self.group = group
        self.setObjectName("Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 16)
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(10)
        kind = "まったく同じ" if group.kind == KIND_EXACT else "似ている"
        self.title = W.label(f"{kind} · {len(group.photos)} 枚", "H3")
        head.addWidget(self.title)
        head.addStretch(1)
        self.reduce = W.label("", "Dim")
        head.addWidget(self.reduce)
        lay.addLayout(head)
        holder = QWidget()
        self.flow = FlowLayout(holder, 10)
        lay.addWidget(holder)
        self.tiles: list[PhotoTile] = []
        self._more: QPushButton | None = None
        self._add_tiles(TILES_PER_GROUP)
        self.refresh()

    def _add_tiles(self, n: int) -> None:
        start = len(self.tiles)
        for p in self.group.photos[start:start + n]:
            t = PhotoTile(self.page, p, self.group)
            self.flow.addWidget(t)
            self.tiles.append(t)
        rest = len(self.group.photos) - len(self.tiles)
        if rest > 0:
            if self._more is None:
                self._more = W.button("", "ghost", G.DOWN, on_click=guard(lambda: self._add_tiles(TILES_PER_GROUP)))
                lay = self.layout()
                assert isinstance(lay, QVBoxLayout)
                lay.addWidget(self._more, 0, Qt.AlignmentFlag.AlignLeft)
            self._more.setText(f"{G.DOWN}  残り {rest:,} 枚を表示")
        elif self._more is not None:
            self._more.hide()

    def refresh(self) -> None:
        m = self.page.m
        if m.model is not None:
            self.reduce.setText(f"減らせる大きさ {human_bytes(m.model.reducible(self.group))}")
        for t in self.tiles:
            t.refresh()


# ------------------------------------------------------------------ 大きなプレビュー(FR-11)
class PreviewDialog(W.StyledDialog):
    def __init__(self, page: TwinSweepPage, photo: Photo) -> None:
        scr = page.screen().availableGeometry() if page.screen() is not None else QRect(0, 0, 1280, 800)
        max_w, max_h = int(scr.width() * 0.8), int(scr.height() * 0.8)
        box_w, box_h = max(320, max_w - 100), max(240, max_h - 210)
        super().__init__(page.window(), os.path.basename(photo.path), G.EYE, page.accent, width=min(box_w + 44, max_w))
        self.page = page
        self.photo = photo
        self.setMaximumSize(max_w, max_h)
        self.image = QLabel("読み込み中…")
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumSize(min(box_w, 480), min(box_h, 320))
        self.image.setMaximumSize(box_w, box_h)
        self.image.setStyleSheet(f"background: {T.SURFACE2}; border-radius: 10px; color: {T.TEXT_MUTE};")
        self._box = QSize(box_w, box_h)
        self.body.addWidget(self.image, 1)
        info_l = W.label(f"{photo.width:,} × {photo.height:,} · {human_bytes(photo.size)} · {_date_text(photo)} · {rel_folder(photo)}",
                         "Dim", wrap=True)
        self.body.addWidget(info_l)
        self.toggle = KeepToggle(self._on_toggle)
        self.toggle.setFixedWidth(220)
        self.buttons.insertWidget(0, self.toggle)
        self.buttons.addWidget(W.button("閉じる", "primary", on_click=self.accept))
        self._sync()
        page.m.signals.selection.connect(self._sync)
        page.m.thumbs().load_once(photo.path, max(box_w, box_h), self._set_image)

    def _set_image(self, pm: Any) -> None:
        if pm is None:
            self.image.setText("この写真は表示できません")
            return
        self.image.setText("")
        self.image.setPixmap(pm.scaled(self._box, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    @guard
    def _on_toggle(self, keep: bool) -> None:
        self.page.m.set_keep(self.photo.pid, keep)
        self._sync()

    @guard
    def _sync(self) -> None:
        m = self.page.m
        if m.model is None or self.photo.pid not in m.model.keep:
            self.toggle.set_state(True, False, False)
            return
        keep = m.model.is_keep(self.photo.pid)
        self.toggle.set_state(keep, m.can_trash(self.photo.pid) if keep else True, not m.busy)


# ------------------------------------------------------------------ 画面
class TwinSweepPage(QWidget):
    def __init__(self, module: TwinSweepModule) -> None:
        super().__init__()
        self.m = module
        self.accent = info("twinsweep").accent
        self.setAcceptDrops(True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.sp = W.ScrollPage()
        outer.addWidget(self.sp, 1)
        self._cards: list[GroupCard] = []
        self._pending: list[tuple[str, Any]] = []   # 描くのを待っている見出し・グループ
        self._target = 0                            # ここまでのグループを描く
        self._drawn_groups = 0
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(0)
        self._render_timer.timeout.connect(self._render_step)
        self._build_hero()
        self._build_tiles()
        self._build_folders()
        self._build_progress()
        self.notes_box = QVBoxLayout()
        self.notes_box.setSpacing(8)
        self.notes_box.setContentsMargins(0, 0, 0, 0)
        self.notes_holder = QWidget()
        self.notes_holder.setLayout(self.notes_box)
        self.sp.add(self.notes_holder)
        self.results_holder = QWidget()
        self.results_box = QVBoxLayout(self.results_holder)
        self.results_box.setContentsMargins(0, 0, 0, 0)
        self.results_box.setSpacing(12)
        self.sp.add(self.results_holder)
        self.more_btn = W.button("さらに表示", "secondary", G.DOWN, on_click=guard(self._load_more))
        self.more_btn.hide()
        self.sp.add(self.more_btn)
        self.sp.finish()
        self._build_bar(outer)
        self.sp.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        sig = module.signals
        sig.state.connect(self._on_state)
        sig.results.connect(self._on_results)
        sig.selection.connect(self._on_selection)
        sig.notices.connect(self._on_notices)
        sig.settings.connect(self._on_settings)
        self._on_settings()
        self._on_notices()
        self._on_results()
        self._on_state()

    # ================================================================ 部品
    def _build_hero(self) -> None:
        cat = info("twinsweep")
        hero = W.Hero("TwinSweep", "フォルダの中のそっくりな写真をまとめて、いちばん良い1枚をおすすめします。"
                      "残りは確認してからごみ箱へ送るので、あとから元に戻せます。", cat.glyph, self.accent)
        self.state_pill = W.StatusPill("待機中", "off")
        hero.add_pill(self.state_pill)
        self.saved_pill = W.StatusPill("", "ok")
        self.saved_pill.hide()
        hero.add_pill(self.saved_pill)
        self._saved_timer = QTimer(self)
        self._saved_timer.setSingleShot(True)
        self._saved_timer.setInterval(1800)
        self._saved_timer.timeout.connect(self.saved_pill.hide)
        self.scan_btn = W.button("調べる", "primary", G.SEARCH, on_click=guard(self._scan))
        hero.add_action(self.scan_btn)
        self.sp.add(hero)

    def _build_tiles(self) -> None:
        box = QWidget()
        grid = QHBoxLayout(box)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(12)
        self.t_files = W.StatTile("調べた写真", "—", G.LIST, self.accent)
        self.t_groups = W.StatTile("グループ", "—", G.LAYOUT, T.ACCENT)
        self.t_sel = W.StatTile("ごみ箱へ送る写真", "0", G.DELETE, T.DANGER)
        self.t_bytes = W.StatTile("減らせる大きさ", "0 B", G.ARCHIVE, T.SUCCESS)
        for t in (self.t_files, self.t_groups, self.t_sel, self.t_bytes):
            t.setMinimumWidth(0)
            grid.addWidget(t, 1)
        self.sp.add(box)

    def _build_folders(self) -> None:
        card = W.Card("調べるフォルダ", "ここにフォルダをドロップするか、「フォルダを追加」を押します(10 個まで)。", G.FOLDER, self.accent)
        self.folder_card = card
        self.folder_list = QListWidget()
        self.folder_list.setFixedHeight(118)
        self.folder_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        card.add(self.folder_list)
        self.folder_empty = W.label("まだフォルダがありません。写真のフォルダをここへドラッグしてください。", "Mute", wrap=True)
        card.add(self.folder_empty)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.add_btn = W.button("フォルダを追加", "secondary", G.ADD, on_click=guard(self._pick_folder))
        self.rm_btn = W.button("選んだフォルダを外す", "ghost", G.DELETE, on_click=guard(self._remove_folders))
        row.addWidget(self.add_btn)
        row.addWidget(self.rm_btn)
        row.addStretch(1)
        card.add_layout(row)
        card.add(W.divider())
        self.rec_toggle = W.ToggleSwitch(bool(self.m.config["recursive"]), self.accent)
        self.rec_toggle.toggled.connect(guard(self._on_recursive))
        card.add(W.SettingRow("サブフォルダも調べる", "選んだフォルダの中のフォルダも調べます。", self.rec_toggle))
        self.level_seg = W.Segmented([("strict", "厳しめ"), ("normal", "ふつう"), ("loose", "ゆるめ")],
                                     str(self.m.config["level"]), self.accent)
        self.level_seg.changed.connect(guard(self._on_level))
        card.add(W.SettingRow("似ている度合い", "「ゆるめ」ほど、少し違う写真も同じグループに入ります。変えてもすぐに並べ直します。",
                              self.level_seg))
        self.mode_seg = W.Segmented([("similar", "似ている写真も"), ("exact", "まったく同じ写真だけ")],
                                    "exact" if self.m.config["exact_only"] else "similar", self.accent)
        self.mode_seg.changed.connect(guard(self._on_mode))
        card.add(W.SettingRow("探す写真", None, self.mode_seg))
        card.add(W.divider())
        self.cache_btn = W.button("キャッシュを消す", "ghost", G.CLEAR, on_click=guard(self._clear_cache))
        card.add(W.SettingRow("キャッシュ", "2 回目から速く調べるための記録です(写真の場所を含みます)。30 日使わなかった分は自動で消えます。",
                              self.cache_btn))
        self.sp.add(card)

    def _build_progress(self) -> None:
        card = W.Card("調べています", None, G.CLOCK, self.accent)
        self.progress_card = card
        self.stage_label = W.label("", "Dim", wrap=True)
        card.add(self.stage_label)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.bar.setStyleSheet(f"QProgressBar::chunk {{ background: {self.accent}; border-radius: 4px; }}")
        card.add(self.bar)
        row = QHBoxLayout()
        row.addStretch(1)
        self.cancel_btn = W.button("中止", "secondary", G.CLOSE, on_click=guard(self.m.cancel_scan))
        row.addWidget(self.cancel_btn)
        card.add_layout(row)
        card.hide()
        self.sp.add(card)

    def _build_bar(self, outer: QVBoxLayout) -> None:
        bar = QFrame()
        bar.setObjectName("TsBar")
        bar.setStyleSheet(f"#TsBar {{ background: {T.SURFACE}; border-top: 1px solid {T.BORDER}; }}")
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(32, 10, 32, 12)
        lay.setSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.open_bin_btn = W.button("ごみ箱を開く", "ghost", G.OPEN, on_click=guard(self.m.open_recycle_bin))
        row.addWidget(self.open_bin_btn)
        row.addStretch(1)
        self.recycle_btn = W.button("選んだ 0 枚をごみ箱へ", "danger", G.DELETE, on_click=guard(self._recycle))
        row.addWidget(self.recycle_btn)
        lay.addLayout(row)
        self.restore_hint = W.label(MSG_RESTORE + "。", "Mute", wrap=True)
        lay.addWidget(self.restore_hint)
        self.bottom_bar = bar
        outer.addWidget(bar)

    # ================================================================ 小物
    def _flash(self, text: str, kind: str = "ok") -> None:
        self.saved_pill.set_state(kind, text)
        self.saved_pill.show()
        self._saved_timer.start()

    def _parent(self) -> QWidget | None:
        return self.window()

    # ================================================================ 設定
    @guard
    def _on_settings(self) -> None:
        self.folder_list.clear()
        for f in self.m.folders():
            self.folder_list.addItem(f)
        has = self.folder_list.count() > 0
        self.folder_list.setVisible(has)
        self.folder_empty.setVisible(not has)
        self.rm_btn.setEnabled(has)
        self.add_btn.setEnabled(self.folder_list.count() < 10)

    def _pick_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self._parent(), "調べるフォルダを選ぶ")
        if path:
            self.try_add_folder(path)

    def try_add_folder(self, path: str, *, ask: bool = True) -> bool:
        res = self.m.check_folder(path)
        if res == "forbidden":
            W.message(self._parent(), MSG_FORBIDDEN, "Windows やアプリのデータのフォルダは調べられません。写真のフォルダを選んでください。",
                      kind="warn")
            return False
        if res == "missing":
            W.message(self._parent(), "フォルダが見つかりません", "フォルダだけを追加できます。", kind="warn")
            return False
        if res == "duplicate":
            self._flash("もう追加してあります", "info")
            return False
        if res == "full":
            W.message(self._parent(), "これ以上追加できません", "調べるフォルダは 10 個までです。", kind="warn")
            return False
        if res == "drive_root" and ask:
            ok, _ = W.confirm(self._parent(), "ドライブ全体を調べます", "時間がかかります。続けますか。", ok_text="続ける")
            if not ok:
                return False
        err = self.m.add_folder(path)
        if err:
            W.message(self._parent(), "追加できませんでした", err, kind="error")
            return False
        self._flash("フォルダを追加しました")
        return True

    def _remove_folders(self) -> None:
        for it in self.folder_list.selectedItems():
            self.m.remove_folder(it.text())

    def _on_recursive(self, v: bool) -> None:
        err = self.m.set_recursive(bool(v))
        self._flash(err or "保存しました", "error" if err else "ok")

    def _on_level(self, level: str) -> None:
        err = self.m.set_level(level)
        self._flash(err or "並べ直しています", "error" if err else "ok")

    def _on_mode(self, v: str) -> None:
        err = self.m.set_exact_only(v == "exact")
        self._flash(err or "並べ直しています", "error" if err else "ok")

    def _clear_cache(self) -> None:
        ok, _ = W.confirm(self._parent(), "キャッシュを消しますか", "写真はそのままです。次に調べるときは最初から調べるので、時間がかかります。",
                          ok_text="消す")
        if not ok:
            return
        if self.m.clear_cache():
            self._flash("キャッシュを消しました")
        else:
            W.message(self._parent(), "消せませんでした", "調べている間は消せません。終わってからもう一度押してください。", kind="warn")

    # ================================================================ ドロップ(FR-1)
    def _dropped_dirs(self, e: Any) -> list[str]:
        md = e.mimeData()
        if md is None or not md.hasUrls():
            return []
        return [u.toLocalFile() for u in md.urls() if u.isLocalFile() and os.path.isdir(u.toLocalFile())]

    def dragEnterEvent(self, e: Any) -> None:  # noqa: N802
        try:
            if self._dropped_dirs(e):
                e.acceptProposedAction()
                self.folder_card.setStyleSheet(f"QFrame#Card {{ border: 1px dashed {self.accent}; }}")
            else:
                e.ignore()
        except Exception as ex:  # noqa: BLE001
            _log.error("drag failed: %s", type(ex).__name__)

    def dragLeaveEvent(self, e: Any) -> None:  # noqa: N802
        self.folder_card.setStyleSheet("")

    def dropEvent(self, e: Any) -> None:  # noqa: N802
        try:
            self.folder_card.setStyleSheet("")
            dirs = self._dropped_dirs(e)
            e.acceptProposedAction()
            for d in dirs:
                self.try_add_folder(d)
        except Exception as ex:  # noqa: BLE001
            _log.error("drop failed: %s", type(ex).__name__)

    # ================================================================ スキャン
    def _scan(self) -> None:
        err = self.m.start_scan()
        if err:
            W.message(self._parent(), "調べられません", err, kind="warn")

    @guard
    def _on_state(self) -> None:
        m = self.m
        busy = m.busy
        self.scan_btn.setEnabled(not busy)
        self.cache_btn.setEnabled(not busy)
        self.add_btn.setEnabled(not busy and self.folder_list.count() < 10)
        self.rm_btn.setEnabled(not busy and self.folder_list.count() > 0)
        self.progress_card.setVisible(m.state in (SCANNING, GROUPING, RECYCLING))
        self.cancel_btn.setVisible(m.state in (SCANNING, GROUPING))
        if m.state == SCANNING:
            self.state_pill.set_state("accent", "調べています")
            text = STAGE_TEXT.get(m.stage, "")
            if m.stage == "features" and m.progress_total:
                text += f"  {m.progress_done:,} / {m.progress_total:,}"
                self.bar.setRange(0, m.progress_total)
                self.bar.setValue(m.progress_done)
            else:
                if m.stage == "count" and m.progress_done:
                    text += f"  {m.progress_done:,} 枚"
                self.bar.setRange(0, 0)
            self.stage_label.setText(text)
        elif m.state == GROUPING:
            self.state_pill.set_state("accent", "並べ直しています")
            self.stage_label.setText(STAGE_TEXT["group"])
            self.bar.setRange(0, 0)
        elif m.state == RECYCLING:
            self.state_pill.set_state("warn", "ごみ箱へ送っています")
            self.stage_label.setText("ごみ箱へ送っています。Windows が確認を出したら答えてください。")
            self.bar.setRange(0, 0)
        elif m.model is not None:
            self.state_pill.set_state("ok", f"グループ {len(m.model.groups):,}")
        else:
            self.state_pill.set_state("off", "待機中")
        self._update_bar()
        for c in self._cards:
            c.refresh()

    @guard
    def _on_notices(self) -> None:
        while self.notes_box.count():
            it = self.notes_box.takeAt(0)
            w = it.widget() if it is not None else None
            if w is not None:
                w.deleteLater()
        for n in [*self.m.scan_notices, *self.m.recycle_notices]:
            self.notes_box.addWidget(_Note(n))
        self.notes_holder.setVisible(self.notes_box.count() > 0)

    # ================================================================ 結果(FR-8・段階的に描く)
    @guard
    def _on_results(self) -> None:
        keep_count = max(GROUP_BATCH, self._drawn_groups)
        self._render_timer.stop()
        for c in self._cards:
            c.deleteLater()
        self._cards = []
        while self.results_box.count():
            it = self.results_box.takeAt(0)
            w = it.widget() if it is not None else None
            if w is not None:
                w.deleteLater()
        self._pending = []
        self._drawn_groups = 0
        m = self.m
        last = m.last
        self.t_files.set_value(f"{last.files:,}" if last is not None else "—")
        model = m.model
        if model is None:
            self.t_groups.set_value("—")
            self.results_box.addWidget(self._empty(G.SEARCH, "フォルダを選んで「調べる」を押します",
                                                   "似ている写真をグループにまとめて、残す1枚をおすすめします。"))
            self.more_btn.hide()
            self._update_bar()
            return
        self.t_groups.set_value(f"{len(model.groups):,}")
        if not model.groups:
            n = last.files if last is not None else len(model.photos)
            self.results_box.addWidget(self._empty(G.CHECK, "似ている写真は見つかりませんでした", f"{n:,} 枚を調べました。"))
            self.more_btn.hide()
            self._update_bar()
            return
        exact = model.exact_groups()
        similar = model.similar_groups()
        if exact:
            self._pending.append(("head", ("まったく同じ写真", f"{len(exact):,} グループ · 中身がまったく同じコピーです", G.COPY)))
            self._pending.extend(("group", g) for g in exact)
        if similar:
            self._pending.append(("head", ("似ている写真", f"{len(similar):,} グループ · 連写・撮り直し・縮小した写真など", G.EYE)))
            self._pending.extend(("group", g) for g in similar)
        self._target = min(keep_count, len(model.groups))
        self._render_timer.start()
        self._update_more()
        self._update_bar()

    def _render_step(self) -> None:
        try:
            drawn = 0
            while self._pending and self._drawn_groups < self._target and drawn < RENDER_CHUNK:
                kind, payload = self._pending.pop(0)
                if kind == "head":
                    title, sub, glyph = payload
                    self.results_box.addWidget(self._section_header(title, sub, glyph))
                    continue
                card = GroupCard(self, payload, self._drawn_groups)
                self.results_box.addWidget(card)
                self._cards.append(card)
                self._drawn_groups += 1
                drawn += 1
            if not self._pending or self._drawn_groups >= self._target:
                self._render_timer.stop()
            self._update_more()
        except Exception as e:  # noqa: BLE001
            self._render_timer.stop()
            _log.error("render failed: %s", type(e).__name__)

    @staticmethod
    def _empty(glyph: str, title: str, text: str) -> QWidget:
        w = W.EmptyState(glyph, title, text)
        w.setMinimumHeight(200)  # 折り返しの文で高さが足りず、字形が見出しに重なるのを防ぐ
        return w

    def _section_header(self, title: str, sub: str, glyph: str) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(2, 10, 2, 0)
        lay.setSpacing(10)
        g = W.Glyph(glyph, 16, self.accent)
        g.setFixedSize(32, 32)
        g.setStyleSheet(f"color: {self.accent}; background: {T.alpha(self.accent, 0.12)}; border-radius: 8px;")
        lay.addWidget(g)
        box = QVBoxLayout()
        box.setSpacing(0)
        box.addWidget(W.label(title, "H2"))
        box.addWidget(W.label(sub, "Mute", wrap=True))
        lay.addLayout(box, 1)
        return w

    def remaining_groups(self) -> int:
        return sum(1 for k, _ in self._pending if k == "group")

    def _update_more(self) -> None:
        rest = self.remaining_groups()
        self.more_btn.setVisible(rest > 0 and not self._render_timer.isActive())
        self.more_btn.setText(f"{G.DOWN}  さらに表示(残り {rest:,} グループ)")

    def _load_more(self) -> None:
        if not self._pending:
            return
        self._target = self._drawn_groups + GROUP_BATCH
        self._render_timer.start()
        self._update_more()

    def _on_scrolled(self, v: int) -> None:
        try:
            sb = self.sp.verticalScrollBar()
            if self._pending and not self._render_timer.isActive() and v >= sb.maximum() - 500:
                self._load_more()
        except Exception as e:  # noqa: BLE001
            _log.error("scroll failed: %s", type(e).__name__)

    # ================================================================ 選択・ごみ箱へ(FR-12)
    @guard
    def _on_selection(self) -> None:
        for c in self._cards:
            c.refresh()
        self._update_bar()

    def _update_bar(self) -> None:
        model = self.m.model
        n = len(model.selected()) if model is not None else 0
        size = model.selected_bytes() if model is not None else 0
        self.t_sel.set_value(f"{n:,}")
        self.t_bytes.set_value(human_bytes(size))
        self.recycle_btn.setText(f"{G.DELETE}  選んだ {n:,} 枚をごみ箱へ(合計 {human_bytes(size)})")
        self.recycle_btn.setEnabled(n > 0 and not self.m.busy)

    def _recycle(self) -> None:
        model = self.m.model
        if model is None or self.m.busy:
            return
        n = len(model.selected())
        if not n:
            return
        size = human_bytes(model.selected_bytes())
        ok, _ = W.confirm(self._parent(), "ごみ箱へ送りますか",
                          f"{n:,} 枚(合計 {size})をごみ箱へ送ります。\nごみ箱から元に戻せます。",
                          ok_text="ごみ箱へ送る", danger=True, glyph=G.DELETE)
        if not ok:
            return
        hwnd: int | None = None
        try:
            hwnd = int(self.window().winId())
        except (RuntimeError, TypeError, ValueError):
            hwnd = None
        err = self.m.recycle_selected(hwnd)
        if err:
            W.message(self._parent(), "送りませんでした", err, kind="warn")

    # ================================================================ プレビュー(FR-11)
    def open_preview(self, photo: Photo) -> None:
        dlg = PreviewDialog(self, photo)
        dlg.exec()
        try:
            self.m.signals.selection.disconnect(dlg._sync)
        except (RuntimeError, TypeError):
            pass
