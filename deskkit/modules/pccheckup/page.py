# Control Center の PcCheckup 画面: ヒーロー → 3つの大きなボタン(と「まとめて診断」)→ 進み具合と「中止」→
# 結果カード(悪い順。問題なしは折りたたみ)→ 空き容量の見張り → 履歴(status だけ)。
# 色は theme(T.*)と catalog のアクセント色を実行時に読む。status は色だけでなく字形と文字でも示す(P-1)。
from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypeVar

from PySide6.QtCore import QEasingCurve, QEvent, QPropertyAnimation, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from deskkit import catalog
from deskkit.modules.pccheckup.checks.base import (
    CATEGORY_LABELS,
    CATEGORY_TITLES,
    STATUS_ORDER,
    STATUS_TEXT,
    Action,
    Finding,
    Row,
    fmt_bytes,
)
from deskkit.modules.pccheckup.module import CATEGORY_GLYPHS
from deskkit.modules.pccheckup.runner import BY_CATEGORY
from deskkit.ui import theme as T
from deskkit.ui import widgets as W
from deskkit.ui.theme import G

if TYPE_CHECKING:
    from deskkit.modules.pccheckup.cleanup import CleanupResult, TempScan
    from deskkit.modules.pccheckup.module import PcCheckupModule

_log = logging.getLogger("deskkit.pccheckup")
F = TypeVar("F", bound=Callable[..., Any])

GLYPH_UNKNOWN = ""
GLYPH_DIAG = ""
HISTORY_ROWS = 8
CATEGORY_SUBS: dict[str, str] = {
    "perf": "CPU・メモリ・起動時のアプリ・電源などを調べます(約 10 秒)",
    "net": "Windows の接続の判定・ルーターの設定・電波を調べます(約 5 秒)",
    "storage": "空き・ごみ箱・一時ファイル・大きいフォルダを調べます(最大 60 秒)",
}


def _guard(fn: F) -> F:
    """シグナルから呼ぶ処理の例外を Qt へ漏らさない。ログには型名だけ(例外の文はパスを含みうる。INV-3)。"""

    @functools.wraps(fn)
    def wrapper(*a: Any, **k: Any) -> Any:
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001
            _log.warning("page handler %s failed: %s", getattr(fn, "__name__", "?"), type(e).__name__)
            return None

    return wrapper  # type: ignore[return-value]


def _acc() -> str:
    return catalog.info("pccheckup").accent


def status_style(status: str) -> tuple[str, str, str]:
    """(色, 字形, 文字)。色は実行時に theme から読む。"""
    return {
        "good": (T.SUCCESS, G.CHECK, STATUS_TEXT["good"]),
        "warn": (T.WARN, G.WARNING, STATUS_TEXT["warn"]),
        "bad": (T.DANGER, G.ERROR, STATUS_TEXT["bad"]),
        "info": (T.INFO, G.INFO, STATUS_TEXT["info"]),
        "unknown": (T.TEXT_MUTE, GLYPH_UNKNOWN, STATUS_TEXT["unknown"]),
    }.get(status, (T.TEXT_MUTE, GLYPH_UNKNOWN, status))


def _clear(lay: QLayout) -> None:
    while lay.count():
        it = lay.takeAt(0)
        w = it.widget() if it is not None else None
        if w is not None:
            w.hide()  # レイアウトから外しただけでは、消えるまで元の位置に見えてしまう
            w.setParent(None)
            w.deleteLater()
        elif it is not None:
            sub = it.layout()
            if sub is not None:
                _clear(sub)


def _fade_in(w: QWidget, ms: int = 260) -> None:
    eff = QGraphicsOpacityEffect(w)
    w.setGraphicsEffect(eff)
    a = QPropertyAnimation(eff, b"opacity", w)
    a.setDuration(ms)
    a.setStartValue(0.0)
    a.setEndValue(1.0)
    a.setEasingCurve(QEasingCurve.Type.OutCubic)
    a.finished.connect(lambda: w.setGraphicsEffect(None))  # type: ignore[arg-type]
    a.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)


