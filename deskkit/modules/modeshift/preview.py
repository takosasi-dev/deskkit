# プレビュー窓(§9.5 / FR-7 / FR-8): Plan を「# | 種別 | 対象 | 現在 | 変更後 | 予定」の表で見せ、「実行」「キャンセル」を置く。
# 実行後は同じ窓で Step ごとの結果(成功/スキップ/失敗/終了せず)をアニメーション付きで表示する。
# 枠なし・角丸・影・フェード+スライドで出る独立ウィンドウ。モード選択の小窓(トレイの「プレビュー…」)もここ。
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    Qt,
)
from PySide6.QtGui import QColor, QGuiApplication, QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from deskkit.modules.modeshift.model import PLAN_COLUMNS, Plan, Step
from deskkit.modules.modeshift.visuals import (
    TYPE_GLYPHS,
    Banner,
    Chip,
    ResultStat,
    accent,
    guard,
    mode_accent,
    result_style,
)
from deskkit.ui import theme as T
from deskkit.ui import widgets as W
from deskkit.ui.theme import G

if TYPE_CHECKING:
    from deskkit.modules.modeshift.module import ModeShiftModule

log = logging.getLogger("deskkit.modeshift")
WINDOW_FLAGS = Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint


class _FloatingPanel(QWidget):
    """枠なし・角丸・影付きの独立ウィンドウの土台(ドラッグで移動、Esc で閉じる)。"""

    def __init__(self, width: int) -> None:
        super().__init__(None, WINDOW_FLAGS)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(30, 30, 30, 30)
        self.panel = QFrame()
        self.panel.setObjectName("MsPanel")
        self.panel.setStyleSheet(f"QFrame#MsPanel {{ background: {T.BG1}; border: 1px solid {T.BORDER_HI}; border-radius: 18px; }}")
        W.shadow(self.panel, 48, 170, 14)
        self.panel.setMinimumWidth(width)
        outer.addWidget(self.panel)
        self._drag: QPoint | None = None
        self._anim: QParallelAnimationGroup | None = None

    def show_animated(self) -> None:
        self.adjustSize()
        scr = QGuiApplication.primaryScreen()
        if scr is not None:
            g = scr.availableGeometry()
            w = min(self.width(), g.width() - 40)
            h = min(self.height(), g.height() - 40)
            self.resize(w, h)
            end = QPoint(g.x() + (g.width() - w) // 2, g.y() + max(20, (g.height() - h) // 3))
        else:
            end = QPoint(100, 100)
        self.move(end + QPoint(0, 22))
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self.activateWindow()
        fade = QPropertyAnimation(self, b"windowOpacity", self)
        fade.setDuration(200)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        slide = QPropertyAnimation(self, b"pos", self)
        slide.setDuration(280)
        slide.setStartValue(end + QPoint(0, 22))
        slide.setEndValue(end)
        slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        grp = QParallelAnimationGroup(self)
        grp.addAnimation(fade)
        grp.addAnimation(slide)
        grp.start()
        self._anim = grp

    def keyPressEvent(self, e: QKeyEvent) -> None:
        try:
            if e.key() == Qt.Key.Key_Escape:
                self.close()
                return
        except Exception:  # noqa: BLE001
            log.exception("keyPressEvent")
        super().keyPressEvent(e)

    def mousePressEvent(self, e: Any) -> None:
        try:
            if e.button() == Qt.MouseButton.LeftButton:
                self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        except Exception:  # noqa: BLE001
            log.exception("mousePressEvent")

    def mouseMoveEvent(self, e: Any) -> None:
        try:
            if self._drag is not None:
                self.move(e.globalPosition().toPoint() - self._drag)
        except Exception:  # noqa: BLE001
            log.exception("mouseMoveEvent")

    def mouseReleaseEvent(self, _e: Any) -> None:
        self._drag = None


class PreviewWindow(_FloatingPanel):
    """Plan のプレビューと実行結果。プレビューで見せた Plan オブジェクトをそのまま実行する(INV-3)。"""

    def __init__(self, plan: Plan, *, accent: str, on_execute: Callable[[Plan], tuple[int, str]],
                 on_closed: Callable[[PreviewWindow], None] | None = None) -> None:
        super().__init__(860)
        self.plan = plan
        self._accent = accent
        self._on_execute = on_execute
        self._on_closed = on_closed
        self._chips: list[Chip] = []
        self._done_steps = 0
        self.setWindowTitle("ModeShift — " + plan.title)
        lay = QVBoxLayout(self.panel)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(14)

        # 見出し
        head = QHBoxLayout()
        head.setSpacing(12)
        ic = W.Glyph(G.UNDO if plan.kind == "undo" else G.MODE, 20, accent)
        ic.setFixedSize(44, 44)
        ic.setStyleSheet(f"color: {accent}; background: {T.alpha(accent, 0.16)}; border-radius: 13px;")
        head.addWidget(ic)
        tb = QVBoxLayout()
        tb.setSpacing(1)
        self.title = W.label(plan.title, "H2")
        tb.addWidget(self.title)
        self.sub = W.label(self._subtitle(), "Mute")
        tb.addWidget(self.sub)
        head.addLayout(tb, 1)
        self.pill = W.StatusPill("プレビュー", "info")
        head.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignTop)
        head.addWidget(W.icon_button(G.CLOSE, "閉じる (Esc)", self.close), 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(head)

        # 注意バナー
        self._confirm_banner: Banner | None = None
        if plan.kind == "switch" and plan.needs_confirmation:
            self._confirm_banner = Banner(G.SHIELD, "初回のため確認が必要",
                                 "このモードは新しいか、定義が変わっています。内容を確かめて「実行」を押すと確認済みになり、"
                                 "以後はホットキーや CLI からそのまま実行できます。", T.WARN)
            lay.addWidget(self._confirm_banner)
        if plan.in_game or any(s.reason == "ゲーム中" for s in plan.steps):
            lay.addWidget(Banner(G.GAME, "ゲーム中", "フォーカスを奪う手順(起動・終了・開く)はスキップします。電源と音量は変更します。", T.DANGER))
        if plan.kind == "undo":
            lay.addWidget(Banner(G.UNDO, "戻すのは電源プランと音量だけ",
                                 "起動・終了したアプリはそのままです。切替のあとに手で変えた項目は戻しません。", T.INFO))

        # 表
        self.table: QTableWidget | None = None
        if plan.steps:
            self.table = self._build_table()
            self.table.setMinimumHeight(min(46 + 42 * len(plan.steps), 430))
            lay.addWidget(self.table, 1)
        else:
            lay.addWidget(W.EmptyState(G.CHECK, "変更する項目はありません", "戻す対象の値が記録されていません。"))

        # 結果の集計(実行後に出す)
        self.results = QFrame()
        rl = QHBoxLayout(self.results)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(10)
        self._stats = {k: ResultStat(k) for k in ("ok", "skipped", "failed", "still_running")}
        for s in self._stats.values():
            rl.addWidget(s, 1)
        self.results.setMaximumHeight(0)
        lay.addWidget(self.results)

        # 下部
        foot = QHBoxLayout()
        foot.setSpacing(10)
        self.summary = W.label(self._plan_summary(), "Dim")
        foot.addWidget(self.summary, 1)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(180)
        self.progress.setRange(0, max(1, len(plan.steps)))
        self.progress.setValue(0)
        self.progress.setVisible(False)
        self.progress.setStyleSheet(f"QProgressBar::chunk {{ background: {accent}; border-radius: 4px; }}")
        foot.addWidget(self.progress)
        self.btn_cancel = W.button("キャンセル", "ghost", on_click=guard(self.close))
        self.btn_run = W.button("元に戻す" if plan.kind == "undo" else "実行", "primary",
                                G.UNDO if plan.kind == "undo" else G.PLAY, on_click=guard(self._execute))
        self.btn_run.setDefault(True)
        foot.addWidget(self.btn_cancel)
        foot.addWidget(self.btn_run)
        lay.addLayout(foot)
        self.msg = W.label("", wrap=True)
        self.msg.setStyleSheet(f"color: {T.WARN};")
        self.msg.setVisible(False)
        lay.addWidget(self.msg)
        self.resize(940, min(640, 300 + 44 * max(1, len(plan.steps))))

    # ---------------------------------------------------------------- 表
    def _subtitle(self) -> str:
        src = {"tray": "トレイ", "hotkey": "ホットキー", "gui": "画面", "cli": "CLI", "auto": "自動切替"}.get(self.plan.source, self.plan.source)
        return f"入口: {src}   ・   run_id {self.plan.run_id}"

    def _plan_summary(self) -> str:
        n = self.plan.planned_count()
        return f"実行 {n} 件 / スキップ {len(self.plan.steps) - n} 件(上から順に実行し、失敗しても続けます)"

    def _build_table(self) -> QTableWidget:
        t = QTableWidget(len(self.plan.steps), len(PLAN_COLUMNS))
        t.setHorizontalHeaderLabels(list(PLAN_COLUMNS))
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        t.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        t.setAlternatingRowColors(True)
        t.setShowGrid(False)
        t.setWordWrap(False)
        t.verticalHeader().setDefaultSectionSize(42)
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        t.setColumnWidth(0, 38)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        t.setColumnWidth(3, 170)
        t.setColumnWidth(4, 190)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)
        t.setColumnWidth(5, 200)
        for r, s in enumerate(self.plan.steps):
            num = QTableWidgetItem(str(s.index))
            num.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            num.setForeground(QColor(T.TEXT_MUTE))
            t.setItem(r, 0, num)
            t.setCellWidget(r, 1, self._type_cell(s))
            for c, text in ((2, s.target), (3, s.current), (4, s.new)):
                it = QTableWidgetItem(text)
                it.setToolTip(text)
                t.setItem(r, c, it)
            chip = Chip()
            self._set_plan_chip(chip, s)
            holder = QWidget()
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(6, 0, 6, 0)
            hl.addWidget(chip)
            hl.addStretch(1)
            t.setCellWidget(r, 5, holder)
            self._chips.append(chip)
        return t

    @staticmethod
    def _type_cell(s: Step) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(8, 0, 8, 0)
        lay.setSpacing(6)
        lay.addWidget(W.Glyph(TYPE_GLYPHS.get(s.type, G.MODE), 13, T.TEXT_DIM))
        lay.addWidget(QLabel(s.type_label))
        lay.addStretch(1)
        return w

    def _set_plan_chip(self, chip: Chip, s: Step) -> None:
        if s.planned:
            chip.set_kind("restore" if self.plan.kind == "undo" else "planned", ("戻す" if self.plan.kind == "undo" else "実行")
                          + (f"({s.reason})" if s.reason else ""))
        else:
            chip.set_kind("skipped", f"スキップ: {s.reason}" if s.reason else "スキップ")
        chip.setToolTip(s.plan_text())

    # ---------------------------------------------------------------- 実行
    def _execute(self) -> None:
        if self.plan.executed:
            return
        self.btn_run.setEnabled(False)
        self.btn_cancel.setEnabled(False)
        self.msg.setVisible(False)
        # 実行開始の表示は先に出す(完了の通知が同期で先に届いても上書きしないため)
        self.pill.set_state("info", "実行中…")
        self.btn_run.setText("実行中…")
        self.progress.setVisible(True)
        self.progress.setValue(0)
        for chip, s in zip(self._chips, self.plan.steps, strict=False):
            if s.planned:
                chip.set_kind("waiting")
        if self.table is not None:
            self.table.setHorizontalHeaderItem(5, QTableWidgetItem("結果"))
        code, text = self._on_execute(self.plan)
        if code != 0 and not self.plan.executed:
            self.pill.set_state("info", "プレビュー")
            self.btn_run.setText("元に戻す" if self.plan.kind == "undo" else "実行")
            self.btn_run.setEnabled(True)
            self.btn_cancel.setEnabled(True)
            self.progress.setVisible(False)
            for chip, s in zip(self._chips, self.plan.steps, strict=False):
                self._set_plan_chip(chip, s)
            if self.table is not None:
                self.table.setHorizontalHeaderItem(5, QTableWidgetItem("予定"))
            self.msg.setText(text)
            self.msg.setVisible(True)

    def step_updated(self, step: Step) -> None:
        i = step.index - 1
        if 0 <= i < len(self._chips) and step.result:
            chip = self._chips[i]
            chip.set_kind(step.result, step.result_text())
            chip.setToolTip(step.result_text())
            fx = QGraphicsOpacityEffect(chip)
            chip.setGraphicsEffect(fx)
            a = QPropertyAnimation(fx, b"opacity", chip)
            a.setDuration(260)
            a.setStartValue(0.15)
            a.setEndValue(1.0)
            a.setEasingCurve(QEasingCurve.Type.OutCubic)
            a.finished.connect(guard(lambda c=chip: c.setGraphicsEffect(None)))
            a.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
            item = self.table.item(i, 0) if self.table is not None else None
            if self.table is not None and item is not None:
                self.table.scrollToItem(item)
        self._done_steps += 1
        pa = QPropertyAnimation(self.progress, b"value", self.progress)
        pa.setDuration(220)
        pa.setStartValue(self.progress.value())
        pa.setEndValue(min(self._done_steps, self.progress.maximum()))
        pa.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)

    def finished(self) -> None:
        for s in self.plan.steps:
            if s.result is not None and s.index - 1 < len(self._chips):
                self._chips[s.index - 1].set_kind(s.result, s.result_text())
        c = self.plan.counts()
        bad = c["failed"] + c["still_running"] + c["aborted"]
        self.pill.set_state("warn" if bad else "ok", "一部うまくいかず" if bad else "完了")
        self.title.setText(self.plan.title + " — 完了")
        self.summary.setText("結果を確認してください。終了しなかったアプリはトレイに残っているか、保存の確認が出ている可能性があります。"
                             if c["still_running"] else "すべての手順を上から順に処理しました。")
        self.progress.setVisible(False)
        self.btn_run.setVisible(False)
        self.btn_cancel.setEnabled(True)
        self.btn_cancel.setText("閉じる")
        self.btn_cancel.setProperty("kind", "primary")
        self.btn_cancel.style().unpolish(self.btn_cancel)
        self.btn_cancel.style().polish(self.btn_cancel)
        if self._confirm_banner is not None:
            self._confirm_banner.setVisible(False)
        # 集計がせり出し、数字は数え上げ
        h = QPropertyAnimation(self.results, b"maximumHeight", self)
        h.setDuration(360)
        h.setStartValue(0)
        h.setEndValue(90)
        h.setEasingCurve(QEasingCurve.Type.OutBack)
        h.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
        for k, st in self._stats.items():
            st.num.run_to(c.get(k, 0))

    def closeEvent(self, e: Any) -> None:
        try:
            if self._on_closed is not None:
                self._on_closed(self)
        except Exception:  # noqa: BLE001
            log.exception("closeEvent")
        super().closeEvent(e)


