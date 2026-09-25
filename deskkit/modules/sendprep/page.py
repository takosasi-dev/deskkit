# Control Center の SendPrep 画面。投入エリア(ドロップ・ファイル選択)・プリセット・キューと結果の行・完了のまとめ・
# プリセットの管理・設定(日付の名前・伏せ字の既定・登録した言葉・「送る」)・この PC で使える機能の状態。
# ファイル名は画面にだけ出す(V-8)。シグナルから呼ぶ処理はすべて guard で例外を握る(ログには型名だけ)。
from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QDragEnterEvent, QDragMoveEvent, QDropEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from deskkit.modules.sendprep import config as cfgmod
from deskkit.modules.sendprep import jobs as J
from deskkit.modules.sendprep import sendto
from deskkit.modules.sendprep.module import MSG_QUEUE_FULL
from deskkit.modules.sendprep.redact import guard
from deskkit.modules.sendprep.video import STATE_EXTRACTED, STATE_MISSING, STATE_NOT_EXTRACTED
from deskkit.ui import theme as T
from deskkit.ui import widgets as W
from deskkit.ui.theme import G

if TYPE_CHECKING:
    from deskkit.modules.sendprep.module import SendPrepModule

FILE_FILTER = ("写真と動画 (*.jpg *.jpeg *.jpe *.jfif *.png *.webp *.heic *.heif *.bmp *.gif "
               "*.mp4 *.mov *.m4v *.mkv *.webm *.avi);;すべてのファイル (*.*)")
G_PHOTO = ""
G_VIDEO = ""


def _local_files(md: Any) -> list[str]:
    if md is None or not md.hasUrls():
        return []
    return [u.toLocalFile() for u in md.urls() if u.isLocalFile() and u.toLocalFile()]


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

    def set_color(self, color: str) -> None:
        self.setStyleSheet(f"QFrame {{ background: {T.alpha(color, 0.08)}; border: 1px solid {T.alpha(color, 0.30)}; border-radius: 10px; }}")
        self.glyph.set_color(color)
        self.glyph.setStyleSheet(f"color: {color}; background: transparent; border: none;")
        self.label.setStyleSheet(f"color: {T.TEXT_DIM}; background: transparent; border: none; font-size: 12px;")

    def set_text(self, text: str) -> None:
        self.label.setText(text)
        self.setVisible(bool(text))


class _DropZone(QFrame):
    """点線の枠の投入エリア。ページ全体がドロップを受けるので、ここは見た目と「ファイルを選ぶ」だけ。"""

    def __init__(self, accent: str, on_pick: Any) -> None:
        super().__init__()
        self._accent = accent
        self._hot = False
        self.setMinimumHeight(210)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(8)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        g = W.Glyph(G.DOWNLOAD, 26, accent)
        g.setFixedSize(52, 52)
        g.setStyleSheet(f"color: {accent}; background: {T.alpha(accent, 0.12)}; border-radius: 26px;")
        lay.addWidget(g, 0, Qt.AlignmentFlag.AlignCenter)
        t = W.label("写真や動画を、ここにドロップ", "H3")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(t)
        s = W.label("フォルダを落とすと、その中のファイルだけを積みます(中のフォルダは見ません)。", "Mute", wrap=True)
        s.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(s)
        b = W.button("ファイルを選ぶ", "secondary", G.FOLDER, on_click=on_pick)
        lay.addWidget(b, 0, Qt.AlignmentFlag.AlignCenter)

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
        pen = QPen(c, 2 if self._hot else 1.5, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawRoundedRect(r, 14, 14)
        p.end()


class _PresetChip(QPushButton):
    def __init__(self, label: str, sub: str, accent: str) -> None:
        super().__init__(f"{label}\n{sub}")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(54)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            f"QPushButton {{ background: {T.SURFACE2}; border: 1px solid {T.BORDER}; border-radius: 10px; padding: 6px 10px;"
            f" text-align: left; color: {T.TEXT_DIM}; font-weight: 600; }}"
            f"QPushButton:hover {{ border-color: {T.BORDER_HI}; }}"
            f"QPushButton:checked {{ background: {T.alpha(accent, 0.16)}; border: 1px solid {T.alpha(accent, 0.7)}; color: {T.TEXT}; }}")


