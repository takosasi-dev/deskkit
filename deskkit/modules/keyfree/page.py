# Control Center の KeyFree 画面: Hero(調べる・状態)→ 始める前の確認(FR-1)→ 進み具合と中止(FR-6)→ 知らせ(FR-7・FR-8・§10)→
# 分かること・分からないこと(FR-18)→ 件数 → おすすめ(FR-11)→ DeskKit のキー(FR-10)→ 1つだけ調べる(FR-9)→ 修飾キーの組ごとの表(FR-3)→ 設定。
# キーの組み合わせの名前は画面にだけ出す(ログには書かない。INV-4)。色は theme(T.*)と catalog のアクセント色を実行時に読む。
from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QApplication,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QProgressBar,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from deskkit import catalog
from deskkit.hotkeys import hotkey_label
from deskkit.modules.keyfree import combos, keynames, scan, trykey
from deskkit.modules.keyfree.combos import Combo
from deskkit.modules.keyfree.module import TEXT_BLOCKED, TEXT_BUSY, TEXT_UNSUPPORTED, state_text
from deskkit.ui import theme as T
from deskkit.ui import widgets as W
from deskkit.ui.theme import G

if TYPE_CHECKING:
    from deskkit.modules.keyfree.module import KeyFreeModule

_log = logging.getLogger("deskkit.keyfree")
F = TypeVar("F", bound=Callable[..., Any])
COLUMNS = 13
KNOWS = ("ほかのアプリが「どこでも効くキー」として使っているかは分かります。開いているアプリの中だけで効くキー"
         "(ブラウザーの Ctrl+T など)と、使っているアプリの名前は分かりません。")
OWNER = "どのアプリが使っているかは、Windows が教えてくれません。"
USED_HELP = ("ほかのアプリが使っています。どのアプリかは Windows が教えてくれません。心当たりのアプリを閉じてから"
             "『調べ直す』を押すと、見当をつけられます")
WIN_NOTE = "Windows 用の組です。空きと出ても、Windows やアプリが先に受け取ることがあるので、おすすめには入れません。"
TRY_TEXT = {
    trykey.RECEIVED: ("ok", "届きました。このキーは使えます"),
    trykey.TIMEOUT: ("warn", "届きませんでした。Windows かほかのアプリが先に受け取っているかもしれません"),
    trykey.CONFLICT: ("warn", "今はほかのアプリが使っています"),
}
SHORT = {scan.FREE: "空き", scan.USED: "使用中", scan.DESKKIT: "DeskKit", scan.ERROR: "調べられない",
         scan.RESERVED: "調べられない", scan.PENDING: "…"}


def _guard(fn: F) -> F:
    """シグナルから呼ぶ処理の例外を Qt へ漏らさない。ログには型名だけ(INV-4)。"""

    @functools.wraps(fn)
    def wrapper(*a: Any, **k: Any) -> Any:
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001
            _log.warning("page handler %s failed: %s", getattr(fn, "__name__", "?"), type(e).__name__)
            return None

    return wrapper  # type: ignore[return-value]


def _acc() -> str:
    return catalog.info("keyfree").accent


def state_color(state: str) -> str:
    return {scan.FREE: T.SUCCESS, scan.USED: T.DANGER, scan.DESKKIT: _acc(), scan.ERROR: T.TEXT_MUTE,
            scan.RESERVED: T.TEXT_MUTE}.get(state, T.BORDER_HI)


def _clear(lay: QLayout) -> None:
    while lay.count():
        it = lay.takeAt(0)
        w = it.widget() if it is not None else None
        if w is not None:
            w.hide()
            w.setParent(None)
            w.deleteLater()
        elif it is not None:
            sub = it.layout()
            if sub is not None:
                _clear(sub)


def _chip(text: str, color: str) -> QLabel:
    lb = QLabel(text)
    lb.setStyleSheet(f"color: {color}; background: {T.alpha(color, 0.13)}; border: 1px solid {T.alpha(color, 0.35)};"
                     f" border-radius: 9px; padding: 1px 8px; font-size: 11px; font-weight: 700;")
    return lb


def _holder_label(name: str) -> str:
    """登録名 → 画面の名前(ホームのホットキー一覧と同じ規則。読めなければ登録名)。"""
    return hotkey_label(name, {})


