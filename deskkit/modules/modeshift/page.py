# Control Center の ModeShift 画面: ヒーロー(現在のモード・最終切替・元に戻す)、数値タイル、モードタイル
# (現在のモードは光彩が脈打つ)、モード編集、動作設定(プレビュー方針・強制終了の許可・undo ホットキー)、
# 自動切替のルール編集、履歴(ops.jsonl の新しい順)。設定の変更はその場で保存して反映する。
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from deskkit.catalog import info as module_info
from deskkit.modules.modeshift.config import POLL_MAX_S
from deskkit.modules.modeshift.editor import ModeEditor, ProcessPicker
from deskkit.modules.modeshift.model import TYPE_LABELS
from deskkit.modules.modeshift.preview import result_color
from deskkit.modules.modeshift.visuals import ModeTile, TileGrid, guard, mode_accent
from deskkit.ui import theme as T
from deskkit.ui import widgets as W
from deskkit.ui.theme import G

if TYPE_CHECKING:
    from deskkit.modules.modeshift.module import ModeShiftModule

log = logging.getLogger("deskkit.modeshift")
SOURCE_LABELS = {"tray": "トレイ", "hotkey": "ホットキー", "cli": "CLI", "auto": "自動", "gui": "画面", "undo": "元に戻す"}
RESULT_LABELS = {"ok": "成功", "skipped": "スキップ", "failed": "失敗", "still_running": "終了せず", "aborted": "中断",
                 "planned": "予定"}