class _JobRow(QFrame):
    def __init__(self, page: SendPrepPage, job: J.Job) -> None:
        super().__init__()
        self.page = page
        self.job = job
        self.setObjectName("Inset")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 10, 8)
        lay.setSpacing(10)
        acc = page.accent
        self.icon = W.Glyph(G_VIDEO if job.kind == "video" else G_PHOTO, 15, acc)
        self.icon.setFixedSize(32, 32)
        self.icon.setStyleSheet(f"color: {acc}; background: {T.alpha(acc, 0.12)}; border-radius: 9px;")
        lay.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        mid = QVBoxLayout()
        mid.setSpacing(2)
        self.name = QLabel()
        self.name.setMinimumWidth(80)
        self.name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.name.setToolTip(str(job.path))
        self.name.setText(job.path.name)
        mid.addWidget(self.name)
        self.status = W.label("", "Mute", wrap=True)
        mid.addWidget(self.status)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        self.bar.setVisible(False)
        mid.addWidget(self.bar)
        lay.addLayout(mid, 1)
        self.edit_btn = W.button("編集", "secondary", G.EDIT, on_click=guard(lambda: page.m.open_editor(self.job), "row:edit"))
        self.cancel_btn = W.button("中止", "danger", G.CLOSE, on_click=guard(lambda: page.m.cancel_job(self.job), "row:cancel"))
        self.remove_btn = W.icon_button(G.CLOSE, "一覧から外す", guard(lambda: page.m.remove_job(self.job), "row:remove"))
        for b in (self.edit_btn, self.cancel_btn, self.remove_btn):
            lay.addWidget(b, 0, Qt.AlignmentFlag.AlignVCenter)
        self.refresh()

    def refresh(self) -> None:
        j = self.job
        self.status.setText(j.result_text())
        color = {J.STATE_DONE: T.SUCCESS, J.STATE_FAILED: T.DANGER, J.STATE_CANCELLED: T.WARN}.get(j.state, T.TEXT_MUTE)
        self.status.setStyleSheet(f"color: {color}; font-size: 12px;")
        running_video = j.kind == "video" and j.state == J.STATE_RUNNING
        self.bar.setVisible(running_video and j.progress is not None)
        if j.progress is not None:
            self.bar.setValue(int(j.progress * 100))
        self.edit_btn.setVisible(j.kind == "image" and j.state == J.STATE_DONE)
        self.cancel_btn.setVisible(j.kind == "video" and j.state in (J.STATE_RUNNING, J.STATE_QUEUED))
        self.remove_btn.setVisible(j.state == J.STATE_PENDING or j.finished)
        if j.out_path is not None and j.state == J.STATE_DONE:
            self.name.setToolTip(f"{j.path}\n→ {j.out_path}")


class _Bridge(QObject):
    ocr = Signal(str)