# ================================================================== キーのボタン
class KeyCap(QAbstractButton):
    """表の1つのキー。色と文字の両方で状態を出す(FR-3)。Windows 用の組には角に「Win用」の印(K-5)。"""

    def __init__(self, combo: Combo, label: str, windows: bool) -> None:
        super().__init__()
        self.combo = combo
        self.label = label
        self.windows = windows
        self.state = scan.PENDING
        self.error = 0
        self.selected = False
        self._hover = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumSize(46, 48)
        self.setMaximumHeight(52)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(62, 50)

    def set_state(self, state: str, error: int = 0) -> None:
        if state != self.state or error != self.error:
            self.state = state
            self.error = error
            self.update()

    def set_selected(self, on: bool) -> None:
        if on != self.selected:
            self.selected = on
            self.update()

    def enterEvent(self, e: Any) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e: Any) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e: object) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        color = QColor(state_color(self.state))
        bg = QColor(color)
        bg.setAlphaF(0.24 if self._hover else 0.13)
        if self.state == scan.PENDING:
            bg = QColor(T.SURFACE3 if self._hover else T.SURFACE2)
        path = QPainterPath()
        path.addRoundedRect(r, 9, 9)
        p.fillPath(path, bg)
        edge = QColor(color)
        edge.setAlphaF(1.0 if self.selected else 0.45)
        p.setPen(QPen(edge if self.state != scan.PENDING else QColor(T.BORDER), 2.0 if self.selected else 1.0))
        p.drawPath(path)
        # キーの名前
        f = QFont(self.font())
        f.setPixelSize(14 if len(self.label) <= 2 else 12)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.setPen(QColor(T.TEXT))
        top = QRectF(r.left(), r.top() + (8 if self.windows else 3), r.width(), r.height() * 0.5)
        p.drawText(top, int(Qt.AlignmentFlag.AlignCenter), self.label)
        # 状態(文字)
        f2 = QFont(self.font())
        f2.setPixelSize(9 if self.state in (scan.ERROR, scan.RESERVED) else 10)
        f2.setWeight(QFont.Weight.Bold)
        p.setFont(f2)
        p.setPen(QColor(T.TEXT_DIM) if self.state == scan.PENDING else color)
        bottom = QRectF(r.left(), r.top() + r.height() * 0.55, r.width(), r.height() * 0.42)
        p.drawText(bottom, int(Qt.AlignmentFlag.AlignCenter), SHORT.get(self.state, ""))
        if self.windows:
            f3 = QFont(self.font())
            f3.setPixelSize(8)
            f3.setWeight(QFont.Weight.Bold)
            p.setFont(f3)
            badge = QRectF(r.right() - 25, r.top() + 2, 23, 11)
            bp = QPainterPath()
            bp.addRoundedRect(badge, 4, 4)
            wc = QColor(T.INFO)
            wc.setAlphaF(0.22)
            p.fillPath(bp, wc)
            p.setPen(QColor(T.INFO))
            p.drawText(badge, int(Qt.AlignmentFlag.AlignCenter), "Win用")
        p.end()


