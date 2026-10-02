# Control Center の PlugSave 画面: Hero(状態・今すぐバックアップ・やめる)→ 進み具合 → お知らせ → 数字 → 初めてのバックアップの計画 →
# 前回の結果(飛ばした・失敗したファイルの表)→ コピー元 → バックアップ先のドライブ → 戻し方 → 設定。
# ラベル・ドライブ文字・パスは画面にだけ出す(V-8・B-14)。色は theme(T.*)と catalog のアクセント色を実行時に読む。
from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QMenu,
    QProgressBar,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from deskkit import catalog
from deskkit.modules.plugsave import drives
from deskkit.modules.plugsave.copier import STAGE_CLEAN, STAGE_COPY, STAGE_PLAN
from deskkit.modules.plugsave.module import WAITING, Candidate, Notice, human_bytes, unfinished_text
from deskkit.modules.plugsave.planner import FAILED_REASONS, REASON_TEXT
from deskkit.modules.plugsave.reminder import days_since, parse_ts
from deskkit.ui import theme as T
from deskkit.ui import widgets as W
from deskkit.ui.theme import G

if TYPE_CHECKING:
    from deskkit.modules.plugsave.module import PlugSaveModule

_log = logging.getLogger("deskkit.plugsave")
F = TypeVar("F", bound=Callable[..., Any])
RESTORE_HELP = ("戻すときは、「バックアップ先を開く」でフォルダを開き、戻したいファイルをコピーして元の場所へ貼り付けてください。\n"
                "上書きする前の古い版は「_以前の版」の中の、日時の名前のフォルダにあります。"
                "PlugSave はバックアップ先のファイルを消しません。_以前の版 は自動では消えません。")
KIND_PATHS = (("documents", "ドキュメント", QStandardPaths.StandardLocation.DocumentsLocation),
              ("pictures", "ピクチャ", QStandardPaths.StandardLocation.PicturesLocation),
              ("desktop", "デスクトップ", QStandardPaths.StandardLocation.DesktopLocation))


def _guard(fn: F) -> F:
    """シグナルから呼ぶ処理の例外を Qt へ漏らさない。ログには型名だけ(例外の文はパスを含みうる。INV-4)。"""

    @functools.wraps(fn)
    def wrapper(*a: Any, **k: Any) -> Any:
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001
            _log.warning("page handler %s failed: %s", getattr(fn, "__name__", "?"), type(e).__name__)
            return None

    return wrapper  # type: ignore[return-value]


def _acc() -> str:
    return catalog.info("plugsave").accent


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


def _ask(parent: QWidget | None, title: str, text: str, ok_text: str) -> bool:
    return bool(W.confirm(parent, title, text, ok_text=ok_text)[0])


def _duration(sec: int) -> str:
    if sec < 60:
        return f"{sec} 秒"
    m = sec // 60
    if m < 60:
        return f"{m} 分"
    return f"{m // 60} 時間 {m % 60} 分"


def _row_frame() -> tuple[QFrame, QVBoxLayout]:
    fr = QFrame()
    fr.setObjectName("Inset")
    lay = QVBoxLayout(fr)
    lay.setContentsMargins(12, 10, 12, 10)
    lay.setSpacing(6)
    return fr, lay