class SendPrepPage(W.ScrollPage):
    def __init__(self, module: SendPrepModule) -> None:
        super().__init__()
        self.m = module
        self.accent = module.accent
        self._rows: dict[int, _JobRow] = {}
        self._preset = module.config.last_preset
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self._build_hero()
        self._build_tiles()
        self._build_input()
        self._build_queue()
        self._build_presets()
        self._build_options()
        self._build_parts()
        self.finish()
        s = module.signals
        s.changed.connect(guard(self._rebuild_rows, "page:changed"))
        s.job_updated.connect(guard(self._on_job, "page:job"))
        s.batch_done.connect(guard(self._refresh_summary, "page:batch"))
        s.info_changed.connect(guard(self._refresh_info, "page:info"))
        self._bridge = _Bridge()
        self._bridge.ocr.connect(guard(lambda _s: self._refresh_parts(), "page:ocr"))
        self._rebuild_rows()
        self._refresh_info()
        if module.ocr_state() == "untested":
            self._check_ocr_bg()

    # ================================================================ 共通
    def _parent(self) -> QWidget | None:
        return self.window()

    def _check_ocr_bg(self) -> None:
        bridge = self._bridge

        def work() -> None:
            try:
                st = self.m.check_ocr()
            except Exception:  # noqa: BLE001
                st = "unavailable"
            try:
                bridge.ocr.emit(st)
            except RuntimeError:
                pass

        threading.Thread(target=work, name="sendprep-ocrcheck", daemon=True).start()

    # ================================================================ ドロップ(FR-1)
    def dragEnterEvent(self, e: QDragEnterEvent) -> None:  # noqa: N802
        try:
            if _local_files(e.mimeData()):
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
                self.m.add_paths(files)
        except Exception as ex:  # noqa: BLE001
            import logging

            logging.getLogger("deskkit.sendprep").error("drop failed: %s", type(ex).__name__)

    def _pick(self) -> None:
        files, _f = QFileDialog.getOpenFileNames(self._parent(), "整えるファイルを選ぶ", "", FILE_FILTER)
        if files:
            self.m.add_paths(files)

    # ================================================================ ヒーロー
    def _build_hero(self) -> None:
        from deskkit.catalog import info

        hero = W.Hero("SendPrep", "写真・スクリーンショット・動画から隠れた情報を消し、送れる大きさにします。元のファイルは変えません。",
                      info("sendprep").glyph or G.SPARKLE, self.accent)
        self.state_pill = W.StatusPill("待機中", "off")
        hero.add_pill(self.state_pill)
        hero.add_action(W.button("ファイルを選ぶ", "primary", G.FOLDER, on_click=guard(self._pick, "hero:pick")))
        hero.add_action(W.button("クリップボードの画像", "secondary", G.CLIPBOARD,
                                 on_click=guard(self.m.open_clipboard_editor, "hero:clip"),
                                 tooltip="クリップボードの画像を1回だけ読み、伏せ字をして戻します"))
        self.add(hero)

    def _build_tiles(self) -> None:
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        self.t_queue = W.StatTile("キュー", "0", G.LIST, self.accent)
        self.t_done = W.StatTile("完了", "0", G.CHECK, T.SUCCESS)
        self.t_fail = W.StatTile("失敗", "0", G.WARNING, T.DANGER)
        self.t_today = W.StatTile("今日整えた", "0", G.SPARKLE, T.ACCENT)
        for t in (self.t_queue, self.t_done, self.t_fail, self.t_today):
            lay.addWidget(t, 1)
        self.add(box)

    # ================================================================ 投入とプリセット
    def _build_input(self) -> None:
        card = W.Card("送り先を選んで整える", "上限を超えるものだけを縮めます。どれを選んでも位置情報などは消えます。",
                      G.SPARKLE, self.accent)
        self.drop = _DropZone(self.accent, guard(self._pick, "drop:pick"))
        card.add(self.drop)
        self.chip_holder = QWidget()
        self.chip_grid = QGridLayout(self.chip_holder)
        self.chip_grid.setContentsMargins(0, 0, 0, 0)
        self.chip_grid.setHorizontalSpacing(8)
        self.chip_grid.setVerticalSpacing(8)
        self.chip_group = QButtonGroup(self)
        self.chip_group.setExclusive(True)
        card.add(self.chip_holder)
        row = QHBoxLayout()
        self.go_hint = W.label("", "Mute", wrap=True)
        row.addWidget(self.go_hint, 1)
        self.go_btn = W.button("整える", "primary", G.PLAY, on_click=guard(self._go, "go"))
        self.go_btn.setMinimumWidth(160)
        row.addWidget(self.go_btn)
        card.add_layout(row)
        self.add(card)

    def _rebuild_chips(self) -> None:
        for b in list(self.chip_group.buttons()):
            self.chip_group.removeButton(b)
            b.setParent(None)
            b.deleteLater()
        presets = self.m.config.presets
        if self.m.config.preset(self._preset) is None:
            self._preset = "meta"
        for i, p in enumerate(presets):
            chip = _PresetChip(p.label, p.describe(), self.accent)
            chip.setChecked(p.id == self._preset)
            chip.clicked.connect(guard(lambda _c=False, pid=p.id: self._select(pid), "chip"))
            self.chip_group.addButton(chip)
            self.chip_grid.addWidget(chip, i // 4, i % 4)
        for c in range(4):
            self.chip_grid.setColumnStretch(c, 1)

    def _select(self, pid: str) -> None:
        self._preset = pid
        self._refresh_go()

    def _go(self) -> None:
        self.m.start_processing(self._preset)

    def _refresh_go(self) -> None:
        n = len(self.m.pending_jobs())
        self.go_btn.setEnabled(n > 0)
        self.go_btn.setText(f"  整える({n} 件)" if n else "  整える")
        p = self.m.config.preset(self._preset)
        if n == 0:
            self.go_hint.setText("ファイルを積むと、ここから整えられます。")
        elif p is not None and p.limit_mb is None:
            self.go_hint.setText("大きさは変えず、位置情報などの隠れた情報だけを消します。")
        elif p is not None:
            self.go_hint.setText(f"1 ファイルずつ {p.limit_mb}MB 以下にします。収まらないものは作りません。")

    # ================================================================ キュー
    def _build_queue(self) -> None:
        card = W.Card("キューと結果", "できたファイルは元の写真の隣に「_share」を付けて保存します。", G.LIST, self.accent)
        self.clear_btn = W.button("一覧を片づける", "ghost", G.CLEAR, on_click=guard(self.m.clear_finished, "queue:clear"))
        card.add_header_widget(self.clear_btn)
        self.notice_limit = _Note(T.WARN, G.WARNING)
        self.notice_limit.set_text("")
        card.add(self.notice_limit)
        self.rows_holder = QWidget()
        self.rows_lay = QVBoxLayout(self.rows_holder)
        self.rows_lay.setContentsMargins(0, 0, 0, 0)
        self.rows_lay.setSpacing(6)
        card.add(self.rows_holder)
        self.empty = W.EmptyState(G_PHOTO, "まだ何もありません", "上の枠にファイルをドロップするか、「ファイルを選ぶ」を押してください。")
        card.add(self.empty)
        self.notice_unsupported = W.label("", "Mute")
        card.add(self.notice_unsupported)
        # 完了のまとめ(FR-18)
        self.summary = QFrame()
        self.summary.setObjectName("Inset")
        sl = QVBoxLayout(self.summary)
        sl.setContentsMargins(14, 10, 14, 10)
        sl.setSpacing(8)
        self.summary_label = W.label("", "H3")
        sl.addWidget(self.summary_label)
        br = QHBoxLayout()
        br.setSpacing(8)
        br.addWidget(W.button("フォルダを開く", "secondary", G.OPEN, on_click=guard(self.m.open_output_folder, "sum:open")))
        br.addWidget(W.button("出力をまとめてコピー", "primary", G.COPY, on_click=guard(self._copy_outputs, "sum:copy")))
        br.addStretch(1)
        sl.addLayout(br)
        self.copy_state = W.label("", "Mute", wrap=True)
        sl.addWidget(self.copy_state)
        self.summary.setVisible(False)
        card.add(self.summary)
        self.add(card)

    def _copy_outputs(self) -> None:
        n = self.m.copy_outputs()
        self.copy_state.setText(f"{n} 個のファイルをコピーしました。エクスプローラーや Discord に貼り付けられます。" if n
                                else "コピーできる出力がありません。")

    def _rebuild_rows(self) -> None:
        ids = [j.id for j in self.m.jobs]
        for jid in list(self._rows):
            if jid not in ids:
                row = self._rows.pop(jid)
                self.rows_lay.removeWidget(row)
                row.setParent(None)
                row.deleteLater()
        for i, j in enumerate(self.m.jobs):
            existing = self._rows.get(j.id)
            if existing is None:
                new_row = _JobRow(self, j)
                self._rows[j.id] = new_row
                self.rows_lay.insertWidget(i, new_row)
            else:
                existing.refresh()
        self._refresh_queue_meta()

    def _on_job(self, jid: int) -> None:
        row = self._rows.get(jid)
        if row is not None:
            row.refresh()
        self._refresh_queue_meta()

    def _refresh_queue_meta(self) -> None:
        jobs = self.m.jobs
        self.empty.setVisible(not jobs)
        self.rows_holder.setVisible(bool(jobs))
        self.clear_btn.setVisible(any(j.finished for j in jobs))
        self.notice_limit.set_text(MSG_QUEUE_FULL if self.m.queue_full else "")
        self.notice_unsupported.setText(f"対応していない形式: {self.m.unsupported} 件" if self.m.unsupported else "")
        self.notice_unsupported.setVisible(bool(self.m.unsupported))
        active = [j for j in jobs if j.state in (J.STATE_QUEUED, J.STATE_RUNNING)]
        self.t_queue.set_value(str(len(self.m.active_jobs())))
        self.t_done.set_value(str(sum(1 for j in jobs if j.state == J.STATE_DONE)))
        self.t_fail.set_value(str(sum(1 for j in jobs if j.state in (J.STATE_FAILED, J.STATE_CANCELLED))))
        if active:
            self.state_pill.set_state("info", f"処理中 {len(active)} 件")
        elif self.m.pending_jobs():
            self.state_pill.set_state("warn", f"待機中 {len(self.m.pending_jobs())} 件")
        else:
            self.state_pill.set_state("off", "待機中")
        self._refresh_go()
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        s = self.m.summary
        show = s is not None and not self.m.busy()
        self.summary.setVisible(bool(show))
        if s is not None and show:
            self.summary_label.setText(f"完了 {s.done} 件・失敗 {s.failed} 件")
        self.t_today.set_value(str(self.m.today_prepared()))

    # ================================================================ プリセットの管理(FR-5)
    def _build_presets(self) -> None:
        card = W.Card("プリセット", f"上限(1〜{cfgmod.LIMIT_MAX_MB}MB)を決めたプリセットを {cfgmod.MAX_CUSTOM_PRESETS} 個まで追加できます。"
                      "最初からある4つは変えられません。", G.SETTINGS, self.accent)
        self.custom_holder = QWidget()
        self.custom_lay = QVBoxLayout(self.custom_holder)
        self.custom_lay.setContentsMargins(0, 0, 0, 0)
        self.custom_lay.setSpacing(6)
        card.add(self.custom_holder)
        self.add_preset_btn = W.button("プリセットを追加", "secondary", G.ADD, on_click=guard(self._add_preset, "preset:add"))
        row = QHBoxLayout()
        row.addWidget(self.add_preset_btn)
        row.addStretch(1)
        card.add_layout(row)
        self.add(card)

    def _rebuild_custom(self) -> None:
        while self.custom_lay.count():
            it = self.custom_lay.takeAt(0)
            w = it.widget() if it is not None else None
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        customs = self.m.config.custom_presets()
        if not customs:
            self.custom_lay.addWidget(W.label("追加したプリセットはまだありません。", "Mute"))
        for p in customs:
            fr = QFrame()
            fr.setObjectName("Inset")
            lay = QHBoxLayout(fr)
            lay.setContentsMargins(12, 6, 8, 6)
            lay.setSpacing(8)
            name = QLabel(p.label)
            name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            lay.addWidget(name, 1)
            spin = QSpinBox()
            spin.setRange(cfgmod.LIMIT_MIN_MB, cfgmod.LIMIT_MAX_MB)
            spin.setValue(int(p.limit_mb or 1))
            spin.setSuffix(" MB")
            spin.setKeyboardTracking(False)
            spin.setMinimumWidth(110)
            spin.valueChanged.connect(guard(lambda v, pid=p.id: self._set_limit(pid, int(v)), "preset:limit"))
            lay.addWidget(spin)
            lay.addWidget(W.icon_button(G.EDIT, "名前を変える", guard(lambda pid=p.id, lb=p.label: self._rename(pid, lb), "preset:rename")))
            lay.addWidget(W.icon_button(G.DELETE, "削除", guard(lambda pid=p.id: self._delete(pid), "preset:delete")))
            self.custom_lay.addWidget(fr)
        self.add_preset_btn.setEnabled(len(customs) < cfgmod.MAX_CUSTOM_PRESETS)

    def _add_preset(self) -> None:
        from PySide6.QtWidgets import QDialog, QLineEdit

        dlg = W.StyledDialog(self._parent(), "プリセットを追加", G.ADD, self.accent, width=420)
        dlg.body.addWidget(W.label("名前", "Dim"))
        name = QLineEdit()
        name.setPlaceholderText("例: LINE")
        dlg.body.addWidget(name)
        dlg.body.addWidget(W.label("上限(MB)", "Dim"))
        spin = QSpinBox()
        spin.setRange(cfgmod.LIMIT_MIN_MB, cfgmod.LIMIT_MAX_MB)
        spin.setValue(25)
        spin.setSuffix(" MB")
        dlg.body.addWidget(spin)
        dlg.buttons.addWidget(W.button("キャンセル", "ghost", on_click=dlg.reject))
        dlg.buttons.addWidget(W.button("追加", "primary", on_click=dlg.accept))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        err = self.m.add_preset(name.text(), int(spin.value()))
        if err:
            W.message(self._parent(), "追加できませんでした", err, kind="error")

    def _rename(self, pid: str, label: str) -> None:
        v = W.text_input(self._parent(), "名前を変える", "新しい名前", label)
        if v is None:
            return
        err = self.m.rename_preset(pid, v)
        if err:
            W.message(self._parent(), "変えられませんでした", err, kind="error")

    def _set_limit(self, pid: str, v: int) -> None:
        err = self.m.set_preset_limit(pid, v)
        if err:
            W.message(self._parent(), "変えられませんでした", err, kind="error")

    def _delete(self, pid: str) -> None:
        p = self.m.config.preset(pid)
        ok, _ = W.confirm(self._parent(), "プリセットを削除", f"「{p.label if p else ''}」を削除します。", ok_text="削除", danger=True)
        if ok:
            self.m.delete_preset(pid)

    # ================================================================ 設定
    def _build_options(self) -> None:
        card = W.Card("設定", None, G.SETTINGS, self.accent)
        self.date_toggle = W.ToggleSwitch(self.m.config.rename_to_date, self.accent)
        self.date_toggle.toggled.connect(guard(self._set_date, "opt:date"))
        card.add(W.SettingRow("ファイル名を日付だけにする", "元の名前に人の名前や場所が入っていても、送り先に伝わりません"
                              "(例: share_20260925_101500.jpg)。", self.date_toggle))
        card.add(W.divider())
        self.style_seg = W.Segmented([("fill", "塗りつぶし"), ("mosaic", "モザイク")], self.m.config.redact_style, self.accent)
        self.style_seg.changed.connect(guard(self._set_style, "opt:style"))
        card.add(W.SettingRow("伏せ字の既定", "塗りつぶしは元に戻せません。細かいモザイクは読み取られることがあるので、"
                              "モザイクのブロックは大きめに固定しています。", self.style_seg))
        card.add(W.divider())
        self.sendto_toggle = W.ToggleSwitch(self.m.config.sendto_enabled, self.accent)
        self.sendto_toggle.toggled.connect(guard(self._set_sendto, "opt:sendto"))
        card.add(W.SettingRow("エクスプローラーの「送る」に追加", "ファイルを右クリック →「送る」→「SendPrep で整える」で積めます。",
                              self.sendto_toggle))
        self.sendto_note = W.label("", "Mute", wrap=True)
        card.add(self.sendto_note)
        card.add(W.divider())
        card.add(W.label("自動検出に使う言葉", "H3"))
        card.add(W.label(f"伏せ字エディタで、ここに入れた言葉(名前・住所など。1〜{cfgmod.WORD_MAX_CHARS} 文字、{cfgmod.MAX_WORDS} 個まで)も"
                         "候補として探します。", "Mute", wrap=True))
        self.words = W.StringListEditor(list(self.m.config.my_words), "言葉を追加", height=110)
        self.words.changed.connect(guard(self._set_words, "opt:words"))
        card.add(self.words)
        warn = _Note(T.WARN, G.LOCK)
        warn.set_text("ここに入れた言葉は、設定ファイルに暗号化せずに残ります。")
        card.add(warn)
        self.add(card)

    def _set_date(self, v: bool) -> None:
        err = self.m.update_settings(lambda s: s.__setitem__("rename_to_date", bool(v)))
        if err:
            W.message(self._parent(), "保存できませんでした", err, kind="error")

    def _set_style(self, v: str) -> None:
        self.m.update_settings(lambda s: s.__setitem__("redact_style", v))

    def _set_sendto(self, v: bool) -> None:
        err = self.m.set_sendto(bool(v))
        if err:
            self.sendto_toggle.set_checked_silent(not v)
            W.message(self._parent(), "「送る」", err, kind="error")
        self._refresh_sendto()

    def _set_words(self, items: list[str]) -> None:
        err = self.m.set_my_words(items)
        if err:
            W.message(self._parent(), "保存できませんでした", err, kind="error")
            self.words.set_items(list(self.m.config.my_words))

    def _refresh_sendto(self) -> None:
        st = self.m.sendto_status()
        text = {sendto.STATUS_REGISTERED: "「送る」に登録されています。",
                sendto.STATUS_OTHER: "「送る」に同じ名前の別のショートカットがあります(DeskKit は触りません)。"}.get(st, "")
        self.sendto_note.setText(text)
        self.sendto_note.setVisible(bool(text))

    # ================================================================ この PC で使える機能(FR-23 の画面版)
    def _build_parts(self) -> None:
        card = W.Card("この PC で使える機能", "動画の部品は、初めて動画を整えるときに用意します(初回だけ少し待ちます)。", G.INFO, self.accent)
        self.p_ffmpeg = W.StatusPill("", "off")
        self.p_h264 = W.StatusPill("", "off")
        self.p_ocr = W.StatusPill("", "off")
        card.add(W.SettingRow("動画の部品", None, self.p_ffmpeg))
        card.add(W.SettingRow("動画を縮める", None, self.p_h264))
        card.add(W.SettingRow("文字の自動検出", "使えない PC でも、伏せ字は手で囲めます。", self.p_ocr))
        self.add(card)

    def _refresh_parts(self) -> None:
        st = self.m.ffmpeg.state()
        self.p_ffmpeg.set_state(*{STATE_EXTRACTED: ("ok", "準備できています"), STATE_NOT_EXTRACTED: ("info", "まだ用意していません"),
                                  STATE_MISSING: ("error", "見つかりません")}.get(st, ("error", "壊れています")))
        h = self.m.ffmpeg.h264_state()
        self.p_h264.set_state(*{"available": ("ok", "使えます"), "unavailable": ("warn", "使えません")}.get(h, ("off", "まだ確かめていません")))
        o = self.m.ocr_state()
        self.p_ocr.set_state(*{"available": ("ok", "使えます"), "unavailable": ("warn", "使えません")}.get(o, ("off", "確かめています…")))

    def _refresh_info(self) -> None:
        self._rebuild_chips()
        self._rebuild_custom()
        self.date_toggle.set_checked_silent(self.m.config.rename_to_date)
        self.sendto_toggle.set_checked_silent(self.m.config.sendto_enabled)
        if self.style_seg.value() != self.m.config.redact_style:
            self.style_seg.set_value(self.m.config.redact_style)
        if self.words.items() != list(self.m.config.my_words):
            self.words.set_items(list(self.m.config.my_words))
        self._refresh_sendto()
        self._refresh_parts()
        self._refresh_go()
        self._refresh_summary()