def _fmt_time(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    now = datetime.now(dt.tzinfo)
    if dt.date() == now.date():
        return dt.strftime("今日 %H:%M")
    return dt.strftime("%m/%d %H:%M")


class ModeShiftPage(W.ScrollPage):
    def __init__(self, module: ModeShiftModule) -> None:
        super().__init__()
        self.module = module
        self.svc = module.service
        self.info = module_info("modeshift")
        self._cfg_seen: Any = None
        self._hist_timer = QTimer(self)
        self._hist_timer.setSingleShot(True)
        self._hist_timer.setInterval(200)
        self._hist_timer.timeout.connect(guard(self._load_history))

        # ---- ヒーロー
        self.hero = W.Hero("ModeShift", self.info.tagline + "。電源プラン・音量・アプリをまとめて切り替え、電源と音量は元に戻せます。",
                           self.info.glyph, self.info.accent)
        self.pill_mode = W.StatusPill("", "off")
        self.pill_undo = W.StatusPill("", "off")
        self.pill_busy = W.StatusPill("切替中…", "info")
        for p in (self.pill_mode, self.pill_undo, self.pill_busy):
            self.hero.add_pill(p)
        self.btn_undo = W.button("元に戻す", "primary", G.UNDO, on_click=guard(lambda: module.undo(source="gui")))
        self.btn_undo_prev = W.button("戻す内容を確かめる", "ghost", G.EYE, on_click=guard(lambda: module.undo(dry_run=True, source="gui")))
        self.hero.add_action(self.btn_undo)
        self.hero.add_action(self.btn_undo_prev)
        self.add(self.hero)

        # ---- 数値タイル
        row = QHBoxLayout()
        row.setSpacing(12)
        self.t_mode = W.StatTile("現在のモード", "—", G.MODE, self.info.accent)
        self.t_last = W.StatTile("最後の切替", "—", G.CLOCK, T.INFO)
        self.t_count = W.StatTile("使えるモード", "0", G.LIST, T.SUCCESS)
        self.t_undo = W.StatTile("元に戻す", "—", G.UNDO, T.WARN)
        for t in (self.t_mode, self.t_last, self.t_count, self.t_undo):
            row.addWidget(t, 1)
        holder = QWidget()
        holder.setLayout(row)
        self.add(holder)

        # ---- モードタイル
        self.c_modes = W.Card("モード", "切り替えると、プレビューに出た通りの手順を上から順に実行します。未確認のモードは最初にプレビューで確認します。",
                              G.MODE, self.info.accent)
        self.grid = TileGrid()
        self.c_modes.add(self.grid)
        self.empty_modes = W.EmptyState(G.MODE, "まだモードがありません", "下の「モードの編集」で + を押して、最初のモードを作ってください。")
        self.c_modes.add(self.empty_modes)
        self.add(self.c_modes)

        # ---- モードの編集
        self.c_edit = W.Card("モードの編集", "表示名・ホットキー・色と、実行するアクションの列を編集します。定義を変えたモードは次の実行時にもう一度確認します。",
                             G.EDIT, self.info.accent)
        self.editor = ModeEditor(module)
        self.c_edit.add(self.editor)
        self.add(self.c_edit)

        # ---- 動作設定
        self.add(self._build_settings())
        self.add(self._build_auto())

        # ---- 履歴
        self.c_hist = W.Card("履歴", "ops.jsonl の新しい順(1手順1行・追記のみ)。", G.LOG, T.INFO)
        hh = QHBoxLayout()
        self.hist_filter = W.Segmented([("all", "すべて"), ("real", "実行"), ("dry", "dry-run")], "all", self.info.accent)
        self.hist_filter.changed.connect(guard(lambda _v: self._load_history()))
        hh.addWidget(self.hist_filter)
        hh.addStretch(1)
        hh.addWidget(W.button("更新", "ghost", G.REFRESH, on_click=guard(self._load_history)))
        self.c_hist.add_layout(hh)
        self.hist = QTableWidget(0, 8)
        self.hist.setHorizontalHeaderLabels(["時刻", "入口", "モード", "#", "種別", "対象", "結果", "理由"])
        self.hist.verticalHeader().setVisible(False)
        self.hist.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.hist.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.hist.setAlternatingRowColors(True)
        self.hist.setShowGrid(False)
        self.hist.setMinimumHeight(280)
        h = self.hist.horizontalHeader()
        for i in range(8):
            h.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.c_hist.add(self.hist)
        self.add(self.c_hist)
        self.finish()

        if self.svc is not None:
            self.svc.add_listener(self._on_changed)
            svc, cb = self.svc, self._on_changed
            self.destroyed.connect(lambda *_: svc.remove_listener(cb))
        self._on_changed()

    # ---------------------------------------------------------------- 動作設定
    def _build_settings(self) -> W.Card:
        c = W.Card("動作設定", None, G.SETTINGS, self.info.accent)
        cfg = self.svc.config if self.svc else None
        self.seg_preview = W.Segmented([("unconfirmed_only", "未確認のときだけ"), ("always", "毎回")],
                                       cfg.preview if cfg else "unconfirmed_only", self.info.accent)
        self.seg_preview.changed.connect(guard(lambda v: self._save_key("preview", v)))
        c.add(W.SettingRow("プレビュー", "トレイ・ホットキーから切り替えるとき、実行前に手順の表を出すか。CLI は常にそのまま実行します(未確認なら実行しません)。",
                           self.seg_preview, G.EYE))
        c.add(W.divider())
        self.tg_force = W.ToggleSwitch(bool(cfg and cfg.allow_force_kill), T.DANGER)
        self.tg_force.toggled.connect(guard(self._on_force_toggle))
        c.add(W.SettingRow("強制終了を許可", "閉じる要求に応じないアプリを、アクションで「強制終了する」を選び、毎回の確認ダイアログで承認したときだけ強制終了します。"
                           "未保存のデータは失われます。自動切替・CLI・ゲーム中からは強制終了しません。", self.tg_force, G.WARNING))
        c.add(W.divider())
        self.hk_undo = W.HotkeyEdit(cfg.undo_hotkey if cfg else "")
        self.hk_undo.changed.connect(guard(lambda t: self._save_key("undo_hotkey", t or None)))
        c.add(W.SettingRow("「元に戻す」のホットキー", "競合したら登録せずに知らせます(別のキーへ振り替えません)。", self.hk_undo, G.KEYBOARD))
        return c

    def _on_force_toggle(self, on: bool) -> None:
        if on:
            ok, _ = W.confirm(self, "強制終了を許可しますか?",
                              "許可しても、アクションごとに「強制終了する」を選び、実行のたびに確認ダイアログで承認したときだけ強制終了します。"
                              "強制終了したアプリの未保存のデータは失われます。", ok_text="許可する", danger=True)
            if not ok:
                self.tg_force.set_checked_silent(False)
                return
        self._save_key("allow_force_kill", bool(on))

    def _save_key(self, key: str, value: Any) -> None:
        sec = self.module.ctx.settings_dict()
        sec[key] = value
        err = self.module.save_section(sec)
        if err:
            W.message(self, "保存できませんでした", err, kind="error")

    # ---------------------------------------------------------------- 自動切替
    def _build_auto(self) -> W.Card:
        c = W.Card("自動切替(任意)", "指定した exe が起動したらモードを適用し、終了したら元に戻します。プロセス一覧(exe 名だけ)を一定間隔で見ます。",
                   G.LIGHTNING, self.info.accent)
        cfg = self.svc.config if self.svc else None
        self.auto_pill = W.StatusPill("", "off")
        c.add_header_widget(self.auto_pill)
        self.tg_auto = W.ToggleSwitch(bool(cfg and cfg.auto_enabled), self.info.accent)
        self.tg_auto.toggled.connect(guard(lambda on: self._save_auto(enabled=bool(on))))
        c.add(W.SettingRow("自動切替を使う", "未確認のモードは自動では実行せず、通知だけします。", self.tg_auto, G.LIGHTNING))
        self.sp_poll = QDoubleSpinBox()
        self.sp_poll.setRange(0, POLL_MAX_S)
        self.sp_poll.setDecimals(1)
        self.sp_poll.setSingleStep(0.5)
        self.sp_poll.setSuffix(" 秒")
        self.sp_poll.setSpecialValueText("未設定")
        self.sp_poll.setValue(float(cfg.poll_interval_s or 0) if cfg else 0)
        self.sp_poll.setMinimumWidth(120)
        self.sp_poll.editingFinished.connect(guard(lambda: self._save_auto(poll=self.sp_poll.value())))
        c.add(W.SettingRow("確認する間隔", "0.5 秒以上。実測で決めてください(短いほど早く気づき、CPU を少し多く使います)。未設定のままでは動きません。",
                           self.sp_poll, G.CLOCK))
        self.rules = QTableWidget(0, 3)
        self.rules.setHorizontalHeaderLabels(["起動を見張る exe", "適用するモード", "その exe が終了したら"])
        self.rules.verticalHeader().setVisible(False)
        self.rules.setMinimumHeight(150)
        self.rules.verticalHeader().setDefaultSectionSize(44)
        hh = self.rules.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        c.add(self.rules)
        self.rules_err = W.label("", wrap=True)
        self.rules_err.setStyleSheet(f"color: {T.DANGER};")
        c.add(self.rules_err)
        bar = QHBoxLayout()
        bar.addWidget(W.button("ルールを追加", "secondary", G.ADD, on_click=guard(self._add_rule)))
        bar.addWidget(W.button("動作中から追加…", "ghost", G.APP, on_click=guard(self._add_rule_from_proc)))
        bar.addWidget(W.button("選んだ行を削除", "ghost", G.DELETE, on_click=guard(self._del_rule)))
        bar.addStretch(1)
        bar.addWidget(W.button("ルールを保存", "primary", G.SAVE, on_click=guard(self._save_rules)))
        c.add_layout(bar)
        self._load_rules()
        return c

    def _rule_row(self, exe: str, mode: str, on_exit: str) -> None:
        r = self.rules.rowCount()
        self.rules.insertRow(r)
        e = QLineEdit(exe)
        e.setPlaceholderText("例: game.exe")
        self.rules.setCellWidget(r, 0, e)
        cb = QComboBox()
        cfg = self.svc.config if self.svc else None
        for m in (cfg.valid_modes() if cfg else []):
            cb.addItem(f"{m.label}({m.name})", m.name)
        if mode and cb.findData(mode) < 0:
            cb.addItem(f"(無い/無効) {mode}", mode)
        cb.setCurrentIndex(max(0, cb.findData(mode)))
        self.rules.setCellWidget(r, 1, cb)
        ox = QComboBox()
        ox.addItem("何もしない", "none")
        ox.addItem("元に戻す", "undo")
        ox.setCurrentIndex(1 if on_exit == "undo" else 0)
        self.rules.setCellWidget(r, 2, ox)

    def _load_rules(self) -> None:
        self.rules.setRowCount(0)
        sec = self.module.ctx.settings_dict()
        for r in (sec.get("auto_switch") or {}).get("rules") or []:
            if isinstance(r, dict):
                self._rule_row(str(r.get("exe") or ""), str(r.get("mode") or ""), str(r.get("on_exit") or "none"))

    def _add_rule(self) -> None:
        self._rule_row("", "", "undo")

    def _add_rule_from_proc(self) -> None:
        dlg = ProcessPicker(self, self.module, exclude_games=False, audio_first=False)
        if dlg.exec() and dlg.value():
            self._rule_row(dlg.value() or "", "", "undo")

    def _del_rule(self) -> None:
        r = self.rules.currentRow()
        if r >= 0:
            self.rules.removeRow(r)

    def _collect_rules(self) -> list[dict[str, Any]]:
        out = []
        for r in range(self.rules.rowCount()):
            e = self.rules.cellWidget(r, 0)
            m = self.rules.cellWidget(r, 1)
            o = self.rules.cellWidget(r, 2)
            exe = e.text().strip().lower() if isinstance(e, QLineEdit) else ""
            if not exe:
                continue
            out.append({"exe": exe, "mode": m.currentData() if isinstance(m, QComboBox) else "",
                        "on_exit": o.currentData() if isinstance(o, QComboBox) else "none"})
        return out

    def _save_rules(self) -> None:
        self._save_auto(rules=self._collect_rules())

    def _save_auto(self, *, enabled: bool | None = None, poll: float | None = None, rules: list[dict[str, Any]] | None = None) -> None:
        sec = self.module.ctx.settings_dict()
        auto = dict(sec.get("auto_switch") or {})
        if enabled is not None:
            auto["enabled"] = enabled
        if poll is not None:
            auto["poll_interval_s"] = float(poll) if poll >= 0.5 else None
        if rules is not None:
            auto["rules"] = rules
        auto.setdefault("rules", [])
        sec["auto_switch"] = auto
        err = self.module.save_section(sec)
        if err:
            W.message(self, "保存できませんでした", err, kind="error")

    def _update_auto(self) -> None:
        cfg = self.svc.config if self.svc else None
        if cfg is None:
            return
        if cfg.auto_active:
            self.auto_pill.set_state("ok", f"監視中({cfg.poll_interval_s:g} 秒ごと)")
        elif cfg.auto_enabled and cfg.auto_error:
            self.auto_pill.set_state("warn", "設定待ち")
        else:
            self.auto_pill.set_state("off", "オフ")
        errs = [f"{r.exe or '(空)'}: {r.error}" for r in cfg.rules if r.error]
        if cfg.auto_enabled and cfg.auto_error:
            errs.insert(0, cfg.auto_error)
        self.rules_err.setText("\n".join("・" + e for e in errs))
        self.rules_err.setVisible(bool(errs))

    # ---------------------------------------------------------------- 更新
    def _on_changed(self) -> None:
        svc = self.svc
        if svc is None:
            return
        cfg = svc.config
        cur = svc.current_mode()
        snap = svc.undo_info()
        busy = svc.busy
        # ヒーロー
        if cur is not None:
            self.pill_mode.set_state(mode_accent(cur, cur.index), f"現在: {cur.label}")
        else:
            self.pill_mode.set_state("off", "モード未適用")
        self.pill_undo.set_state("warn" if snap else "off", f"元に戻せる: 「{snap.get('label_to')}」適用前" if snap else "元に戻す記録なし")
        self.pill_busy.setVisible(busy)
        self.btn_undo.setEnabled(snap is not None and not busy)
        self.btn_undo_prev.setEnabled(snap is not None and not busy)
        # 数値
        self.t_mode.set_value(cur.label if cur else "—")
        self.t_last.set_value(_fmt_time(svc.state.data.get("last_switch_at")))
        self.t_count.set_value(f"{len(cfg.valid_modes())} / {len(cfg.modes)}")
        self.t_undo.set_value("可" if snap else "不可")
        # タイル(設定が変わったか、状態が変わったときに作り直す)
        tiles = []
        for m in cfg.modes:
            tile = ModeTile(m, mode_accent(m, m.index), current=(cur is not None and cur.name == m.name), busy=busy)
            tile.preview_clicked.connect(guard(lambda n: self.module.switch(n, dry_run=True, source="gui")))
            tile.switch_clicked.connect(guard(lambda n: self.module.switch(n, source="gui")))
            tiles.append(tile)
        self.grid.set_tiles(tiles)
        self.grid.setVisible(bool(tiles))
        self.empty_modes.setVisible(not tiles)
        # 設定そのものが変わった(外部で保存・確認済みになった)ら、未編集の編集画面を読み直す
        if cfg is not self._cfg_seen:
            self._cfg_seen = cfg
            if not self.editor.dirty:
                self.editor.reload()
            self.seg_preview.set_value(cfg.preview, animate=False)
            self.tg_force.set_checked_silent(cfg.allow_force_kill)
            self.tg_auto.set_checked_silent(cfg.auto_enabled)
            self.sp_poll.blockSignals(True)
            self.sp_poll.setValue(float(cfg.poll_interval_s or 0))
            self.sp_poll.blockSignals(False)
            self._update_auto()
        self._hist_timer.start()

    def _load_history(self) -> None:
        svc = self.svc
        if svc is None:
            return
        rows = svc.oplog.tail(300)
        f = self.hist_filter.value()
        if f == "real":
            rows = [r for r in rows if not r.get("dry_run")]
        elif f == "dry":
            rows = [r for r in rows if r.get("dry_run")]
        rows = rows[:200]
        self.hist.setRowCount(len(rows))
        for i, r in enumerate(rows):
            res = str(r.get("result") or "")
            vals = [
                _fmt_time(str(r.get("ts") or "")),
                SOURCE_LABELS.get(str(r.get("source")), str(r.get("source"))) + ("(dry-run)" if r.get("dry_run") else ""),
                str(r.get("mode") or ""),
                str(r.get("step") or ""),
                TYPE_LABELS.get(str(r.get("type")), str(r.get("type"))),
                str(r.get("target") or ""),
                "●  " + RESULT_LABELS.get(res, res),
                str(r.get("reason") or ""),
            ]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setToolTip(v)
                if c == 6:
                    it.setForeground(QColor(result_color("planned" if res == "planned" else res)))
                if c == 3:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.hist.setItem(i, c, it)