class PlugSavePage(W.ScrollPage):
    def __init__(self, module: PlugSaveModule) -> None:
        super().__init__()
        self.m = module
        acc = _acc()
        info = catalog.info("plugsave")
        # ---- Hero
        self.hero = W.Hero("PlugSave", info.tagline, info.glyph, acc)
        self.pill = W.StatusPill("", "ok")
        self.hero.add_pill(self.pill)
        self.now_btn = W.button("今すぐバックアップ", "primary", G.SAVE, self._backup_now)
        self.cancel_btn = W.button("やめる", "secondary", G.CLOSE, self._cancel)
        self.hero.add_action(self.now_btn)
        self.hero.add_action(self.cancel_btn)
        self.add(self.hero)
        # ---- 進み具合(FR-19)
        self.prog_card = W.Card(None, padding=14)
        self.stage_label = W.label("", "H3")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.eta_label = W.label("", "Mute")
        self.prog_card.add(self.stage_label)
        self.prog_card.add(self.bar)
        self.prog_card.add(self.eta_label)
        self.add(self.prog_card)
        # ---- お知らせ
        self.notice_card = W.Card(None, padding=14)
        self.notice_box = QVBoxLayout()
        self.notice_box.setSpacing(6)
        self.notice_card.add_layout(self.notice_box)
        self.add(self.notice_card)
        # ---- 数字
        self.t_last = W.StatTile("最後のバックアップ", "—", G.CLOCK, acc)
        self.t_src = W.StatTile("コピー元", "0", G.FOLDER, acc)
        self.t_drv = W.StatTile("つながっているドライブ", "0 / 0", G.SAVE, acc)
        tiles = QWidget()
        tiles.setLayout(W.hbox(self.t_last, self.t_src, self.t_drv, spacing=12))
        self.add(tiles)
        # ---- 初めてのバックアップ(FR-10)
        self.first_card = W.Card("初めてのバックアップ", "登録したドライブへの最初の回は、何をコピーするかを確かめてから始めます。",
                                 G.SPARKLE, acc)
        self.first_box = QVBoxLayout()
        self.first_box.setSpacing(8)
        self.first_card.add_layout(self.first_box)
        self.add(self.first_card)
        # ---- 前回の結果(FR-23)
        self.res_card = W.Card("前回の結果", None, G.LIST, acc)
        self.res_card.add_header_widget(W.button("バックアップ先を開く", "secondary", G.OPEN, self._open_folder))
        self.res_summary = W.label("まだバックアップしていません。", "Dim", wrap=True)
        self.res_card.add(self.res_summary)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["ファイル", "理由"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.setMinimumHeight(180)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.res_card.add(self.table)
        self.add(self.res_card)
        # ---- コピー元(FR-1〜FR-3)
        self.src_card = W.Card("コピー元", "バックアップするフォルダです(10 個まで)。", G.FOLDER, acc)
        self.add_src_btn = W.button("コピー元を足す", "secondary", G.ADD, self._add_source_menu)
        self.src_card.add_header_widget(self.add_src_btn)
        self.src_box = QVBoxLayout()
        self.src_box.setSpacing(6)
        self.src_card.add_layout(self.src_box)
        self.add(self.src_card)
        # ---- バックアップ先のドライブ(FR-4〜FR-6)
        self.drv_card = W.Card("バックアップ先のドライブ", "USB メモリ・外付けの SSD や HDD を 3 台まで登録できます。", G.SAVE, acc)
        self.reg_btn = W.button("ドライブを登録", "secondary", G.ADD, self._register_dialog)
        self.drv_card.add_header_widget(self.reg_btn)
        self.drv_box = QVBoxLayout()
        self.drv_box.setSpacing(6)
        self.drv_card.add_layout(self.drv_box)
        self.add(self.drv_card)
        # ---- 戻し方
        rc = W.Card("戻し方", None, G.UNDO, acc)
        rc.add(W.label(RESTORE_HELP, "Dim", wrap=True))
        self.add(rc)
        # ---- 設定
        sc = W.Card("設定", None, G.SETTINGS, acc)
        self.sw_auto = W.ToggleSwitch(bool(self.m.config["auto_start"]), acc)
        self.sw_auto.toggled.connect(_guard(lambda v: self._show_err(self.m.set_auto_start(bool(v)))))
        sc.add(W.SettingRow("挿したら自動で始める", "登録したドライブを挿すと、待ち時間のあとに始めます。一時停止中とゲーム中は始めません。",
                            self.sw_auto))
        self.delay = QSpinBox()
        self.delay.setRange(3, 60)
        self.delay.setSuffix(" 秒")
        self.delay.setValue(int(self.m.config["start_delay_s"]))
        self.delay.editingFinished.connect(_guard(lambda: self._show_err(self.m.set_int("start_delay_s", self.delay.value()))))
        sc.add(W.SettingRow("始めるまでの待ち時間", "この間に通知を押すと、やめられます。", self.delay))
        self.remind = QSpinBox()
        self.remind.setRange(0, 90)
        self.remind.setSuffix(" 日")
        self.remind.setSpecialValueText("知らせない")
        self.remind.setValue(int(self.m.config["remind_days"]))
        self.remind.editingFinished.connect(_guard(lambda: self._show_err(self.m.set_int("remind_days", self.remind.value()))))
        sc.add(W.SettingRow("しばらくバックアップしていないときに知らせる", "この日数を過ぎると、ドライブを挿すよう知らせます。", self.remind))
        self.add(sc)
        self.finish()
        s = self.m.signals
        s.state.connect(self._refresh_state)
        s.drives.connect(self._refresh_lists)
        s.settings.connect(self._refresh_lists)
        s.result.connect(self._refresh_result)
        s.notices.connect(self._refresh_notices)
        self._refresh_all()

    # ------------------------------------------------------------ 表示
    def _refresh_all(self) -> None:
        self._refresh_state()
        self._refresh_notices()
        self._refresh_lists()
        self._refresh_result()

    @_guard
    def _refresh_state(self) -> None:
        m = self.m
        r = m.run
        running = r is not None
        if running and r is not None and not r.plan_only:
            self.pill.set_state("warn", f"バックアップ中 {m.percent()}%")
        elif running:
            self.pill.set_state("info", "計画を作っています")
        elif m.state == WAITING:
            self.pill.set_state("warn", "始めるのを待っています")
        elif not m.config["drives"] or not m.config["sources"]:
            self.pill.set_state("off", "準備ができていません")
        else:
            self.pill.set_state("ok", m.status_text())
        self.cancel_btn.setVisible(running or m.pending is not None)
        self.now_btn.setEnabled(not running and bool(m.config["drives"]) and bool(m.config["sources"]))
        self.prog_card.setVisible(running or m.pending is not None)
        if r is not None:
            if r.stage in ("", STAGE_CLEAN):
                self.stage_label.setText("準備しています")
                self.bar.setRange(0, 0)
                self.eta_label.setText("")
            elif r.stage == STAGE_PLAN:
                self.stage_label.setText(f"調べています {r.done:,} 件")
                self.bar.setRange(0, 0)
                self.eta_label.setText("")
            elif r.stage == STAGE_COPY:
                self.bar.setRange(0, 1000)
                frac = (r.done_bytes / r.total_bytes) if r.total_bytes else ((r.done / r.total) if r.total else 0.0)
                self.bar.setValue(int(min(1.0, frac) * 1000))
                self.stage_label.setText(f"コピーしています {r.done:,} / {r.total:,}({human_bytes(r.done_bytes)} / "
                                         f"{human_bytes(r.total_bytes)})")
                eta = m.eta_seconds()
                self.eta_label.setText("残り時間を計っています" if eta is None else f"残り およそ {_duration(eta)}")
            if r.cancel.is_set():
                self.eta_label.setText("中止しています…")
        elif m.pending is not None:
            self.bar.setRange(0, 0)
            if m.pending.deferred:
                self.stage_label.setText("ゲーム・全画面が終わるのを待っています")
            else:
                self.stage_label.setText(f"{int(m.config['start_delay_s'])} 秒後にバックアップを始めます")
            self.eta_label.setText("やめるときは「やめる」を押してください。")
        self.t_last.set_value(self._last_text())

    def _last_text(self) -> str:
        d = self.m.days_since_success()
        if d is None:
            return "まだ"
        return "今日" if d == 0 else f"{d} 日前"

    @_guard
    def _refresh_notices(self) -> None:
        _clear(self.notice_box)
        items = list(self.m.notices)
        m = self.m
        if not m.config["sources"]:
            items.append(Notice("info", "コピー元を足してください。"))
        if not m.config["drives"]:
            items.append(Notice("info", "ドライブを登録してください。"))
        colors = {"ok": T.SUCCESS, "warn": T.WARN, "error": T.DANGER, "info": T.INFO}
        glyphs = {"ok": G.CHECK, "warn": G.WARNING, "error": G.ERROR, "info": G.INFO}
        for n in items:
            row = QWidget()
            lay = QHBoxLayout(row)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(8)
            lay.addWidget(W.Glyph(glyphs.get(n.kind, G.INFO), 14, colors.get(n.kind, T.INFO)), 0, Qt.AlignmentFlag.AlignTop)
            lay.addWidget(W.label(n.text, wrap=True), 1)
            self.notice_box.addWidget(row)
        last = m.last
        if last is not None and last.result == "no_space" and last.need_more and m.run is None:
            did = str(m.last_meta.get("drive_id", ""))
            self.notice_box.addLayout(W.hbox(
                W.button("入る分だけコピー", "primary", G.SAVE, lambda: self._show_err(m.start_backup(did, trigger="manual", fit=True))),
                W.button("バックアップ先を開く", "secondary", G.OPEN, self._open_folder), None))
        for did, p in m.mismatched.items():
            cfg = m.drive_cfg(did)
            name = (cfg or {}).get("label") or "登録したドライブ"
            self.notice_box.addWidget(W.label(f"{p.letter}: は「{name}」と同じ印を持つ、別のドライブです"
                                              f"(フォーマットし直したか、フォルダごと写したドライブかもしれません)。", wrap=True))
            self.notice_box.addLayout(W.hbox(W.button("このドライブを使う", "secondary", G.CHECK,
                                                      functools.partial(self._use_this, did)), None))
        self.notice_card.setVisible(self.notice_box.count() > 0)

    @_guard
    def _refresh_lists(self) -> None:
        m = self.m
        acc = _acc()
        # コピー元
        _clear(self.src_box)
        srcs = m.config["sources"]
        if not srcs:
            self.src_box.addWidget(W.EmptyState(G.FOLDER, "コピー元がありません", "「コピー元を足す」で、バックアップするフォルダを選んでください。"))
        for i, s in enumerate(srcs):
            fr, lay = _row_frame()
            path = W.label(s["path"], "Mute")
            path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lay.addLayout(W.hbox(W.Glyph(G.FOLDER, 15, acc), W.label(s["name"], "H3"), None,
                                 W.button("外す", "ghost", G.DELETE, functools.partial(self._remove_source, i)), spacing=8))
            lay.addWidget(path)
            lay.addWidget(W.label(f"バックアップ先では「{s['name']}」のフォルダに入ります。", "Mute"))
            self.src_box.addWidget(fr)
        self.add_src_btn.setEnabled(len(srcs) < 10)
        self.t_src.set_value(str(len(srcs)))
        # ドライブ
        _clear(self.drv_box)
        drvs = m.config["drives"]
        if not drvs:
            self.drv_box.addWidget(W.EmptyState(G.SAVE, "ドライブが登録されていません", "バックアップ用のドライブを挿して「ドライブを登録」を押してください。"))
        for d in drvs:
            self.drv_box.addWidget(self._drive_row(d))
        self.reg_btn.setEnabled(len(drvs) < 3)
        self.t_drv.set_value(f"{len(m.connected)} / {len(drvs)}")
        self._refresh_first()
        self._refresh_state()
        self._refresh_notices()

    def _drive_row(self, d: dict[str, Any]) -> QWidget:
        m = self.m
        acc = _acc()
        fr, lay = _row_frame()
        did = d["id"]
        p = m.connected.get(did)
        mis = m.mismatched.get(did)
        label = d.get("label") or "(ラベルなし)"
        head: list[QWidget | int | None] = [W.Glyph(G.SAVE, 15, acc), W.label(label, "H3")]
        if p is not None:
            head.append(_chip(f"{p.letter}: につながっています", T.SUCCESS))
        elif mis is not None:
            head.append(_chip("別のドライブ", T.WARN))
        else:
            head.append(_chip("つながっていません", T.TEXT_MUTE))
        if not d.get("first_done"):
            head.append(_chip("初回はまだ", T.INFO))
        head.append(None)
        if p is not None and d.get("first_done"):
            head.append(W.button("今すぐバックアップ", "secondary", G.SAVE, functools.partial(self._backup_drive, did)))
        head.append(W.button("登録を外す", "ghost", G.DELETE, functools.partial(self._unregister, did)))
        lay.addLayout(W.hbox(*head, spacing=8))
        ls = parse_ts(d.get("last_success_at"))
        if ls is None:
            when = "まだバックアップしていません"
        else:
            n = days_since(ls, m._now()) or 0
            when = f"最後のバックアップ: {ls.strftime('%Y/%m/%d %H:%M')}({'今日' if n == 0 else f'{n} 日前'})"
        extra = ""
        if p is not None and p.volume is not None:
            extra = f" ・ {p.volume.fs}"
        lay.addWidget(W.label(when + extra, "Mute"))
        return fr

    @_guard
    def _refresh_first(self) -> None:
        m = self.m
        _clear(self.first_box)
        shown = False
        for d in m.config["drives"]:
            if d.get("first_done") or d["id"] not in m.connected:
                continue
            shown = True
            did = d["id"]
            p = m.connected[did]
            fr, lay = _row_frame()
            lay.addWidget(W.label(f"{p.letter}: {d.get('label') or '(ラベルなし)'}", "H3"))
            pv = m.previews.get(did)
            r = m.run
            if r is not None and r.drive_id == did and r.plan_only:
                lay.addWidget(W.label("何をコピーするか調べています…", "Dim"))
            elif pv is None:
                lay.addWidget(W.label("まず、何をコピーするかを調べます。コピー元のファイルは読むだけで、変えません。", "Dim", wrap=True))
                lay.addLayout(W.hbox(W.button("計画を作る", "primary", G.SEARCH, functools.partial(self._preview, did)), None))
            elif pv.result != "ok":
                lay.addWidget(W.label("調べられませんでした。ドライブとコピー元を確かめて、もう一度お試しください。", "Dim", wrap=True))
                lay.addLayout(W.hbox(W.button("もう一度調べる", "secondary", G.REFRESH, functools.partial(self._preview, did)), None))
            else:
                skipped = pv.skipped_total + pv.failed
                lay.addWidget(W.label(f"コピーするファイル: {pv.plan_new + pv.plan_changed:,} 件(合計 {human_bytes(pv.plan_bytes)})", "H3"))
                lay.addWidget(W.label(f"すでに同じもの: {pv.unchanged:,} 件 ・ 飛ばすもの: {skipped:,} 件 ・ "
                                      f"ドライブの空き: {human_bytes(pv.free)}", "Mute", wrap=True))
                if pv.plan_verify:
                    lay.addWidget(W.label(f"前回は途中で止まったので、その回に書いた小さいファイル {pv.plan_verify:,} 件も中身を確かめます。",
                                          "Mute", wrap=True))
                if pv.truncated:
                    lay.addWidget(W.label("ファイルが多いため、1回目は一部だけをコピーし、残りは次の回にコピーします。", "Mute", wrap=True))
                if pv.need_more:
                    lay.addWidget(W.label(f"空きが足りません。あと {human_bytes(pv.need_more)} 要ります。", "Mute", wrap=True))
                lay.addWidget(W.label("初回は時間がかかることがあります。途中で「やめる」を押しても、コピーの済んだファイルは残ります。",
                                      "Mute", wrap=True))
                lay.addLayout(W.hbox(W.button("始める", "primary", G.PLAY, functools.partial(self._start_first, did)),
                                     W.button("調べ直す", "secondary", G.REFRESH, functools.partial(self._preview, did)), None))
            self.first_box.addWidget(fr)
        self.first_card.setVisible(shown)

    @_guard
    def _refresh_result(self) -> None:
        m = self.m
        self._refresh_first()
        out = m.last
        self.table.setRowCount(0)
        if out is None:
            self.res_summary.setText("まだバックアップしていません。")
            self.table.setVisible(False)
            return
        parts = [f"新しくコピー: {out.new:,} 件", f"変わったのでコピー: {out.changed:,} 件(古い版を移した数: {out.moved_old:,})",
                 f"そのまま: {out.unchanged:,} 件"]
        line = unfinished_text(out)
        if line:
            parts.insert(0, line)
        if out.recopied:
            parts.insert(3, f"中身が欠けていたのでコピーし直した: {out.recopied:,} 件(古い方は _以前の版 へ移しました)")
        for code, n in sorted(out.skipped.items()):
            parts.append(f"飛ばした({REASON_TEXT.get(code, code)}): {n:,} 件")
        if out.failed:
            parts.append(f"失敗: {out.failed:,} 件")
        if out.left_out:
            parts.append(f"空きが足りず今回コピーしなかった: {out.left_out:,} 件")
        parts.append(f"コピーした大きさ: {human_bytes(out.bytes)} ・ かかった時間: {_duration(out.ms // 1000)}")
        self.res_summary.setText("\n".join(parts))
        rows = [(p, r) for p, r in out.rows]
        rows += [(p, "utime") for p in out.utime_failed]
        self.table.setVisible(bool(rows))
        self.table.setRowCount(len(rows))
        for i, (path, code) in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(path))
            text = "日時を写せませんでした(コピーは済み)" if code == "utime" else REASON_TEXT.get(code, code)
            if code in FAILED_REASONS:
                text = "失敗: " + text
            self.table.setItem(i, 1, QTableWidgetItem(text))

    # ------------------------------------------------------------ 操作
    def _show_err(self, err: str | None) -> None:
        if err:
            W.message(self.window(), "PlugSave", err, kind="warn")

    @_guard
    def _backup_now(self) -> None:
        self.m.backup_now()

    @_guard
    def _backup_drive(self, did: str) -> None:
        self._show_err(self.m.backup_now(did))

    @_guard
    def _cancel(self) -> None:
        self.m.cancel_any()

    @_guard
    def _preview(self, did: str) -> None:
        self._show_err(self.m.make_preview(did))
        self._refresh_first()

    @_guard
    def _start_first(self, did: str) -> None:
        self._show_err(self.m.start_backup(did, trigger="first"))

    @_guard
    def _open_folder(self) -> None:
        self._show_err(self.m.open_backup_folder())

    @_guard
    def _use_this(self, did: str) -> None:
        if _ask(self.window(), "このドライブを使う", "このドライブを、登録したドライブとして使います。よろしいですか。", "使う"):
            self._show_err(self.m.use_this_drive(did))
            self._refresh_notices()

    @_guard
    def _remove_source(self, index: int) -> None:
        srcs = self.m.config["sources"]
        if not 0 <= index < len(srcs):
            return
        if _ask(self.window(), "コピー元を外す", f"「{srcs[index]['name']}」をコピー元から外します。"
                     "バックアップ先にコピー済みのファイルはそのまま残ります。", "外す"):
            self._show_err(self.m.remove_source(index))

    @_guard
    def _unregister(self, did: str) -> None:
        if _ask(self.window(), "登録を外す", "このドライブの登録を外します。ドライブの中の DeskKitバックアップ フォルダはそのまま残ります。",
                     "外す"):
            self._show_err(self.m.unregister_drive(did))

    @_guard
    def _add_source_menu(self) -> None:
        menu = QMenu(self)
        for kind, label, loc in KIND_PATHS:
            path = QStandardPaths.writableLocation(loc)
            if path:
                menu.addAction(f"{label}({path})", functools.partial(self._add_source, path, kind))
        menu.addSeparator()
        menu.addAction("フォルダを選ぶ…", self._pick_folder)
        menu.exec(self.add_src_btn.mapToGlobal(self.add_src_btn.rect().bottomLeft()))

    @_guard
    def _pick_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self.window(), "コピー元のフォルダを選ぶ")
        if path:
            self._add_source(path, None)

    @_guard
    def _add_source(self, path: str, kind: str | None) -> None:
        chk = self.m.check_source(path)
        if chk.status == "drive_root":
            if not _ask(self.window(), "ドライブの直下を足す", "ドライブの直下は、時間がかかります。続けますか。", "足す"):
                return
            self._show_err(self.m.add_source(path, kind, confirmed_root=True))
            return
        self._show_err(self.m.add_source(path, kind))

    @_guard
    def _register_dialog(self) -> None:
        self.reg_btn.setEnabled(False)
        self.reg_btn.setText("調べています…")

        def done(cands: list[Candidate]) -> None:
            self.reg_btn.setText("ドライブを登録")
            self.reg_btn.setEnabled(len(self.m.config["drives"]) < 3)
            self.candidates_dialog(cands).exec()

        self.m.list_candidates(_guard(done))

    def candidates_dialog(self, cands: list[Candidate]) -> W.StyledDialog:
        """FR-4 の候補の一覧のダイアログを作る(表示は呼ぶ側)。"""
        dlg = W.StyledDialog(self.window(), "ドライブを登録", G.SAVE, _acc(), width=560)
        box = dlg.body
        if not cands:
            box.addWidget(W.label("登録できるドライブが見つかりません。USB メモリか外付けのドライブを挿してから、もう一度押してください。"
                                  "システムのドライブと、コピー元のあるドライブは選べません。", "Dim", wrap=True))
        for c in cands:
            fr, lay = _row_frame()
            kind = "USB メモリなど" if c.drive_type == 2 else "外付け・内蔵のドライブ"
            state = {"new": "", "reuse": "前に PlugSave が使った印があります(その印を使います)",
                     "registered": "登録済みです", "broken": "印を読めません"}[c.state]
            btn = W.button("登録する", "primary", G.ADD, functools.partial(self._do_register, dlg, c))
            btn.setEnabled(c.state != "registered")
            lay.addLayout(W.hbox(W.label(f"{c.letter}: {c.label or '(ラベルなし)'}", "H3"), None, btn, spacing=8))
            lay.addWidget(W.label(f"{kind} ・ {c.fs} ・ 全体 {human_bytes(c.total)} ・ 空き {human_bytes(c.free)}", "Mute"))
            if state:
                lay.addWidget(W.label(state, "Mute"))
            box.addWidget(fr)
        dlg.buttons.addWidget(W.button("閉じる", "secondary", None, dlg.reject))
        return dlg

    @_guard
    def _do_register(self, dlg: Any, c: Candidate) -> None:
        replace = False
        if c.state == "broken":
            if not _ask(self.window(), "印を書き直す", "このドライブの印を読めません。新しい印で登録し直します。"
                             "前のバックアップはそのまま残ります。", "登録し直す"):
                return
            replace = True

        def done(err: str | None) -> None:
            if err:
                self._show_err(err)
                return
            dlg.accept()
            W.message(self.window(), "登録しました", f"{c.letter}: を登録しました。ドライブの中に「{drives.BACKUP_DIR}」フォルダを作りました。"
                      "初めてのバックアップは、この画面の「計画を作る」から始めてください。", kind="info")

        self.m.register_drive(c.letter, replace_broken=replace, done=_guard(done))

