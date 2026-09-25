# Control Center の LayoutKeep 画面。モード切替・モニタマップ・保存済みレイアウト一覧・計画表・targets 編集・
# タイミング / ID 方式・ホットキー・操作履歴をまとめて表示する。設定の変更は即 ctx.write_settings で保存する。
# 画面にはタイトルを出さない(「今開いているウィンドウから選ぶ」ダイアログ内だけ例外)。スロットはすべて例外を握る。
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from deskkit import catalog
from deskkit.ui import theme as T
from deskkit.ui import widgets as W
from deskkit.ui.theme import G

from . import config
from .dialogs import pick_windows
from .model import ID_SOURCES, Layout, RawMonitor, as_dict, as_list, exe_basename, rect_text
from .monitor_map import MapBox, MapMonitor, MonitorMap
from .monitors import primary_work_origin
from .planner import Plan
from .store import BrokenLayoutError, LayoutSummary
from .windows import pickable

if TYPE_CHECKING:
    from .module import LayoutKeepModule

log = logging.getLogger("deskkit.layoutkeep.page")

ACTION_LABELS = {"save": "保存", "plan": "計画(試運転)", "apply": "適用", "undo": "取り消し", "propose": "提案",
                 "suppressed": "抑止"}
SOURCE_LABELS = {"tray": "トレイ", "hotkey": "ホットキー", "auto": "自動", "event": "イベント", "gui": "画面", "cli": "CLI"}
SKIP_LABELS = {"not_running": "未起動", "ambiguous": "曖昧", "unchanged": "既に同じ位置", "set_failed": "失敗", "gone": "閉じた"}


def _skip_label(k: str) -> str:
    if k.startswith("excluded:"):
        return "除外:" + k.split(":", 1)[1]
    return SKIP_LABELS.get(k, k)


def _table(headers: Sequence[str], stretch: int | None = None, *, height: int = 220, editable: bool = False) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(list(headers))
    t.verticalHeader().setVisible(False)
    t.setAlternatingRowColors(True)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    if not editable:
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    else:
        t.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed
                          | QAbstractItemView.EditTrigger.SelectedClicked)
    t.setMinimumHeight(height)
    t.setWordWrap(False)
    hh = t.horizontalHeader()
    for i in range(len(headers)):
        hh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
    if stretch is not None:
        hh.setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
    t.setShowGrid(False)
    return t


def _item(text: str, *, color: str | None = None, bold: bool = False, editable: bool = False, tip: str | None = None) -> QTableWidgetItem:
    it = QTableWidgetItem(text)
    if not editable:
        it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
    if color:
        it.setForeground(QColor(color))
    if bold:
        f = QFont()
        f.setBold(True)
        it.setFont(f)
    if tip:
        it.setToolTip(tip)
    return it


def _text(t: QTableWidget, r: int, c: int) -> str:
    it = t.item(r, c)
    return it.text().strip() if it is not None else ""