class ModePicker(_FloatingPanel):
    """トレイの「プレビュー…」: どのモードをプレビューするか選ぶ小窓。"""

    def __init__(self, module: ModeShiftModule) -> None:
        super().__init__(360)
        self.setWindowTitle("ModeShift — プレビュー")
        svc = module.service
        lay = QVBoxLayout(self.panel)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(10)
        head = QHBoxLayout()
        acc = accent()
        g = W.Glyph(G.EYE, 18, acc)
        g.setFixedSize(36, 36)
        g.setStyleSheet(f"color: {acc}; background: {T.alpha(acc, 0.16)}; border-radius: 10px;")
        head.addWidget(g)
        tb = QVBoxLayout()
        tb.setSpacing(0)
        tb.addWidget(W.label("プレビュー", "H2"))
        tb.addWidget(W.label("何も変えずに、実行する手順を表で確かめます", "Mute"))
        head.addLayout(tb, 1)
        head.addWidget(W.icon_button(G.CLOSE, "閉じる (Esc)", self.close), 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(head)
        modes = svc.config.valid_modes() if svc else []
        if not modes:
            lay.addWidget(W.EmptyState(G.MODE, "モードがありません", "Control Center の ModeShift 画面でモードを作ってください。"))
        for m in modes:
            acc = mode_accent(m, m.index)
            b = W.button(m.label + ("" if m.confirmed else "  ・未確認"), "secondary", G.MODE,
                         on_click=guard(lambda n=m.name: self._pick(module, n)))
            b.setStyleSheet(f"QPushButton {{ text-align: left; padding: 10px 14px; border-left: 3px solid {acc}; }}")
            lay.addWidget(b)
        if svc is not None and svc.undo_info() is not None:
            lay.addWidget(W.divider())
            snap = svc.undo_info() or {}
            ub = W.button(f"元に戻す(「{snap.get('label_to')}」適用前へ)を確かめる", "ghost", G.UNDO,
                          on_click=guard(lambda: self._undo(module)))
            lay.addWidget(ub)

    def _pick(self, module: ModeShiftModule, name: str) -> None:
        self.close()
        module.switch(name, dry_run=True, source="tray")

    def _undo(self, module: ModeShiftModule) -> None:
        self.close()
        module.undo(dry_run=True, source="tray")


def result_color(kind: str) -> str:
    return result_style(kind)[0]