# ================================================================== 下に出る小さな知らせ
class Toast(QLabel):
    """画面の下に数秒だけ出る知らせ(FR-4 の「コピーしました」)。"""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(f"QLabel {{ background: {T.SURFACE3}; color: {T.TEXT}; border: 1px solid {T.BORDER_HI};"
                           f" border-radius: 12px; padding: 9px 18px; font-weight: 600; }}")
        self._eff = QGraphicsOpacityEffect(self)
        self._eff.setOpacity(0.0)
        self.setGraphicsEffect(self._eff)
        self._anim = QPropertyAnimation(self._eff, b"opacity", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._hide = QTimer(self)
        self._hide.setSingleShot(True)
        self._hide.timeout.connect(self._fade_out)
        self.hide()

    def show_text(self, text: str) -> None:
        self.setText(text)
        self.adjustSize()
        par = self.parentWidget()
        if par is not None:
            self.move(max(8, (par.width() - self.width()) // 2), max(8, par.height() - self.height() - 22))
        self.show()
        self.raise_()
        self._anim.stop()
        self._anim.setStartValue(self._eff.opacity())
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._hide.start(2400)

    def _fade_out(self) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._eff.opacity())
        self._anim.setEndValue(0.0)
        self._anim.start()


# ================================================================== 修飾キーの組ごとのカード
class GroupCard(W.Card):
    def __init__(self, page: KeyFreePage, mods: int, plan: combos.Plan, chars: dict[int, str]) -> None:
        name = combos.group_name(mods)
        super().__init__(name.replace("+", " + "), None, G.KEYBOARD, _acc())
        self.page = page
        self.mods = mods
        self.summary = QLabel("")
        self.summary.setObjectName("Mute")
        self.add_header_widget(self.summary)
        if mods & combos.MOD_WIN:
            self.add_header_widget(_chip("Windows 用", T.INFO))
        self.caps: dict[Combo, KeyCap] = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        row = 0
        for krow, vks in plan.rows:
            if not vks:
                continue
            lab = W.label(krow.label, "Mute")
            lab.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            lab.setFixedWidth(52)
            lines = (len(vks) + COLUMNS - 1) // COLUMNS
            grid.addWidget(lab, row, 0, lines, 1, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
            for i, vk in enumerate(vks):
                c = (mods, vk)
                cap = KeyCap(c, keynames.key_label(vk, chars), combos.windows_mark(mods, vk))
                cap.clicked.connect(_guard(lambda _=False, cc=c: self.page.select(cc)))
                self.caps[c] = cap
                grid.addWidget(cap, row + i // COLUMNS, 1 + i % COLUMNS)
            row += lines
        for col in range(1, COLUMNS + 1):
            grid.setColumnStretch(col, 1)
        self.add_layout(grid)
        self.detail = QFrame()
        self.detail.setObjectName("Inset")
        self.detail_lay = QVBoxLayout(self.detail)
        self.detail_lay.setContentsMargins(14, 10, 14, 12)
        self.detail_lay.setSpacing(6)
        self.detail.hide()
        self.add(self.detail)

    def update_states(self, res: scan.ScanResult, selected: Combo | None) -> None:
        n = {scan.FREE: 0, scan.USED: 0, scan.DESKKIT: 0}
        for c, cap in self.caps.items():
            cell = res.cells.get(c)
            st = cell.state if cell is not None else scan.PENDING
            cap.set_state(st, cell.error if cell is not None else 0)
            cap.set_selected(c == selected)
            if st in n:
                n[st] += 1
            tip = f"{self.page.m.format(c)}: {state_text(st, cap.error) if st != scan.PENDING else 'まだ調べていません'}"
            if cap.windows:
                tip += "(Windows 用)"
            cap.setToolTip(tip)
        self.summary.setText(f"空き {n[scan.FREE]}・使用中 {n[scan.USED]}・DeskKit {n[scan.DESKKIT]}")


# ================================================================== 画面
class KeyFreePage(W.ScrollPage):
    def __init__(self, module: KeyFreeModule) -> None:
        super().__init__()
        self.m = module
        acc = _acc()
        info = catalog.info("keyfree")
        self.selected: Combo | None = None
        self._confirming = False
        self.single_text = ""
        self.single_level = "info"
        self.recheck_text: dict[Combo, str] = {}
        self._plan_key: object = None
        self._build_queue: list[int] = []
        self.groups: list[GroupCard] = []
        # ---- Hero
        self.hero = W.Hero("KeyFree", info.tagline, info.glyph, acc)
        self.pill = W.StatusPill("", "off")
        self.hero.add_pill(self.pill)
        self.scan_btn = W.button("調べる", "primary", G.SEARCH, _guard(self._ask_start))
        self.hero.add_action(self.scan_btn)
        self.add(self.hero)
        # ---- 始める前の確認(FR-1)
        self.confirm = W.Card(None, padding=16)
        row = QHBoxLayout()
        row.setSpacing(12)
        g = W.Glyph(G.KEYBOARD, 18, T.WARN)
        g.setFixedSize(40, 40)
        g.setStyleSheet(f"color: {T.WARN}; background: {T.alpha(T.WARN, 0.14)}; border-radius: 12px;")
        row.addWidget(g, 0, Qt.AlignmentFlag.AlignTop)
        tb = QVBoxLayout()
        tb.setSpacing(3)
        tb.addWidget(W.label("数秒、キーボードに触れないでください", "H3"))
        tb.addWidget(W.label("キーの組み合わせを1つずつ Windows に登録して、すぐに外します。調べている一瞬に押されたキーは、"
                             "使っているアプリに届かないことがあります。", "Dim", wrap=True))
        row.addLayout(tb, 1)
        self.go_btn = W.button("始める", "primary", G.PLAY, _guard(self._start))
        row.addWidget(self.go_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(W.button("やめる", "ghost", None, _guard(self._hide_confirm)), 0, Qt.AlignmentFlag.AlignVCenter)
        self.confirm.add_layout(row)
        self.confirm.hide()
        self.add(self.confirm)
        # ---- 進み具合(FR-6)
        self.progress_card = W.Card(None, padding=16)
        self.progress_label = W.label("", "H3")
        self.cancel_btn = W.button("中止", "danger", G.CLOSE, _guard(self.m.cancel_scan))
        self.progress_card.add_layout(W.hbox(self.progress_label, None, self.cancel_btn))
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.progress_card.add(self.bar)
        self.progress_card.hide()
        self.add(self.progress_card)
        # ---- 知らせ
        self.notice_card = W.Card(None, padding=14)
        self.notice_box = QVBoxLayout()
        self.notice_box.setSpacing(6)
        self.notice_card.add_layout(self.notice_box)
        self.notice_card.hide()
        self.add(self.notice_card)
        # ---- 分かること・分からないこと(FR-18・K-6)
        about = W.Card("分かること・分からないこと", None, G.INFO, acc)
        about.add(W.label(KNOWS, "Dim", wrap=True))
        about.add(W.label(OWNER + "「使用中」の組は、心当たりのアプリを閉じてから『調べ直す』と、見当をつけられます。", "Mute", wrap=True))
        self.add(about)
        # ---- 件数
        self.t_free = W.StatTile("空き", "–", G.CHECK, T.SUCCESS)
        self.t_used = W.StatTile("使用中", "–", G.LOCK, T.DANGER)
        self.t_deskkit = W.StatTile("DeskKit", "–", G.KEYBOARD, acc)
        self.t_unavail = W.StatTile("調べられない", "–", G.WARNING, T.TEXT_MUTE)
        tiles = QWidget()
        tiles.setLayout(W.hbox(self.t_free, self.t_used, self.t_deskkit, self.t_unavail, spacing=12))
        self.add(tiles)
        # ---- おすすめ(FR-11)
        self.rec_card = W.Card("おすすめ", "空いていて、Windows 用でない英字・数字の組です。押すとコピーします。", G.SPARKLE, acc)
        self.rec_box = QHBoxLayout()
        self.rec_box.setSpacing(8)
        self.rec_card.add_layout(self.rec_box)
        self.add(self.rec_card)
        # ---- DeskKit のキー(FR-10)
        self.dk_card = W.Card("DeskKit のキー", "DeskKit(本体と各モジュール)が使っているキーと、取れなかったキーです。", G.KEYBOARD, acc)
        self.dk_box = QVBoxLayout()
        self.dk_box.setSpacing(8)
        self.dk_card.add_layout(self.dk_box)
        self.add(self.dk_card)
        # ---- 1つだけ調べる(FR-9)
        one = W.Card("1つだけ調べる", "修飾キーが1つの組(Win+E など)もここでは調べられます。", G.SEARCH, acc)
        self.one_edit = QLineEdit()
        self.one_edit.setPlaceholderText("例: Ctrl+Alt+Space")
        self.one_edit.returnPressed.connect(_guard(self._check_one))
        self.one_btn = W.button("調べる", "secondary", G.SEARCH, _guard(self._check_one))
        one.add_layout(W.hbox(self.one_edit, self.one_btn))
        self.one_result = W.label("", "Dim", wrap=True)
        self.one_result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        one.add(self.one_result)
        self.add(one)
        # ---- 表(FR-3)
        self.table_head = W.Card("修飾キーの組ごとの表", "空きのキーを押すとコピーします。使用中のキーを押すと、調べ直せます。", G.LIST, acc)
        legend = QHBoxLayout()
        legend.setSpacing(8)
        for st, text in ((scan.FREE, "空き"), (scan.USED, "使用中"), (scan.DESKKIT, "DeskKit"), (scan.ERROR, "調べられない")):
            legend.addWidget(_chip(text, state_color(st)))
        legend.addWidget(_chip("Win用 = Windows 用", T.INFO))
        legend.addStretch(1)
        self.table_head.add_layout(legend)
        self.empty = W.EmptyState(G.KEYBOARD, "まだ調べていません", "「調べる」を押すと、修飾キーの組ごとに、空いている組と使われている組を出します。")
        self.table_head.add(self.empty)
        self.add(self.table_head)
        self.groups_box = QVBoxLayout()
        self.groups_box.setSpacing(16)
        self.groups_box.setContentsMargins(0, 0, 0, 0)
        holder = QWidget()
        holder.setLayout(self.groups_box)
        self.add(holder)
        # ---- 設定
        sc = W.Card("設定", None, G.SETTINGS, acc)
        self.sw_win = W.ToggleSwitch(bool(self.m.cfg["include_win"]), acc)
        self.sw_win.toggled.connect(_guard(lambda v: self._set("include_win", bool(v))))
        sc.add(W.SettingRow("Windows キーの組み合わせも調べる",
                            "Windows キーの組は Windows 用に予約されています。7 つの組を足すので、調べる数が約 3 倍になります。", self.sw_win))
        self.sp_batch = self._spin(8, 128, "batch_size", " 組")
        sc.add(W.SettingRow("まとめて返す数", "この数ごとに表を塗ります(押されたキーの確かめもこの区切りで行います)。", self.sp_batch))
        self.sp_try = self._spin(5, 30, "try_seconds", " 秒")
        sc.add(W.SettingRow("押して確かめる時間", "この時間だけ DeskKit がキーを預かります。過ぎたら必ず返します。", self.sp_try))
        self.sp_rec = self._spin(1, 20, "recommend_count", " 個")
        sc.add(W.SettingRow("おすすめの数", None, self.sp_rec))
        self.order = QListWidget()
        self.order.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.order.setFixedHeight(136)
        self.order.addItems([str(x) for x in self.m.cfg["recommend_order"]])
        self.order.model().rowsMoved.connect(_guard(lambda *_a: self._save_order()))
        sc.add(W.SettingRow("おすすめに使う修飾キーの順", "ドラッグで並べ替えます。上の組から順におすすめします。", None))
        sc.add(self.order)
        self.add(sc)
        self.finish()
        self.toast = Toast(self.viewport())
        self._ago = QTimer(self)
        self._ago.setInterval(30_000)
        self._ago.timeout.connect(_guard(self._refresh_pill))
        self._ago.start()
        self.m.notifier.changed.connect(_guard(self._refresh))
        self._refresh()

    # ------------------------------------------------------------ 部品
    def _spin(self, lo: int, hi: int, key: str, suffix: str) -> QSpinBox:
        sp = QSpinBox()
        sp.setRange(lo, hi)
        sp.setSuffix(suffix)
        sp.setValue(int(self.m.cfg[key]))
        sp.editingFinished.connect(_guard(lambda: self._set(key, sp.value()) if int(self.m.cfg[key]) != sp.value() else None))
        return sp

    def _set(self, key: str, value: Any) -> None:
        err = self.m.set_option(key, value)
        if err:
            self.toast.show_text(err)

    def _save_order(self) -> None:
        self._set("recommend_order", [self.order.item(i).text() for i in range(self.order.count())])

    # ------------------------------------------------------------ 表示
    def _refresh(self) -> None:
        m = self.m
        res = m.result()
        running = res is not None and res.running
        supported = m.supported()
        self.scan_btn.setEnabled(supported and not running)
        self.one_btn.setEnabled(supported)
        self.one_edit.setEnabled(supported)
        self._refresh_pill()
        # 進み具合
        self.progress_card.setVisible(running)
        if running and res is not None:
            total = max(1, res.to_probe)
            done = min(res.done_count(), total)
            self.bar.setRange(0, total)
            self.bar.setValue(done)
            self.progress_label.setText(f"{res.to_probe} 組中 {done} 組を調べました")
            self.cancel_btn.setEnabled(res.stop_why is None)
        # 件数
        n = res.counts() if res is not None else None
        for tile, key in ((self.t_free, "free"), (self.t_used, "used"), (self.t_deskkit, "deskkit"),
                          (self.t_unavail, "unavailable")):
            tile.set_value(str(n[key]) if n is not None else "–")
        self._fill_notices()
        self._fill_recommend()
        self._fill_deskkit()
        self._fill_groups()
        self.one_result.setText(self.single_text)
        self.one_result.setVisible(bool(self.single_text))
        self.one_result.setStyleSheet(f"color: {self._level_color(self.single_level)};" if self.single_text else "")

    def _refresh_pill(self) -> None:
        m = self.m
        res = m.result()
        if not m.supported():
            self.pill.set_state("error", "この DeskKit では使えません")
        elif res is None:
            self.pill.set_state("off", "まだ調べていません")
        elif res.running:
            self.pill.set_state("info", "調べています")
        else:
            ago = m.minutes_ago()
            when = "1 分以内に調べた結果" if not ago else f"{ago} 分前に調べた結果"
            self.pill.set_state("ok" if res.reason == "finished" else "warn",
                                when if res.reason == "finished" else f"{when}(途中まで)")

    @staticmethod
    def _level_color(level: str) -> str:
        return {"ok": T.SUCCESS, "warn": T.WARN, "error": T.DANGER}.get(level, T.TEXT_DIM)

    def notices(self) -> list[tuple[str, str]]:
        """いま出す知らせ(レベル, 文)。テストからも読む。"""
        m = self.m
        out: list[tuple[str, str]] = []
        if not m.supported():
            out.append(("error", TEXT_UNSUPPORTED))
        if m.notice is not None and m.notice[1] != TEXT_UNSUPPORTED:
            out.append(m.notice)
        if m.scanner.last_busy:
            out.append(("warn", TEXT_BUSY))
        res = m.result()
        if res is None:
            return out
        if not res.running:
            if res.reason == "cancelled":
                out.append(("warn", TEXT_BLOCKED) if res.stop_why == "foreground" else ("info", "途中で止めました"))
            elif res.reason == "release_failed":
                out.append(("error", "キーを返せませんでした。DeskKit を終了して起動し直してください"))
            elif res.reason == "error":
                out.append(("error", "調べる途中で問題が起きました。もう一度調べてください"))
            if res.layout_changed:
                out.append(("warn", "配列が変わりました。もう一度調べてください"))
            if res.deskkit_changed:
                out.append(("warn", "途中で DeskKit のキーが変わりました。もう一度調べてください"))
        seen: set[Combo] = set()
        for c in res.pressed:
            if c in seen:
                continue
            seen.add(c)
            out.append(("warn", f"調べている間に {m.format(c)} が押されました。そのアプリには届いていないかもしれません。"
                                "もう一度押してください"))
        return out

    def _fill_notices(self) -> None:
        _clear(self.notice_box)
        items = self.notices()
        for level, text in items:
            row = QHBoxLayout()
            row.setSpacing(10)
            color = self._level_color(level)
            glyph = {"ok": G.CHECK, "warn": G.WARNING, "error": G.ERROR}.get(level, G.INFO)
            row.addWidget(W.Glyph(glyph, 14, color), 0, Qt.AlignmentFlag.AlignTop)
            lb = W.label(text, None, wrap=True)
            lb.setStyleSheet(f"color: {color};")
            row.addWidget(lb, 1)
            self.notice_box.addLayout(row)
        self.notice_card.setVisible(bool(items))

    def _copy_button(self, c: Combo) -> QWidget:
        text = self.m.format(c)
        return W.button(text, "secondary", G.COPY, _guard(lambda cc=c: self.copy(cc)), tooltip="押すとコピーします")

    def _fill_recommend(self) -> None:
        _clear(self.rec_box)
        recs = self.m.recommendations()
        if not recs:
            res = self.m.result()
            text = ("「調べる」を押すと、ここにおすすめが出ます。" if res is None or res.running
                    else "おすすめできる空きがありませんでした。表から選んでください。")
            self.rec_box.addWidget(W.label(text, "Mute", wrap=True))
            return
        flow = QGridLayout()
        flow.setSpacing(8)
        for i, c in enumerate(recs):
            flow.addWidget(self._copy_button(c), i // 4, i % 4)
        self.rec_box.addLayout(flow)
        self.rec_box.addStretch(1)

    def _fill_deskkit(self) -> None:
        _clear(self.dk_box)
        snap = self.m.snapshot()
        self.dk_card.setVisible(snap is not None)
        if snap is None:
            return
        held = [h for h in snap.held if not str(h.name).startswith("keyfree.")]
        failed = [f for f in snap.failed if not str(f.name).startswith("keyfree.")]
        if not held and not failed:
            self.dk_box.addWidget(W.label("DeskKit は今、ショートカットキーを使っていません。", "Mute"))
            return
        for h in held:
            row = QHBoxLayout()
            row.setSpacing(10)
            row.addWidget(W.Glyph(G.CHECK, 14, T.SUCCESS))
            row.addWidget(W.label(_holder_label(str(h.name))), 1)
            row.addWidget(_chip(self.m.format((int(h.mods), int(h.vk))), _acc()))
            self.dk_box.addLayout(row)
        for f in failed:
            box = QFrame()
            box.setObjectName("Inset")
            lay = QVBoxLayout(box)
            lay.setContentsMargins(14, 10, 14, 10)
            lay.setSpacing(6)
            head = QHBoxLayout()
            head.setSpacing(10)
            head.addWidget(W.Glyph(G.WARNING, 14, T.WARN))
            head.addWidget(W.label(_holder_label(str(f.name)), "H3"), 1)
            head.addWidget(_chip(self.m.format((int(f.mods), int(f.vk))), T.WARN))
            lay.addLayout(head)
            why = ("ほかのアプリが使っているため、DeskKit では使えていません。" if int(f.error) == scan.ERROR_HOTKEY_ALREADY_REGISTERED
                   else f"DeskKit では使えていません(エラー {int(f.error)})。")
            lay.addWidget(W.label(why + "設定で、下の空いている組に変えられます。", "Dim", wrap=True))
            recs = self.m.recommendations(3)
            if recs:
                lay.addLayout(W.hbox(*[self._copy_button(c) for c in recs], None))
            else:
                lay.addWidget(W.label("「調べる」を押すと、代わりの候補を3つ出します。", "Mute"))
            self.dk_box.addWidget(box)

    def _fill_groups(self) -> None:
        res = self.m.result()
        self.empty.setVisible(res is None)
        if res is None:
            if self.groups:
                _clear(self.groups_box)
                self.groups = []
            self._plan_key = None
            return
        key = (res.plan, tuple(sorted(res.chars.items())))
        if key != self._plan_key:
            _clear(self.groups_box)
            self.groups = []
            self._plan_key = key
            self._build_queue = list(res.plan.groups)
            # 表示中は1回のイベントで1枚ずつ作る(956 組の表を一度に作ると 200ms ほど画面が止まる。NFR4-4)
            while self._build_queue:
                self._build_one(res)
                if self.isVisible():
                    break
            if self._build_queue:
                QTimer.singleShot(0, _guard(self._build_rest))
        for g in self.groups:
            g.update_states(res, self.selected)
        self._fill_detail()

    def _build_one(self, res: scan.ScanResult) -> None:
        g = GroupCard(self, self._build_queue.pop(0), res.plan, res.chars)
        self.groups_box.addWidget(g)
        self.groups.append(g)

    def _build_rest(self) -> None:
        res = self.m.result()
        if res is None or not self._build_queue or (res.plan, tuple(sorted(res.chars.items()))) != self._plan_key:
            return
        self._build_one(res)
        self.groups[-1].update_states(res, self.selected)
        if self._build_queue:
            QTimer.singleShot(0, _guard(self._build_rest))
        else:
            self._fill_detail()

    # ------------------------------------------------------------ 選んだキーの詳しい欄
    def _fill_detail(self) -> None:
        res = self.m.result()
        for g in self.groups:
            if self.selected is None or self.selected not in g.caps or res is None:
                g.detail.hide()
                continue
            _clear(g.detail_lay)
            self._detail_for(g, self.selected, res)
            g.detail.show()

    def _detail_for(self, g: GroupCard, c: Combo, res: scan.ScanResult) -> None:
        m = self.m
        lay = g.detail_lay
        cell = res.cells.get(c)
        st = cell.state if cell is not None else scan.PENDING
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(W.label(m.format(c), "H3"))
        head.addWidget(_chip(state_text(st, cell.error if cell else 0) if st != scan.PENDING else "まだ調べていません",
                             state_color(st)))
        if combos.windows_mark(*c):
            head.addWidget(_chip("Windows 用", T.INFO))
        head.addStretch(1)
        lay.addLayout(head)
        running = res.running or m.scanner.single_running
        if st == scan.FREE:
            lay.addWidget(W.label(f"{m.format(c)} をコピーしました。DeskKit やほかのアプリの設定に貼れます。", "Dim", wrap=True))
            if combos.windows_mark(*c):
                lay.addWidget(W.label(WIN_NOTE, "Mute", wrap=True))
            res_label = keynames.key_label(c[1], res.chars)
            if c[1] in keynames.SYMBOLS and not m.format(c).endswith("+" + res_label):
                lay.addWidget(W.label(f"キーボードの「{res_label}」のキーです。コピーした書き方は DeskKit の設定で使う書き方で、"
                                      "記号のキーは米国配列の名前で書きます。", "Mute", wrap=True))
            tk = m.trykey
            if tk.active == c:
                lay.addWidget(W.label(f"今、このキーを押してみてください(残り {tk.remaining()} 秒)", None, wrap=True))
                lay.addLayout(W.hbox(W.button("やめる", "ghost", G.CLOSE, _guard(tk.stop)), None))
            else:
                if tk.last == c and tk.result in TRY_TEXT:
                    level, text = TRY_TEXT[tk.result]
                    lb = W.label(text, None, wrap=True)
                    lb.setStyleSheet(f"color: {self._level_color(level)};")
                    lay.addWidget(lb)
                elif tk.last == c and tk.result == trykey.FAILED:
                    lay.addWidget(W.label(f"登録できませんでした(エラー {tk.error})", "Dim"))
                b = W.button("押して確かめる", "secondary", G.KEYBOARD, _guard(lambda cc=c: self._try(cc)),
                             tooltip=f"{m.cfg['try_seconds']} 秒だけ DeskKit がこのキーを預かり、押したら届いたかを出します")
                b.setEnabled(not running and tk.active is None)
                lay.addLayout(W.hbox(W.button("もう一度コピー", "ghost", G.COPY, _guard(lambda cc=c: self.copy(cc))), b, None))
        elif st in (scan.USED, scan.ERROR):
            text = USED_HELP if st == scan.USED else "調べられませんでした。少し待ってから『調べ直す』を押してください。"
            lay.addWidget(W.label(text, "Dim", wrap=True))
            if c in self.recheck_text:
                lay.addWidget(W.label(self.recheck_text[c], None, wrap=True))
            b = W.button("調べ直す", "secondary", G.REFRESH, _guard(lambda cc=c: self._recheck(cc)))
            b.setEnabled(not running)
            lay.addLayout(W.hbox(b, None))
        elif st == scan.DESKKIT:
            holder = res.held_names.get(c)
            lay.addWidget(W.label(f"DeskKit が使っています({_holder_label(holder)})。" if holder else "DeskKit が使っています。",
                                  "Dim", wrap=True))
        elif st == scan.RESERVED:
            lay.addWidget(W.label("Windows が使う組なので調べません(F12 はデバッガー用、Ctrl+Alt+Delete は Windows の画面用)。",
                                  "Dim", wrap=True))
        else:
            lay.addWidget(W.label("まだ調べていません。", "Mute"))

    # ------------------------------------------------------------ 操作
    def _ask_start(self) -> None:
        if self.m.result() is not None and self.m.result().running:  # type: ignore[union-attr]
            return
        self._confirming = True
        self.confirm.show()
        self.go_btn.setFocus()

    def _hide_confirm(self) -> None:
        self._confirming = False
        self.confirm.hide()

    def _start(self) -> None:
        if not self._confirming:
            return                      # 連打は無視(§10)
        self._hide_confirm()
        self.selected = None
        self.recheck_text.clear()
        self.m.begin_scan()

    def select(self, c: Combo) -> None:
        res = self.m.result()
        if res is None:
            return
        self.selected = c
        if res.state(c) == scan.FREE:
            self.copy(c)           # FR-4
        self._refresh()

    def copy(self, c: Combo) -> None:
        text = self.m.format(c)
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(text)
        self.m.note_copy()
        self.toast.show_text(f"{text} をコピーしました")

    def _try(self, c: Combo) -> None:
        r = self.m.try_key(c)
        if r == scan.RUNNING:
            self.toast.show_text(TEXT_BUSY)
        self._refresh()

    def _recheck(self, c: Combo) -> None:
        name = self.m.format(c)

        def done(r: scan.SingleResult) -> None:
            self.recheck_text[c] = self._single_line(name, r)
            self._refresh()

        st = self.m.check(c, done)
        if st != scan.STARTED and st != scan.DONE:
            self.recheck_text[c] = {scan.BLOCKED: TEXT_BLOCKED, scan.RUNNING: TEXT_BUSY}.get(st, TEXT_UNSUPPORTED)
        self._refresh()

    def _single_line(self, name: str, r: scan.SingleResult) -> str:
        special = {"busy": TEXT_BUSY, "cancelled": "途中で止めました",
                   "release_failed": "キーを返せませんでした。DeskKit を終了して起動し直してください"}
        if r.state in special:
            return special[r.state]
        line = f"{name}: {state_text(r.state, r.error)}"
        if r.state == scan.DESKKIT and r.holder:
            line += f"({_holder_label(r.holder)})"
        if r.state == scan.USED:
            line += "。" + USED_HELP
        for p in dict.fromkeys(r.pressed):
            line += (f"\n調べている間に {self.m.format(p)} が押されました。そのアプリには届いていないかもしれません。"
                     "もう一度押してください")
        return line

    def _check_one(self) -> None:
        combo, why = self.m.parse_text(self.one_edit.text())
        if combo is None:
            self.single_text, self.single_level = why or "", "warn"
            self._refresh()
            return
        name = self.m.format(combo)

        def done(r: scan.SingleResult) -> None:
            ok = r.state == scan.FREE
            self.single_text = self._single_line(name, r)
            self.single_level = "ok" if ok else ("warn" if r.state in (scan.USED, "busy", "cancelled") else "info")
            self._refresh()

        self.single_text, self.single_level = f"{name} を調べています…", "info"
        st = self.m.check(combo, done)
        if st not in (scan.STARTED, scan.DONE):
            self.single_text = {scan.BLOCKED: TEXT_BLOCKED, scan.RUNNING: TEXT_BUSY}.get(st, TEXT_UNSUPPORTED)
            self.single_level = "warn"
        self._refresh()

    def resizeEvent(self, e: Any) -> None:  # noqa: N802
        super().resizeEvent(e)
        if self.toast.isVisible():
            par = self.toast.parentWidget()
            if par is not None:
                self.toast.move(max(8, (par.width() - self.toast.width()) // 2),
                                max(8, par.height() - self.toast.height() - 22))