def _fmt_ts(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%m/%d %H:%M:%S")
    except (TypeError, ValueError):
        return ts or "—"


def _w(lay: QHBoxLayout) -> QWidget:
    box = QWidget()
    box.setLayout(lay)
    return box


class LayoutKeepPage(W.ScrollPage):
    def __init__(self, mod: LayoutKeepModule) -> None:
        super().__init__()
        self.mod = mod
        self.ctx = mod.ctx
        info = catalog.info("layoutkeep")
        self.accent = info.accent
        self._shown_sig: str | None = None  # マップに出しているレイアウト(None なら現在の構成)
        self._loading = False
        self._layouts: list[LayoutSummary] = []
        self._entries_layout: Layout | None = None
        self._build_hero(info)
        self._build_disabled_banner()
        self._build_stats()
        self._build_map_card()
        self._build_plan_card()
        self._build_layouts_card()
        self._build_targets_card()
        self._build_timing_card()
        self._build_hotkeys_card()
        self._build_history_card()
        self.finish()
        mod.signals.changed.connect(self._on_changed)
        QTimer.singleShot(0, self._initial)

    # ================================================================ 共通
    def _s(self, fn: Callable[..., Any], label: str) -> Callable[..., Any]:
        """スロット用の例外ラッパー(ctx.safe。Qt から例外を漏らさない)。"""
        wrapped: Callable[..., Any] = self.ctx.safe(fn, f"page:{label}")
        return wrapped

    def _parent(self) -> QWidget:
        return self.window()

    def _flash(self, text: str = "保存しました", kind: str = "ok") -> None:
        self.pill_saved.set_state(kind, text)
        self.pill_saved.show()
        QTimer.singleShot(1800, self._hide_flash)

    def _hide_flash(self) -> None:
        try:
            self.pill_saved.hide()
        except RuntimeError:
            pass

    def _settings_result(self, err: str | None, ok_text: str = "保存しました") -> bool:
        if err:
            W.message(self._parent(), "設定を保存できません", err, kind="error")
            self._flash("保存に失敗", "error")
            return False
        self._flash(ok_text)
        return True

    def _initial(self) -> None:
        try:
            self.refresh(animate=False)
        except Exception:  # noqa: BLE001
            log.exception("page initial refresh")

    def _on_changed(self) -> None:
        try:
            self.refresh()
        except Exception:  # noqa: BLE001 - 画面更新の失敗でモジュールを止めない
            log.exception("page refresh")

    # ================================================================ 見出し
    def _build_hero(self, info: catalog.ModuleInfo) -> None:
        hero = W.Hero(info.title, "モニタ構成ごとにウィンドウ配置を保存し、抜き差しやスリープ復帰で崩れた配置をワンアクションで戻します。",
                      info.glyph, self.accent)
        self.pill_mode = W.StatusPill()
        self.pill_cfg = W.StatusPill()
        self.pill_prop = W.StatusPill("提案中", "accent")
        self.pill_saved = W.StatusPill("保存しました", "ok")
        self.pill_saved.hide()
        for p in (self.pill_mode, self.pill_cfg, self.pill_prop, self.pill_saved):
            hero.add_pill(p)
        self.seg_mode = W.Segmented([("dry_run", "試運転"), ("live", "本番")], self.mod.cfg.mode, self.accent)
        self.seg_mode.changed.connect(self._s(self._on_mode, "mode"))
        self.tg_auto = W.ToggleSwitch(self.mod.cfg.auto_apply, self.accent)
        self.tg_auto.setToolTip("構成が変わって落ち着いたら、提案通知を出さずに自動で戻します(本番のときだけ)")
        self.tg_auto.toggled.connect(self._s(self._on_auto, "auto_apply"))
        hero.add_action(self.seg_mode)
        hero.add_action(_w(W.hbox(None, W.label("自動で戻す", "Dim"), self.tg_auto)))
        self.add(hero)

    def _build_disabled_banner(self) -> None:
        self.banner = W.Card("LayoutKeep は無効です", self.mod.disabled_reason or "", G.WARNING, T.WARN)
        self.banner.add(W.label("DeskKit 本体が Per-Monitor V2 の DPI awareness で動いていないため、座標がずれる恐れがあり、"
                                "配置の保存・復元を行いません。本体の起動方法を確認してください(LayoutKeep は awareness を変更しません)。",
                                "Dim", wrap=True))
        self.banner.setVisible(bool(self.mod.disabled_reason))
        self.add(self.banner)

    def _build_stats(self) -> None:
        self.st_mon = W.StatTile("モニタ", "—", G.MONITOR, self.accent)
        self.st_layouts = W.StatTile("保存済みレイアウト", "—", G.LAYOUT, self.accent)
        self.st_wins = W.StatTile("この構成で保存したウィンドウ", "—", G.APP, self.accent)
        self.st_undo = W.StatTile("元に戻せる配置", "—", G.UNDO, self.accent)
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        for s in (self.st_mon, self.st_layouts, self.st_wins, self.st_undo):
            lay.addWidget(s, 1)
        self.add(row)

    # ================================================================ マップ
    def _build_map_card(self) -> None:
        c = W.Card("モニタマップ", "モニタを縮尺どおりに描き、保存したウィンドウの位置を重ねて表示します。箱にマウスを載せると下の表の行が光ります。",
                   G.MONITOR, self.accent)
        self.lbl_shown = W.StatusPill("現在の構成", "info")
        c.add_header_widget(self.lbl_shown)
        self.btn_back = W.button("現在の構成を表示", "ghost", G.REFRESH, on_click=self._s(self._show_current, "show_current"))
        c.add_header_widget(self.btn_back)
        self.map = MonitorMap(self.accent)
        self.map.setMinimumHeight(300)
        self.map.hovered.connect(self._s(self._on_map_hover, "map_hover"))
        c.add(self.map)
        legend = W.hbox(self._legend(self.accent, "保存した位置"), self._legend(T.TEXT_DIM, "今の位置(計画表示中)", dashed=True),
                        self._legend(T.TEXT_MUTE, "動かさない"), None, spacing=18)
        c.add_layout(legend)
        self.tbl_entries = _table(["#", "exe", "クラス", "表示", "保存した位置", "title_regex(ダブルクリックで編集)", "計画"], 5,
                                  height=180, editable=True)
        self.tbl_entries.setMouseTracking(True)
        self.tbl_entries.cellEntered.connect(self._s(lambda r, _c: self.map.set_highlight(r), "entry_hover"))
        self.tbl_entries.cellChanged.connect(self._s(self._on_entry_changed, "entry_regex"))
        self.tbl_entries.itemSelectionChanged.connect(self._s(self._on_entry_select, "entry_select"))
        self.empty_entries = W.EmptyState(G.LAYOUT, "この構成のレイアウトはまだありません",
                                          "対象のウィンドウ(targets)を並べてから「現在の配置を保存」を押してください。")
        c.add(self.tbl_entries)
        c.add(self.empty_entries)
        self.btn_save = W.button("現在の配置を保存", "primary", G.SAVE, on_click=self._s(self._save, "save"))
        self.btn_plan = W.button("計画を見る(動かさない)", "secondary", G.EYE, on_click=self._s(self._plan, "plan"))
        self.btn_apply = W.button("配置を戻す", "secondary", G.PLAY, on_click=self._s(self._apply, "apply"))
        self.btn_undo = W.button("直前の適用を元に戻す", "ghost", G.UNDO, on_click=self._s(self._undo, "undo"))
        self.btn_name = W.button("この構成に名前を付ける", "ghost", G.EDIT, on_click=self._s(self._name_current, "name"))
        c.add_layout(W.hbox(self.btn_save, self.btn_plan, self.btn_apply, None, self.btn_name, self.btn_undo))
        self.add(c)

    @staticmethod
    def _legend(color: str, text: str, *, dashed: bool = False) -> QWidget:
        sw = QWidget()
        sw.setFixedSize(14, 10)
        style = "dashed" if dashed else "solid"
        sw.setStyleSheet(f"background: {T.alpha(color, 0.0 if dashed else 0.25)}; border: 1px {style} {color}; border-radius: 3px;")
        return _w(W.hbox(sw, W.label(text, "Mute"), spacing=6))

    # ================================================================ 計画
    def _build_plan_card(self) -> None:
        c = W.Card("適用計画", "「計画を見る」「配置を戻す」を押すと、動かす / 動かさないと理由、移動前後の配置をここに出します。",
                   G.LIST, self.accent)
        self.pill_plan = W.StatusPill("計画なし", "off")
        c.add_header_widget(self.pill_plan)
        self.lbl_plan = W.label("", "Dim", wrap=True)
        c.add(self.lbl_plan)
        self.tbl_plan = _table(["判定", "exe", "クラス", "規則", "理由", "今の配置", "戻す配置"], 4, height=200)
        self.tbl_plan.setMouseTracking(True)
        self.tbl_plan.cellEntered.connect(self._s(self._on_plan_hover, "plan_hover"))
        c.add(self.tbl_plan)
        self.plan_card = c
        self.add(c)

    # ================================================================ レイアウト一覧
    def _build_layouts_card(self) -> None:
        c = W.Card("保存済みレイアウト", "構成(モニタの組み合わせ)ごとに1つ。選ぶとマップに表示します。適用できるのは現在の構成と一致するものだけです。",
                   G.LAYOUT, self.accent)
        self.tbl_layouts = _table(["名前", "シグネチャ", "モニタ", "ウィンドウ", "保存日時", "状態"], 0, height=130)
        self.tbl_layouts.itemSelectionChanged.connect(self._s(self._on_layout_select, "layout_select"))
        c.add(self.tbl_layouts)
        self.btn_l_plan = W.button("計画(試運転)", "secondary", G.EYE, on_click=self._s(self._plan, "l_plan"))
        self.btn_l_apply = W.button("今すぐ適用", "secondary", G.PLAY, on_click=self._s(self._apply, "l_apply"))
        self.btn_l_rename = W.button("名前を変更", "ghost", G.EDIT, on_click=self._s(self._rename_selected, "rename"))
        self.btn_l_delete = W.button("削除", "danger", G.DELETE, on_click=self._s(self._delete_selected, "delete"))
        self.lbl_l_hint = W.label("", "Mute", wrap=True)
        c.add_layout(W.hbox(self.btn_l_plan, self.btn_l_apply, None, self.btn_l_rename, self.btn_l_delete))
        c.add(self.lbl_l_hint)
        self.add(c)

    # ================================================================ targets
    def _build_targets_card(self) -> None:
        c = W.Card("対象ウィンドウ(targets)", "保存・復元するのはここに書いたウィンドウだけです(許可リスト)。exe はファイル名かフルパス、"
                   "クラスと title_regex は空なら条件なし。", G.APP, self.accent)
        self.tbl_targets = _table(["exe", "クラス(空=すべて)", "title_regex(空=すべて)"], 2, height=170, editable=True)
        self.tbl_targets.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tbl_targets.cellChanged.connect(self._s(self._on_target_changed, "target_changed"))
        c.add(self.tbl_targets)
        self.lbl_targets = W.label("", "Mute", wrap=True)
        c.add(self.lbl_targets)
        pick = W.button("今開いているウィンドウから選ぶ", "primary", G.SEARCH, on_click=self._s(self._pick_targets, "pick"))
        add = W.button("行を追加", "secondary", G.ADD, on_click=self._s(self._add_target_row, "add_target"))
        rm = W.button("選択を削除", "ghost", G.DELETE, on_click=self._s(self._remove_targets, "rm_target"))
        c.add_layout(W.hbox(pick, add, None, rm))
        c.add(W.divider())
        c.add(W.label("除外するクラス(targets に当たっても保存・適用しない)", "H3"))
        self.ed_exclude = W.StringListEditor(sorted(self.mod.cfg.exclude_classes),
                                             "クラス名(例: #32770)", height=96)
        self.ed_exclude.changed.connect(self._s(self._on_exclude_changed, "exclude"))
        c.add(self.ed_exclude)
        self.add(c)

    # ================================================================ タイミング・ID
    def _build_timing_card(self) -> None:
        c = W.Card("検知と構成シグネチャ", "モニタの抜き差し・解像度変更・スリープ復帰を受けてから提案するまでの待ち方と、モニタを見分ける ID の取り方。",
                   G.CLOCK, self.accent)
        cfg = self.mod.cfg
        self.sp_debounce = self._spin(100, 120_000, 100, " ms", cfg.debounce_ms)
        self.sp_settle = self._spin(1, 20, 1, " 回", cfg.settle_checks)
        self.sp_wait = self._spin(0, 600, 1, " 秒", cfg.max_wait_s)
        c.add(W.SettingRow("デバウンス", "最後のイベントからこの時間だけ待ってから構成を調べます。", self.sp_debounce, G.CLOCK))
        c.add(W.SettingRow("落ち着いたとみなす回数", "構成シグネチャがこの回数続けて同じなら、提案(または自動適用)します。", self.sp_settle,
                           G.CHECK))
        c.add(W.SettingRow("layout.apply の最大待ち時間", "ModeShift などが wait_s を指定したときの上限。起動していないウィンドウを"
                           "この間だけ探し直します。", self.sp_wait, G.LIGHTNING))
        self._timing_timer = QTimer(self)
        self._timing_timer.setSingleShot(True)
        self._timing_timer.setInterval(700)
        self._timing_timer.timeout.connect(self._s(self._save_timing, "timing"))
        for sp in (self.sp_debounce, self.sp_settle, self.sp_wait):
            sp.valueChanged.connect(self._s(lambda _v: self._timing_timer.start(), "timing_changed"))
        c.add(W.divider())
        self.seg_id = W.Segmented([(k, config.ID_SOURCE_LABELS[k]) for k in ID_SOURCES], cfg.id_source, self.accent)
        self.seg_id.changed.connect(self._s(self._on_id_source, "id_source"))
        c.add(W.SettingRow("モニタ ID の取り方", "構成シグネチャに使うモニタの ID。変えると既存のレイアウトは別の構成扱いになります。", None,
                           G.MONITOR))
        c.add(self.seg_id)
        self.lbl_id_help = W.label("", "Dim", wrap=True)
        c.add(self.lbl_id_help)
        self.tbl_ids = _table(["#", "デバイス", "主", "ID(選択中の方式)"], 3, height=90)
        c.add(self.tbl_ids)
        self.add(c)

    @staticmethod
    def _spin(lo: int, hi: int, step: int, suffix: str, value: int) -> QSpinBox:
        sp = QSpinBox()
        sp.setRange(lo, hi)
        sp.setSingleStep(step)
        sp.setSuffix(suffix)
        sp.setValue(value)
        sp.setMinimumWidth(130)
        sp.setAlignment(Qt.AlignmentFlag.AlignRight)
        return sp

    # ================================================================ ホットキー
    def _build_hotkeys_card(self) -> None:
        c = W.Card("ホットキー", "既定は未割り当て。クリックしてキーを押すと登録します(Backspace で解除)。変更するとモジュールを再起動します。",
                   G.KEYBOARD, self.accent)
        self.hk: dict[str, W.HotkeyEdit] = {}
        for name, title, desc in (("save", "現在の配置を保存", "layoutkeep.save"), ("apply", "配置を戻す", "layoutkeep.apply")):
            ed = W.HotkeyEdit(self.mod.cfg.hotkeys.get(name, ""))
            ed.changed.connect(self._s(lambda text, n=name: self._on_hotkey(n, text), f"hotkey_{name}"))
            status = self.mod.hotkey_ok.get(name)
            row = W.SettingRow(title, desc + ("  —  登録できませんでした(他のアプリと競合)" if status is False else ""), ed, G.KEYBOARD)
            c.add(row)
            self.hk[name] = ed
        self.add(c)

    # ================================================================ 履歴
    def _build_history_card(self) -> None:
        c = W.Card("操作履歴", "oplog.jsonl の新しい順。ウィンドウのタイトルは記録していません。", G.LOG, self.accent)
        c.add_header_widget(W.icon_button(G.REFRESH, "再読み込み", self._s(self._fill_history, "history")))
        self.tbl_hist = _table(["時刻", "操作", "起点", "構成", "動かした", "動かさなかった内訳", "エラー"], 5, height=220)
        c.add(self.tbl_hist)
        self.add(c)

    # ================================================================ 更新
    def refresh(self, animate: bool = True) -> None:
        mod = self.mod
        cfg = mod.cfg
        disabled = bool(mod.disabled_reason)
        self.banner.setVisible(disabled)
        for b in (self.btn_save, self.btn_plan, self.btn_apply, self.btn_undo, self.btn_name, self.btn_l_plan, self.btn_l_apply):
            b.setEnabled(not disabled)
        self.seg_mode.set_value(cfg.mode, animate=animate)
        self.tg_auto.set_checked_silent(cfg.auto_apply)
        self.pill_mode.set_state("warn" if not cfg.live else "ok", "試運転" if not cfg.live else "本番")
        sr = mod.last_sig if mod.last_sig is not None else (None if disabled else mod.current_signature())
        cur = sr.signature if sr else None
        if cur:
            self.pill_cfg.set_state("info", f"{mod.display_name(cur)}  ·  {cur}")
        else:
            self.pill_cfg.set_state("error", "構成シグネチャを計算できません" if sr else "無効")
            if sr and sr.reason:
                self.pill_cfg.setToolTip(sr.reason)
        self.pill_prop.setVisible(bool(mod.proposal_sig and mod.proposal_sig == cur))
        # 統計
        self._layouts = mod.store.list_layouts()
        self.st_mon.set_value(str(len(sr.monitors)) if sr else "—")
        self.st_layouts.set_value(str(len(self._layouts)))
        cur_sum = next((s for s in self._layouts if s.signature == cur), None)
        self.st_wins.set_value(f"{cur_sum.window_count} 枚" if cur_sum else "未保存")
        undo = mod.store.read_undo()
        self.st_undo.set_value(f"{len(undo.get('windows', []))} 件" if undo else "なし")
        self.btn_undo.setEnabled(not disabled and undo is not None)
        self.btn_apply.setText("配置を戻す(試運転)" if not cfg.live else "配置を戻す")
        self.btn_l_apply.setText("今すぐ適用(試運転)" if not cfg.live else "今すぐ適用")
        # 表示中のレイアウト
        if self._shown_sig is not None and self._shown_sig not in {s.signature for s in self._layouts}:
            self._shown_sig = None
        self._fill_layouts(cur)
        self._fill_entries_and_map(cur, animate)
        self._fill_plan(cur)
        self._fill_targets()
        self._fill_ids(sr.monitors if sr else [])
        self._fill_history()

    def _shown(self, cur: str | None) -> str | None:
        return self._shown_sig or cur

    def _fill_layouts(self, cur: str | None) -> None:
        t = self.tbl_layouts
        self._loading = True
        try:
            keep = self._shown(cur)
            t.setRowCount(0)
            sel_row = -1
            for s in sorted(self._layouts, key=lambda x: (x.signature != cur, x.saved_at), reverse=False):
                r = t.rowCount()
                t.insertRow(r)
                is_cur = s.signature == cur
                name = self.mod.display_name(s.signature)
                first = _item(("●  " if is_cur else "") + name, color=self.accent if is_cur else None, bold=is_cur)
                first.setData(Qt.ItemDataRole.UserRole, s.signature)
                t.setItem(r, 0, first)
                t.setItem(r, 1, _item(s.signature, color=T.TEXT_DIM))
                t.setItem(r, 2, _item("—" if s.broken else f"{s.monitor_count} 台"))
                t.setItem(r, 3, _item("—" if s.broken else f"{s.window_count} 枚"))
                t.setItem(r, 4, _item(_fmt_ts(s.saved_at) if s.saved_at else "—"))
                state = "壊れています" if s.broken else ("現在の構成" if is_cur else "別の構成")
                t.setItem(r, 5, _item(state, color=T.DANGER if s.broken else (T.SUCCESS if is_cur else T.TEXT_MUTE)))
                if s.signature == keep:
                    sel_row = r
            if sel_row >= 0:
                t.selectRow(sel_row)
        finally:
            self._loading = False
        self._update_layout_buttons(cur)

    def _selected_layout(self) -> str | None:
        r = self.tbl_layouts.currentRow()
        it = self.tbl_layouts.item(r, 0) if r >= 0 else None
        if it is None:
            return None
        v = it.data(Qt.ItemDataRole.UserRole)
        return str(v) if v else None

    def _update_layout_buttons(self, cur: str | None) -> None:
        sel = self._selected_layout()
        is_cur = sel is not None and sel == cur
        disabled = bool(self.mod.disabled_reason)
        self.btn_l_plan.setEnabled(is_cur and not disabled)
        self.btn_l_apply.setEnabled(is_cur and not disabled)
        self.btn_l_rename.setEnabled(sel is not None)
        self.btn_l_delete.setEnabled(sel is not None)
        if sel is None:
            self.lbl_l_hint.setText("保存するとここに並びます。" if not self._layouts else "")
        elif not is_cur:
            self.lbl_l_hint.setText("このレイアウトは今のモニタ構成と一致しないため適用できません(近い構成への部分適用はしません)。")
        else:
            self.lbl_l_hint.setText("")

    def _load_shown(self, sig: str | None) -> tuple[Layout | None, str | None]:
        if not sig:
            return None, None
        try:
            return self.mod.store.load_layout(sig), None
        except BrokenLayoutError as e:
            return None, str(e)

    def _fill_entries_and_map(self, cur: str | None, animate: bool) -> None:
        shown = self._shown(cur)
        lay, err = self._load_shown(shown)
        self._entries_layout = lay
        is_cur = shown is not None and shown == cur
        self.lbl_shown.set_state("info" if is_cur else "warn",
                                 "現在の構成" if is_cur else f"表示中: {self.mod.display_name(shown)}")
        self.btn_back.setVisible(not is_cur and cur is not None)
        plan = self.mod.last_plan if (self.mod.last_plan and self.mod.last_plan.signature == shown) else None
        plan_by = {i.index: i for i in plan.items} if plan else {}
        # モニタ
        mons: list[MapMonitor] = []
        if is_cur and self.mod.last_sig is not None:
            src = self.mod.cfg.id_source
            ordered = sorted(self.mod.last_sig.monitors, key=lambda m: (m.rect[0], m.rect[1]))
            for n, m in enumerate(ordered, 1):
                mons.append(MapMonitor(m.ids.get(src) or m.device, m.rect, m.work, m.primary, m.dpi, n))
            origin = primary_work_origin(self.mod.last_sig.monitors)
        elif lay is not None:
            ordered_d = sorted(lay.monitors, key=lambda d: (int(d.get("x", 0)), int(d.get("y", 0))))
            for n, d in enumerate(ordered_d, 1):
                x, y, w, h = (int(d.get(k, 0)) for k in ("x", "y", "w", "h"))
                mons.append(MapMonitor(str(d.get("id") or n), (x, y, x + w, y + h), None, bool(d.get("primary")),
                                       int(d["dpi"]) if isinstance(d.get("dpi"), int) else None, n))
            origin = (0, 0)
        else:
            origin = (0, 0)
        boxes: list[MapBox] = []
        t = self.tbl_entries
        self._loading = True
        try:
            t.setRowCount(0)
            if lay is not None:
                for i, e in enumerate(lay.windows):
                    r = t.rowCount()
                    t.insertRow(r)
                    t.setItem(r, 0, _item(str(i + 1), color=T.TEXT_MUTE))
                    t.setItem(r, 1, _item(e.exe_name, bold=True, tip=e.exe))
                    t.setItem(r, 2, _item(e.cls, color=T.TEXT_DIM))
                    t.setItem(r, 3, _item("最大化" if e.show == "maximized" else "通常"))
                    t.setItem(r, 4, _item(rect_text(e.normal_rect)))
                    t.setItem(r, 5, _item(e.title_regex or "", editable=True))
                    it = plan_by.get(i)
                    if it is None:
                        t.setItem(r, 6, _item("—", color=T.TEXT_MUTE))
                    else:
                        color = T.SUCCESS if it.action == "move" else (T.WARN if it.key == "ambiguous" else T.TEXT_MUTE)
                        t.setItem(r, 6, _item("動かす" if it.action == "move" else "動かさない", color=color, tip=it.reason))
                    rect = e.screen_rect or (e.normal_rect[0] + origin[0], e.normal_rect[1] + origin[1],
                                             e.normal_rect[2] + origin[0], e.normal_rect[3] + origin[1])
                    boxes.append(MapBox(key=f"{shown}:{i}", index=i, rect=rect, label=e.exe_name.rsplit(".", 1)[0] or e.exe_name,
                                        maximized=e.show == "maximized",
                                        current=it.before_screen if it else None, moving=bool(it and it.action == "move"),
                                        muted=bool(it and it.action == "skip" and it.key != "unchanged")))
        finally:
            self._loading = False
        has = lay is not None and bool(lay.windows)
        t.setVisible(has)
        self.empty_entries.setVisible(not has)
        if err:
            self.map.set_empty_text(f"レイアウトを読めません: {err}")
        elif not mons:
            self.map.set_empty_text("構成を取得できません" if self.mod.disabled_reason is None else "LayoutKeep は無効です")
        self.map.set_data(mons, boxes, animate=animate)

    def _fill_plan(self, cur: str | None) -> None:
        plan: Plan | None = self.mod.last_plan if (self.mod.last_plan and self.mod.last_plan.signature == cur) else None
        t = self.tbl_plan
        t.setRowCount(0)
        if plan is None:
            self.pill_plan.set_state("off", "計画なし")
            self.lbl_plan.setText("まだ計画を作っていません。")
            return
        moves = len(plan.moves)
        self.pill_plan.set_state("ok" if moves else "off", f"動かす {moves} 件")
        sk = plan.skipped()
        detail = "、".join(f"{_skip_label(k)} {v}" for k, v in sk.items())
        text = f"動かす {moves} 件 / 動かさない {sum(sk.values())} 件" + (f"({detail})" if detail else "")
        for exe, _cls, n in plan.ambiguous_groups():
            text += f"\n{exe.rsplit('.', 1)[0]} のウィンドウ {n} 枚は区別できないため動かしません。title_regex を書くと区別できます。"
        self.lbl_plan.setText(text)
        for it in plan.items:
            r = t.rowCount()
            t.insertRow(r)
            if it.action == "move":
                head = _item("動かす", color=T.SUCCESS, bold=True)
            else:
                head = _item("動かさない", color=T.WARN if it.key == "ambiguous" else T.TEXT_MUTE, bold=True)
            head.setData(Qt.ItemDataRole.UserRole, it.index)
            t.setItem(r, 0, head)
            t.setItem(r, 1, _item(it.exe_name, tip=it.exe))
            t.setItem(r, 2, _item(it.cls, color=T.TEXT_DIM))
            t.setItem(r, 3, _item(it.rule, color=T.TEXT_DIM))
            t.setItem(r, 4, _item(it.reason))
            before = "—" if it.before_rect is None else f"{'最大化 ' if it.before_show == 'maximized' else ''}{rect_text(it.before_rect)}"
            after = f"{'最大化 ' if it.after_show == 'maximized' else ''}{rect_text(it.after_rect)}"
            t.setItem(r, 5, _item(before, color=T.TEXT_DIM))
            t.setItem(r, 6, _item(after, color=self.accent if it.action == "move" else T.TEXT_DIM))

    def _fill_targets(self) -> None:
        sec, _ = config.fill_defaults(self.ctx.settings_dict())
        rows = as_list(sec.get("targets"))
        t = self.tbl_targets
        self._loading = True
        try:
            t.setRowCount(0)
            for d in rows:
                if not isinstance(d, dict):
                    continue
                r = t.rowCount()
                t.insertRow(r)
                t.setItem(r, 0, _item(str(d.get("exe") or ""), editable=True))
                t.setItem(r, 1, _item(str(d.get("class") or ""), editable=True))
                t.setItem(r, 2, _item(str(d.get("title_regex") or ""), editable=True))
        finally:
            self._loading = False
        inv = self.mod.cfg.invalid_targets
        n = len(self.mod.cfg.targets)
        if inv:
            self.lbl_targets.setText(f"有効 {n} 件 / 無効 {len(inv)} 件: " + " / ".join(inv))
            self.lbl_targets.setStyleSheet(f"color: {T.WARN};")
        else:
            self.lbl_targets.setText(f"{n} 件の対象。" if n else "まだ対象がありません。「今開いているウィンドウから選ぶ」で追加してください。")
            self.lbl_targets.setStyleSheet("")

    def _fill_ids(self, mons: Sequence[RawMonitor]) -> None:
        src = self.mod.cfg.id_source
        self.lbl_id_help.setText(config.ID_SOURCE_HELP.get(src, ""))
        t = self.tbl_ids
        t.setRowCount(0)
        for n, m in enumerate(sorted(mons, key=lambda x: (x.rect[0], x.rect[1])), 1):
            r = t.rowCount()
            t.insertRow(r)
            t.setItem(r, 0, _item(str(n), color=T.TEXT_MUTE))
            t.setItem(r, 1, _item(m.device))
            t.setItem(r, 2, _item("主" if m.primary else "", color=self.accent))
            v = m.ids.get(src)
            t.setItem(r, 3, _item(v or "取得できません(シグネチャは null になります)", color=None if v else T.DANGER))

    def _fill_history(self) -> None:
        t = self.tbl_hist
        t.setRowCount(0)
        for rec in self.mod.store.read_oplog(200):
            r = t.rowCount()
            t.insertRow(r)
            action = str(rec.get("action") or "")
            res = rec.get("result")
            label = ACTION_LABELS.get(action, action)
            if res and res not in ("applied",):
                label += f"({res})"
            color = {"apply": T.SUCCESS, "undo": T.INFO, "suppressed": T.WARN, "propose": self.accent}.get(action)
            if res in ("undo_failed", "sig_mismatch"):
                color = T.DANGER
            t.setItem(r, 0, _item(_fmt_ts(str(rec.get("ts") or "")), color=T.TEXT_DIM))
            t.setItem(r, 1, _item(label, color=color, bold=True))
            t.setItem(r, 2, _item(SOURCE_LABELS.get(str(rec.get("source")), str(rec.get("source") or "—"))))
            sig = rec.get("sig")
            t.setItem(r, 3, _item(self.mod.display_name(str(sig)) if sig else "—", tip=str(sig or "")))
            moved = rec.get("moved", rec.get("planned", rec.get("count")))
            t.setItem(r, 4, _item("—" if moved is None else str(moved)))
            sk = as_dict(rec.get("skipped"))
            reason = rec.get("reason")
            text = "、".join(f"{_skip_label(str(k))} {v}" for k, v in sk.items()) or (f"理由: {reason}" if reason else "—")
            t.setItem(r, 5, _item(text, color=T.TEXT_DIM))
            errs = as_list(rec.get("errors"))
            etext = ", ".join(f"{e.get('exe')}:{e.get('win32_error')}" for e in errs if isinstance(e, dict))
            t.setItem(r, 6, _item(etext or "—", color=T.DANGER if etext else T.TEXT_MUTE))

    # ================================================================ 操作
    def _on_mode(self, mode: str) -> None:
        if mode == "live":
            ok, _ = W.confirm(self._parent(), "本番に切り替えますか?",
                              "本番では「配置を戻す」で実際にウィンドウを動かします(SetWindowPlacement のみ。アプリの起動・終了はしません)。"
                              "動かす前の配置は undo.json に残り、「直前の適用を元に戻す」で戻せます。",
                              ok_text="本番にする", glyph=G.WARNING)
            if not ok:
                self.seg_mode.set_value("dry_run")
                return
        self._settings_result(self.mod.set_mode(mode), "本番にしました" if mode == "live" else "試運転にしました")

    def _on_auto(self, on: bool) -> None:
        err = self.mod.update_settings(lambda s: s.__setitem__("auto_apply", bool(on)))
        if self._settings_result(err) and on and not self.mod.cfg.live:
            self._flash("試運転中は自動適用せず、提案通知だけ出します", "warn")

    def _save(self) -> None:
        self.mod.save("gui")
        self._shown_sig = None

    def _plan(self) -> None:
        plan, text = self.mod.preview_plan()
        if plan is None:
            W.message(self._parent(), "計画を作れません", text, kind="warn")
            return
        self._shown_sig = None
        self.refresh()
        self._flash("計画を作りました", "info")

    def _apply(self) -> None:
        self._shown_sig = None
        self.mod.apply_current("gui")

    def _undo(self) -> None:
        ok, _ = W.confirm(self._parent(), "直前の適用を元に戻しますか?",
                          "undo.json に残っている配置へ戻します。取り消し自体は取り消せません。", ok_text="元に戻す", glyph=G.UNDO)
        if ok:
            self.mod.undo("gui")

    def _name_current(self) -> None:
        cur = self.mod.last_sig.signature if self.mod.last_sig else None
        if not cur:
            W.message(self._parent(), "名前を付けられません", "構成シグネチャを計算できません。", kind="warn")
            return
        self._rename(cur)

    def _rename(self, sig: str) -> None:
        v = W.text_input(self._parent(), "構成に名前を付ける", f"構成 {sig} の名前。layout.apply でもこの名前で指定できます。",
                         self.mod.display_name(sig))
        if v is None:
            return
        self._settings_result(self.mod.rename(sig, v), "名前を変更しました")

    def _rename_selected(self) -> None:
        sig = self._selected_layout()
        if sig:
            self._rename(sig)

    def _delete_selected(self) -> None:
        sig = self._selected_layout()
        if not sig:
            return
        ok, _ = W.confirm(self._parent(), "レイアウトを削除しますか?",
                          f"「{self.mod.display_name(sig)}」({sig})のレイアウトを一覧から外します。ファイルは消さずに "
                          f"{sig}.json.deleted-<日時> へ改名して残します。", ok_text="削除", danger=True)
        if not ok:
            return
        err = self.mod.delete_layout(sig)
        if err:
            W.message(self._parent(), "削除できません", err, kind="error")
            return
        if self._shown_sig == sig:
            self._shown_sig = None
        self._flash("削除しました")
        self.refresh()

    def _on_layout_select(self) -> None:
        if self._loading:
            return
        sig = self._selected_layout()
        cur = self.mod.last_sig.signature if self.mod.last_sig else None
        self._update_layout_buttons(cur)
        if sig is None:
            return
        new_shown = None if sig == cur else sig
        if new_shown != self._shown_sig:
            self._shown_sig = new_shown
            self._fill_entries_and_map(cur, True)

    def _show_current(self) -> None:
        self._shown_sig = None
        self.refresh()

    def _on_map_hover(self, idx: int) -> None:
        self.map.set_highlight(idx)
        if 0 <= idx < self.tbl_entries.rowCount():
            self._loading = True
            try:
                self.tbl_entries.selectRow(idx)
                first = self.tbl_entries.item(idx, 0)
                if first is not None:
                    self.tbl_entries.scrollToItem(first)
            finally:
                self._loading = False
        elif idx < 0:
            self._loading = True
            try:
                self.tbl_entries.clearSelection()
            finally:
                self._loading = False

    def _on_entry_select(self) -> None:
        if self._loading:
            return
        self.map.set_highlight(self.tbl_entries.currentRow())

    def _on_plan_hover(self, row: int, _col: int) -> None:
        it = self.tbl_plan.item(row, 0)
        if it is not None:
            v = it.data(Qt.ItemDataRole.UserRole)
            self.map.set_highlight(int(v) if isinstance(v, int) else -1)

    def _on_entry_changed(self, row: int, col: int) -> None:
        if self._loading or col != 5 or self._entries_layout is None:
            return
        it = self.tbl_entries.item(row, col)
        text = it.text().strip() if it else ""
        sig = self._entries_layout.signature
        # 編集確定のシグナル中に表を作り直さないよう、保存は次のイベントループで行う
        QTimer.singleShot(0, self._s(lambda: self._save_entry_regex(sig, row, text), "entry_regex_save"))

    def _save_entry_regex(self, sig: str, row: int, text: str) -> None:
        err = self.mod.set_entry_regex(sig, row, text or None)
        if err:
            W.message(self._parent(), "title_regex を保存できません", err, kind="error")
            self.refresh(animate=False)
            return
        self._flash("title_regex を保存しました")

    # ---- targets
    def _collect_targets(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        t = self.tbl_targets
        for r in range(t.rowCount()):
            vals = [_text(t, r, c) for c in range(3)]
            if not vals[0] and not vals[1] and not vals[2]:
                continue
            out.append({"exe": vals[0], "class": vals[1] or None, "title_regex": vals[2] or None})
        return out

    def _save_targets(self, rows: list[dict[str, Any]]) -> None:
        err = self.mod.update_settings(lambda s: s.__setitem__("targets", rows))
        self._settings_result(err)
        self._fill_targets()

    def _on_target_changed(self, _row: int, _col: int) -> None:
        if self._loading:
            return
        rows = self._collect_targets()
        if any(not r["exe"] for r in rows):
            self.lbl_targets.setText("exe が空の行があります(入力すると保存します)")
            self.lbl_targets.setStyleSheet(f"color: {T.WARN};")
            return
        # 編集確定のシグナル中に表を作り直さないよう、保存は次のイベントループで行う
        QTimer.singleShot(0, self._s(lambda: self._save_targets(rows), "targets_save"))

    def _add_target_row(self) -> None:
        t = self.tbl_targets
        self._loading = True
        try:
            r = t.rowCount()
            t.insertRow(r)
            for c in range(3):
                t.setItem(r, c, _item("", editable=True))
        finally:
            self._loading = False
        t.setCurrentCell(r, 0)
        first = t.item(r, 0)
        if first is not None:
            t.editItem(first)

    def _remove_targets(self) -> None:
        rows = sorted({i.row() for i in self.tbl_targets.selectedIndexes()}, reverse=True)
        if not rows:
            return
        self._loading = True
        try:
            for r in rows:
                self.tbl_targets.removeRow(r)
        finally:
            self._loading = False
        self._save_targets(self._collect_targets())

    def _pick_targets(self) -> None:
        wins = pickable(self.mod.snapshot_windows())
        existing = {(exe_basename(t.exe).lower(), t.cls or "") for t in self.mod.cfg.targets}
        chosen = pick_windows(self._parent(), wins, existing, self.accent)
        if not chosen:
            return
        rows = self._collect_targets()
        for exe, cls in chosen:
            if not any(r["exe"].lower() == exe.lower() and (r["class"] or "") == cls for r in rows):
                rows.append({"exe": exe, "class": cls, "title_regex": None})
        self._save_targets(rows)

    def _on_exclude_changed(self, items: list[str]) -> None:
        self._settings_result(self.mod.update_settings(lambda s: s.__setitem__("exclude_classes", list(items))))

    # ---- タイミング・ID・ホットキー
    def _save_timing(self) -> None:
        d, s_, w_ = self.sp_debounce.value(), self.sp_settle.value(), self.sp_wait.value()

        def mut(s: dict[str, Any]) -> None:
            s["debounce_ms"], s["settle_checks"], s["max_wait_s"] = d, s_, w_

        self._settings_result(self.mod.update_settings(mut))

    def _on_id_source(self, src: str) -> None:
        ok, _ = W.confirm(self._parent(), "モニタ ID の取り方を変えますか?",
                          f"「{config.ID_SOURCE_LABELS.get(src, src)}」に変えると構成シグネチャが変わり、今までのレイアウトは"
                          "別の構成として扱われます(ファイルは残ります。戻せば再び使えます)。", ok_text="変える")
        if not ok:
            self.seg_id.set_value(self.mod.cfg.id_source)
            return

        def mut(s: dict[str, Any]) -> None:
            s.setdefault("signature", {})["id_source"] = src

        self.mod.last_plan = None
        self._settings_result(self.mod.update_settings(mut))

    def _on_hotkey(self, name: str, text: str) -> None:
        def mut(s: dict[str, Any]) -> None:
            s.setdefault("hotkeys", {})[name] = text

        self._settings_result(self.mod.update_settings(mut, restart=True), "ホットキーを保存しました(再起動します)")