# ------------------------------------------------------------------ 部品
class StatusBadge(QFrame):
    """status の字形+文字の札(色だけに頼らない。P-1)。"""

    def __init__(self, status: str) -> None:
        super().__init__()
        color, glyph, text = status_style(status)
        self.setObjectName("StatusBadge")
        self.setStyleSheet(f"QFrame#StatusBadge {{ background: {T.alpha(color, 0.13)}; border: 1px solid {T.alpha(color, 0.35)};"
                           f" border-radius: 11px; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 2, 10, 2)
        lay.setSpacing(4)
        g = W.Glyph(glyph, 12, color)
        g.setFixedSize(18, 18)
        g.set_color(color)
        lay.addWidget(g)
        self.text = QLabel(text)
        self.text.setStyleSheet(f"color: {color}; font-size: 12px; font-weight: 700; background: transparent;")
        lay.addWidget(self.text)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)


class BigButton(QFrame):
    """大きな押しボタン(字形・見出し・説明)。無効のときは薄くなる。"""

    clicked = Signal()

    def __init__(self, glyph: str, title: str, sub: str, color: str) -> None:
        super().__init__()
        self.setObjectName("BigButton")
        self._color = color
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setMinimumHeight(128)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(6)
        self.glyph = W.Glyph(glyph, 22, color)
        self.glyph.setFixedSize(44, 44)
        self.glyph.setStyleSheet(f"color: {color}; background: {T.alpha(color, 0.14)}; border-radius: 12px;")
        lay.addWidget(self.glyph)
        self.title = W.label(title, "H3", wrap=True)
        lay.addWidget(self.title)
        self.sub = W.label(sub, "Mute", wrap=True)
        lay.addWidget(self.sub)
        lay.addStretch(1)
        for w in (self.glyph, self.title, self.sub):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._restyle()

    def _restyle(self) -> None:
        c = self._color
        self.setStyleSheet(
            f"QFrame#BigButton {{ background: {T.SURFACE}; border: 1px solid {T.BORDER}; border-radius: 16px; }}"
            f"QFrame#BigButton:hover {{ background: {T.alpha(c, 0.08)}; border: 1px solid {T.alpha(c, 0.55)}; }}"
            f"QFrame#BigButton:focus {{ border: 1px solid {c}; }}"
            f"QFrame#BigButton:disabled {{ background: {T.SURFACE}; border: 1px solid {T.BORDER}; }}"
        )

    def changeEvent(self, e: Any) -> None:  # noqa: N802
        super().changeEvent(e)
        if e.type() != QEvent.Type.EnabledChange or not hasattr(self, "glyph"):
            return
        try:
            self.glyph.setGraphicsEffect(None)  # type: ignore[arg-type]
            if not self.isEnabled():
                eff = QGraphicsOpacityEffect(self)
                eff.setOpacity(0.45)
                self.glyph.setGraphicsEffect(eff)
        except RuntimeError:
            pass

    def mouseReleaseEvent(self, e: Any) -> None:  # noqa: N802
        try:
            if self.isEnabled() and e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
                self.clicked.emit()
        except RuntimeError:
            pass

    def keyPressEvent(self, e: Any) -> None:  # noqa: N802
        if e.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.isEnabled():
            self.clicked.emit()
            return
        super().keyPressEvent(e)


class FindingCard(QFrame):
    """1つのチェックの結果。左端の色帯・札・見出し・値・説明・一覧・次にやること。"""

    def __init__(self, f: Finding, worse: bool, on_action: Callable[[Action, QPushButton], None],
                 on_folder: Callable[[str], None]) -> None:
        super().__init__()
        self.finding = f
        color, _g, _t = status_style(f.status)
        self.setObjectName("FindingCard")
        self.setStyleSheet(f"QFrame#FindingCard {{ background: {T.SURFACE}; border: 1px solid {T.BORDER};"
                           f" border-left: 4px solid {color}; border-radius: 12px; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 14)
        lay.setSpacing(8)
        top = QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(StatusBadge(f.status), 0, Qt.AlignmentFlag.AlignTop)
        title = W.label(f.title, "H3", wrap=True)
        top.addWidget(title, 1)
        if f.value:
            v = QLabel(f.value)
            v.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            v.setStyleSheet(f"color: {color if f.status != 'good' else T.TEXT}; font-size: 15px; font-weight: 700;")
            top.addWidget(v, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(top)
        if worse:
            wp = W.StatusPill("前回より悪化", "error" if f.status == "bad" else "warn")
            wp.setText(f"{G.DOWN}  前回より悪化")
            wp.setFont(T.ui_font(12))
            lay.addWidget(wp, 0, Qt.AlignmentFlag.AlignLeft)
        if f.detail:
            lay.addWidget(W.label(f.detail, "Dim", wrap=True))
        if f.rows:
            lay.addWidget(self._rows(f.rows, on_folder))
        if f.actions:
            lay.addWidget(self._actions(f.actions, on_action))

    @staticmethod
    def _rows(rows: tuple[Row, ...], on_folder: Callable[[str], None]) -> QWidget:
        box = QFrame()
        box.setObjectName("Inset")
        g = QGridLayout(box)
        g.setContentsMargins(12, 8, 12, 8)
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(4)
        g.setColumnStretch(1, 1)
        for i, r in enumerate(rows):
            col = 0
            if r.status:
                color, glyph, text = status_style(r.status)
                gl = W.Glyph(glyph, 12, color)
                gl.setToolTip(text)
                gl.setFixedSize(18, 18)
                g.addWidget(gl, i, 0)
            col = 1
            name = W.label(r.label, None, wrap=True)
            g.addWidget(name, i, col)
            val = QLabel(r.value + (f"({r.note})" if r.note else ""))
            val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            val.setStyleSheet(f"color: {T.TEXT_DIM}; font-weight: 600;")
            g.addWidget(val, i, 2)
            if r.path:
                path = r.path
                b = W.icon_button(G.FOLDER, "フォルダを開く", _guard(lambda p=path: on_folder(p)))
                b.setFixedSize(28, 28)
                g.addWidget(b, i, 3)
        return box

    @staticmethod
    def _actions(actions: tuple[Action, ...], on_action: Callable[[Action, QPushButton], None]) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 2, 0, 0)
        v.setSpacing(6)
        head = W.label("次にやること", "Eyebrow")
        v.addWidget(head)
        for a in actions:
            row = QHBoxLayout()
            row.setSpacing(8)
            if a.text:
                gl = W.Glyph(G.CHEVRON, 11, _acc())
                gl.setFixedSize(16, 18)
                row.addWidget(gl, 0, Qt.AlignmentFlag.AlignTop)
                row.addWidget(W.label(a.text, None, wrap=True), 1)
            if a.button:
                btn = W.button(a.button, "secondary", _action_glyph(a))
                btn.clicked.connect(_guard(lambda _=False, aa=a, bb=btn: on_action(aa, bb)))
                if not a.text:
                    row.addWidget(btn)
                    row.addStretch(1)
                else:
                    row.addWidget(btn, 0, Qt.AlignmentFlag.AlignTop)
            v.addLayout(row)
        return box


def _action_glyph(a: Action) -> str:
    return {"uri": G.SETTINGS, "taskmgr": G.APP, "folder": G.FOLDER, "category": G.SEARCH, "cleanup": G.DELETE}.get(
        a.kind or "", G.OPEN)


class CategorySection(QWidget):
    """1カテゴリ分の結果。悪い順に並べ、問題なしは折りたたむ(FR-3)。"""

    def __init__(self, category: str, page: PcCheckupPage) -> None:
        super().__init__()
        self.category = category
        self.page = page
        self.cards: dict[str, FindingCard] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(10)
        gl = W.Glyph(CATEGORY_GLYPHS[category], 16, _acc())
        gl.setFixedSize(32, 32)
        gl.setStyleSheet(f"color: {_acc()}; background: {T.alpha(_acc(), 0.13)}; border-radius: 9px;")
        head.addWidget(gl)
        head.addWidget(W.label(CATEGORY_TITLES[category], "H2"))
        self.meta = W.label("", "Mute")
        head.addWidget(self.meta)
        head.addStretch(1)
        self.state_pill = W.StatusPill("調べています", "accent")
        head.addWidget(self.state_pill)
        lay.addLayout(head)
        self.main = QVBoxLayout()
        self.main.setSpacing(10)
        lay.addLayout(self.main)
        self.good_toggle = W.button("問題なしの項目(0 件)", "ghost", G.CHEVRON, on_click=_guard(self._toggle_good))
        self.good_toggle.setVisible(False)
        lay.addWidget(self.good_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.good_box = QWidget()
        self.good_lay = QVBoxLayout(self.good_box)
        self.good_lay.setContentsMargins(0, 0, 0, 0)
        self.good_lay.setSpacing(10)
        self.good_box.setVisible(False)
        lay.addWidget(self.good_box)
        self.good_open = False

    def add(self, f: Finding, worse: bool, animate: bool = True) -> None:
        old = self.cards.pop(f.check_id, None)
        if old is not None:
            old.hide()
            old.setParent(None)
            old.deleteLater()
        card = FindingCard(f, worse, self.page.on_action, self.page.on_folder)
        self.cards[f.check_id] = card
        self._relayout()
        if animate and (f.status != "good" or self.good_open):
            _fade_in(card)

    def _relayout(self) -> None:
        for lay in (self.main, self.good_lay):
            while lay.count():
                lay.takeAt(0)
        ordered = sorted(self.cards.values(), key=lambda c: (STATUS_ORDER[c.finding.status], c.finding.check_id))
        n_good = 0
        for c in ordered:
            if c.finding.status == "good":
                self.good_lay.addWidget(c)
                n_good += 1
            else:
                self.main.addWidget(c)
        self.good_toggle.setVisible(n_good > 0)
        self._set_toggle_text(n_good)

    def _set_toggle_text(self, n: int) -> None:
        glyph = G.UP if self.good_open else G.DOWN
        self.good_toggle.setText(f"{glyph}  問題なしの項目({n} 件)")

    def _toggle_good(self) -> None:
        self.good_open = not self.good_open
        self.good_box.setVisible(self.good_open)
        self._set_toggle_text(self.good_lay.count())
        if self.good_open:
            _fade_in(self.good_box, 200)

    def set_done(self, ms: int | None, cancelled: bool) -> None:
        if cancelled:
            self.state_pill.set_state("warn", "中止しました")
        else:
            self.state_pill.set_state("ok", "完了")
        self.meta.setText(f"{ms / 1000:.1f} 秒" if ms is not None else "")


# ------------------------------------------------------------------ 画面
class PcCheckupPage(W.ScrollPage):
    def __init__(self, module: PcCheckupModule) -> None:
        super().__init__()
        self.m = module
        self.sections: dict[str, CategorySection] = {}
        self._hist_count = 0
        self._build_hero()
        self._build_buttons()
        self._build_progress()
        self._build_results()
        self._build_watch()
        self._build_history()
        self.finish()
        n = module.notifier
        n.changed.connect(self._refresh_state)
        n.run_started.connect(self._on_run_started)
        n.finding.connect(self._on_finding)
        n.category_done.connect(self._on_category_done)
        n.run_finished.connect(self._on_run_finished)
        n.history_changed.connect(self._refresh_history)
        n.settings_changed.connect(self._refresh_watch)
        self._restore()
        self._refresh_state()
        self._refresh_history()

    # ================================================================ ヒーロー・ボタン
    def _build_hero(self) -> None:
        hero = W.Hero("PcCheckup", "困っていることのボタンを押すと、原因の候補と次にやることを示します。Windows の設定は変えません。",
                      catalog.info("pccheckup").glyph, _acc())
        self.state_pill = W.StatusPill("待機中", "off")
        self.watch_pill = W.StatusPill("空き容量を見張り中", "info")
        hero.add_pill(self.state_pill)
        hero.add_pill(self.watch_pill)
        self.all_btn = W.button("まとめて診断", "primary", GLYPH_DIAG, on_click=_guard(lambda: self.m.run(list(BY_CATEGORY))))
        self.all_btn.setToolTip("重い・ネット・容量の3つを続けて調べます")
        hero.add_action(self.all_btn)
        self.add(hero)

    def _build_buttons(self) -> None:
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        colors = {"perf": T.DANGER, "net": T.INFO, "storage": T.WARN}
        self.cat_btns: dict[str, BigButton] = {}
        for cat in BY_CATEGORY:
            b = BigButton(CATEGORY_GLYPHS[cat], CATEGORY_TITLES[cat], CATEGORY_SUBS[cat], colors[cat])
            b.clicked.connect(_guard(functools.partial(self.m.run, [cat])))
            lay.addWidget(b, 1)
            self.cat_btns[cat] = b
        self.add(box)

    def _build_progress(self) -> None:
        c = W.Card(None, padding=16)
        row = QHBoxLayout()
        row.setSpacing(12)
        self.spin = W.Glyph(G.REFRESH, 18, _acc())
        row.addWidget(self.spin, 0, Qt.AlignmentFlag.AlignTop)
        tb = QVBoxLayout()
        tb.setSpacing(2)
        self.prog_title = W.label("", "H3", wrap=True)
        self.prog_text = W.label("", "Dim", wrap=True)
        tb.addWidget(self.prog_title)
        tb.addWidget(self.prog_text)
        row.addLayout(tb, 1)
        self.cancel_btn = W.button("中止", "danger", G.CLOSE, on_click=_guard(self.m.cancel))
        self.cancel_btn.setToolTip("いま調べている項目が終わったところで止めます")
        row.addWidget(self.cancel_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        c.add_layout(row)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.bar.setStyleSheet(f"QProgressBar::chunk {{ background: {_acc()}; border-radius: 4px; }}")
        c.add(self.bar)
        self.progress_card = c
        c.setVisible(False)
        self.add(c)
        self._spin_timer = QTimer(self)
        self._spin_timer.setInterval(450)
        self._spin_timer.timeout.connect(self._tick_spin)
        self._spin_phase = 0

    @_guard
    def _tick_spin(self) -> None:
        self._spin_phase = (self._spin_phase + 1) % 2
        self.spin.set_color(_acc() if self._spin_phase else T.alpha(_acc(), 0.45))

    def _build_results(self) -> None:
        head = QWidget()
        h = QVBoxLayout(head)
        h.setContentsMargins(4, 6, 0, 0)
        h.setSpacing(6)
        row1 = QHBoxLayout()
        row1.setSpacing(10)
        row1.addWidget(W.label("結果", "H2"))
        self.when = W.label("", "Mute")
        row1.addWidget(self.when)
        row1.addStretch(1)
        self.copy_btn = W.button("結果をコピー", "secondary", G.COPY, on_click=_guard(self._copy))
        self.copy_btn.setToolTip("人に相談するときに貼り付けられる文にします。ユーザー名・SSID・PC 名は入れません")
        row1.addWidget(self.copy_btn)
        h.addLayout(row1)
        self.chips = QHBoxLayout()
        self.chips.setSpacing(8)
        h.addLayout(self.chips)
        self.results_head = head
        self.add(head)
        self.empty = W.EmptyState(G.SEARCH, "まだ診断していません",
                                  "上のボタンを押すと、原因の候補を調べて、次にやることを示します。")
        self.add(self.empty)
        self.results_box = QWidget()
        self.results_lay = QVBoxLayout(self.results_box)
        self.results_lay.setContentsMargins(0, 0, 0, 0)
        self.results_lay.setSpacing(22)
        self.add(self.results_box)

    def _build_watch(self) -> None:
        c = W.Card("空き容量の見張り", "オンにすると、6 時間ごとにシステムドライブの空きを読み、少なくなったら通知します。", G.EYE, _acc())
        self.watch_toggle = W.ToggleSwitch(self.m.watch_disk, _acc())
        self.watch_toggle.toggled.connect(self._on_watch)
        c.add(W.SettingRow("見張る", "通知は同じ状態で 24 時間に 1 回まで。一時停止中は読みません。", self.watch_toggle))
        self.add(c)

    def _build_history(self) -> None:
        c = W.Card("これまでの診断", "時刻・カテゴリ・判定の数だけを残しています(最新 200 件)。", G.CLOCK, _acc())
        self.hist_box = QVBoxLayout()
        self.hist_box.setSpacing(6)
        c.add_layout(self.hist_box)
        self.add(c)

    # ================================================================ 状態の反映
    @_guard
    def _refresh_state(self) -> None:
        st = self.m.state
        running = st.running
        for b in self.cat_btns.values():
            b.setEnabled(not running)
        self.all_btn.setEnabled(not running)
        self.progress_card.setVisible(running)
        self.copy_btn.setEnabled(not running and any(st.results.values()))
        if running:
            self.state_pill.set_state("accent", "診断中")
            if not self._spin_timer.isActive():
                self._spin_timer.start()
            total = sum(len(BY_CATEGORY[c]) for c in st.categories)
            done = sum(len(v) for v in st.results.values()) + sum(len(BY_CATEGORY[c]) - len(st.results.get(c, []))
                                                                  for c in st.cancelled)
            self.bar.setMaximum(max(1, total))
            self.bar.setValue(min(total, done))
            cat = st.progress_category or (st.categories[0] if st.categories else "")
            idx = st.categories.index(cat) + 1 if cat in st.categories else 1
            multi = f"({idx}/{len(st.categories)})" if len(st.categories) > 1 else ""
            self.prog_title.setText(f"「{CATEGORY_LABELS.get(cat, cat)}」を調べています{multi}")
            self.prog_text.setText(f"{st.step + 1}/{st.total}  {st.progress_text}" if st.progress_text and not st.cancel_requested
                                   else st.progress_text)
            self.cancel_btn.setEnabled(not st.cancel_requested)
        else:
            self._spin_timer.stop()
            c = st.counts()
            if not st.done:
                self.state_pill.set_state("off", "待機中")
            elif c["bad"]:
                self.state_pill.set_state("error", f"対処が必要 {c['bad']} 件")
            elif c["warn"]:
                self.state_pill.set_state("warn", f"注意 {c['warn']} 件")
            else:
                self.state_pill.set_state("ok", "大きな問題はありません")
        self.watch_pill.setVisible(self.m.watch_disk)
        has = any(st.results.values()) or running
        self.empty.setVisible(not has)
        self.results_head.setVisible(has)
        self._refresh_chips()
        if st.started is not None:
            self.when.setText(st.started.strftime("%m/%d %H:%M") + " に開始")

    def _refresh_chips(self) -> None:
        _clear(self.chips)
        c = self.m.state.counts()
        for s in ("bad", "warn", "unknown", "info", "good"):
            if c[s]:
                color, glyph, text = status_style(s)
                chip = QLabel(f"{glyph}  {text} {c[s]}")
                f = T.ui_font(12)
                f.setFamilies([*T.UI_FAMILIES[:2], T.icon_family(), *T.UI_FAMILIES[2:]])
                chip.setFont(f)
                chip.setStyleSheet(f"color: {color}; background: {T.alpha(color, 0.12)}; border: 1px solid {T.alpha(color, 0.3)};"
                                   f" border-radius: 11px; padding: 3px 10px; font-weight: 600;")
                self.chips.addWidget(chip)
        self.chips.addStretch(1)

    def _restore(self) -> None:
        """作り直された画面に、今ある結果を出す(アニメーションなし)。"""
        st = self.m.state
        if not st.categories:
            return
        self._on_run_started(st.categories)
        for cat in st.categories:
            for f in st.results.get(cat, []):
                self.sections[cat].add(f, f.check_id in st.worse, animate=False)
            if cat in st.done:
                self.sections[cat].set_done(st.ms.get(cat), cat in st.cancelled)

    @_guard
    def _on_run_started(self, cats: object) -> None:
        _clear(self.results_lay)
        self.sections.clear()
        for cat in list(cats) if isinstance(cats, list) else []:
            sec = CategorySection(cat, self)
            self.sections[cat] = sec
            self.results_lay.addWidget(sec)

    @_guard
    def _on_finding(self, cat: str, f: object) -> None:
        sec = self.sections.get(cat)
        if sec is not None and isinstance(f, Finding):
            sec.add(f, f.check_id in self.m.state.worse)
        self._refresh_chips()

    @_guard
    def _on_category_done(self, cat: str) -> None:
        sec = self.sections.get(cat)
        if sec is not None:
            sec.set_done(self.m.state.ms.get(cat), cat in self.m.state.cancelled)

    @_guard
    def _on_run_finished(self) -> None:
        self._refresh_state()

    # ================================================================ 操作
    def _parent(self) -> QWidget | None:
        return self.window()

    @_guard
    def _copy(self) -> None:
        text = self.m.copy_text()
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(text)
        self.copy_btn.setText(f"{G.CHECK}  コピーしました")
        QTimer.singleShot(1800, self._reset_copy)

    @_guard
    def _reset_copy(self) -> None:
        self.copy_btn.setText(f"{G.COPY}  結果をコピー")

    def on_action(self, a: Action, btn: QPushButton) -> None:
        if a.kind == "cleanup":
            self._start_cleanup(btn)
            return
        if not self.m.open_action(a) and a.kind != "category":
            W.message(self._parent(), "開けませんでした", "この画面を開けませんでした。", kind="warn")

    def on_folder(self, path: str) -> None:
        if not self.m.open_folder(path):
            W.message(self._parent(), "開けませんでした", "フォルダが見つかりませんでした。", kind="warn")

    # ---- FR-9 古い一時ファイルをごみ箱へ
    def _start_cleanup(self, btn: QPushButton) -> None:
        if self.m.cleaning:
            return
        self._cleanup_btn = btn
        self._cleanup_text = btn.text()
        btn.setEnabled(False)
        btn.setText("一時ファイルを数えています…")
        self.m.scan_temp(self._on_scanned)

    def _restore_cleanup_btn(self) -> None:
        try:
            self._cleanup_btn.setEnabled(True)
            self._cleanup_btn.setText(self._cleanup_text)
        except (RuntimeError, AttributeError):
            pass

    @_guard
    def _on_scanned(self, scan: TempScan | None) -> None:
        if scan is None:
            self._restore_cleanup_btn()
            W.message(self._parent(), "一時ファイルを数えられませんでした", "一時ファイルのフォルダを読めませんでした。", kind="warn")
            return
        if scan.count == 0:
            self._restore_cleanup_btn()
            W.message(self._parent(), "対象のファイルはありません",
                      "一時ファイルのフォルダに、7 日より前から更新されていないファイルはありませんでした。", kind="ok")
            return
        extra = "(時間がかかるため、途中まで数えた分です)" if scan.partial else ""
        ok, _ = W.confirm(
            self._parent(), "古い一時ファイルをごみ箱へ送りますか?",
            f"一時ファイルのフォルダ(%TEMP%)の、7 日より前から更新されていないファイル {scan.count} 個"
            f"(合計 {fmt_bytes(scan.bytes)}){extra}をごみ箱へ送ります。\n\n"
            "使用中のファイルは送りません。ごみ箱からいつでも元に戻せます。",
            ok_text="ごみ箱へ送る", glyph=G.DELETE)
        if not ok:
            self._restore_cleanup_btn()
            return
        try:
            self._cleanup_btn.setText("ごみ箱へ送っています…")
        except (RuntimeError, AttributeError):
            pass
        win = self.window()
        hwnd = int(win.winId()) if win is not None and win.isVisible() else None
        self.m.recycle_temp(scan, hwnd, self._on_recycled)

    @_guard
    def _on_recycled(self, res: CleanupResult) -> None:
        self._restore_cleanup_btn()
        lines = [f"{res.sent} 個をごみ箱へ送りました。"]
        if res.skipped:
            lines.append(f"使用中などで送れなかったファイル: {res.skipped} 個。")
        if res.sent:
            lines.append(f"ごみ箱を空にすると {fmt_bytes(res.bytes)} 空きます。")
        W.message(self._parent(), "ごみ箱へ送りました" if res.sent else "送れませんでした", "".join(lines),
                  kind="ok" if res.sent else "warn")

    # ---- 見張り
    @_guard
    def _on_watch(self, on: bool) -> None:
        err = self.m.set_watch(bool(on))
        if err:
            self.watch_toggle.set_checked_silent(self.m.watch_disk)
            W.message(self._parent(), "保存できませんでした", err, kind="error")

    @_guard
    def _refresh_watch(self) -> None:
        self.watch_toggle.set_checked_silent(self.m.watch_disk)
        self._refresh_state()

    # ---- 履歴
    def history_rows(self) -> int:
        return self._hist_count

    @_guard
    def _refresh_history(self) -> None:
        _clear(self.hist_box)
        rows = self.m.history.entries()[-HISTORY_ROWS:]
        self._hist_count = len(rows)
        if not rows:
            self.hist_box.addWidget(W.label("まだ履歴はありません。", "Mute"))
            return
        for r in reversed(rows):
            line = QHBoxLayout()
            line.setSpacing(10)
            try:
                t = datetime.fromisoformat(str(r["ts"])).strftime("%m/%d %H:%M")
            except ValueError:
                t = "—"
            tl = W.label(t, "Mute")
            tl.setMinimumWidth(80)
            line.addWidget(tl)
            cl = W.label(CATEGORY_LABELS.get(str(r["category"]), "?"))
            cl.setMinimumWidth(56)
            line.addWidget(cl)
            res = r["results"]
            for s in ("bad", "warn", "unknown", "info", "good"):
                n = sum(1 for v in res.values() if v == s)
                if n:
                    color, glyph, text = status_style(s)
                    lb = QLabel(f"{glyph} {text} {n}")
                    f = T.ui_font(12)
                    f.setFamilies([*T.UI_FAMILIES[:2], T.icon_family(), *T.UI_FAMILIES[2:]])
                    lb.setFont(f)
                    lb.setStyleSheet(f"color: {color};")
                    line.addWidget(lb)
            line.addStretch(1)
            self.hist_box.addLayout(line)
