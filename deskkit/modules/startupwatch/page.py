# Control Center の StartupWatch 画面: Hero(状態・読み直す)→ 覚えた/記録の知らせ → 新しく増えた物 → 今ある物の表 →
# 止めたいとき → ショートカットキーが取られているとき → 見ている場所 → 見ていない場所 → 設定。
# 名前・コマンドは画面にだけ出す(V-8)。色は theme(T.*)と catalog のアクセント色を実行時に読む。
from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypeVar

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from deskkit import catalog
from deskkit.modules.startupwatch import _win32
from deskkit.modules.startupwatch.sources import LOCATIONS, StartupItem
from deskkit.modules.startupwatch.watcher import MODE_NOTIFY, MODE_UNREADABLE
from deskkit.ui import theme as T
from deskkit.ui import widgets as W
from deskkit.ui.theme import G

if TYPE_CHECKING:
    from deskkit.modules.startupwatch.module import StartupWatchModule, ViewItem

_log = logging.getLogger("deskkit.startupwatch")
F = TypeVar("F", bound=Callable[..., Any])
LOC_BY_KEY = {loc.key: loc for loc in LOCATIONS}
KEY_HELP = ("どのアプリが使っているかは、Windows が教えてくれません。次の順で見当をつけられます。\n"
            "1. 下の表の『動いています』の物から、心当たりの物を選ぶ。\n"
            "2. そのアプリを、画面の右下の通知領域から終了する。\n"
            "3. KeyFree(空いているショートカット探し)で、そのキーを『調べ直す』。\n"
            "4. 空いたら、そのアプリが使っていました。空かなければ、アプリを起動し直して次の物へ。")
DETAIL_HINT = "行を選ぶと、何を起動するかを全部出します。"
NOT_WATCHED = ("タスク スケジューラ・Microsoft Store のアプリ・サービスから自動で起動する物は、この一覧に出ません。"
               "設定の『スタートアップ アプリ』には出ることがあります。")


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
    return catalog.info("startupwatch").accent


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


def _when(iso: str) -> str:
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    return d.strftime("%Y/%m/%d %H:%M")


def _mark_chip(text: str) -> QLabel:
    color = {"新しい": T.WARN, "中身が変わりました": T.INFO, "DeskKit": _acc()}.get(text, T.TEXT_DIM)
    lb = QLabel(text)
    lb.setStyleSheet(f"color: {color}; background: {T.alpha(color, 0.13)}; border: 1px solid {T.alpha(color, 0.35)};"
                     f" border-radius: 9px; padding: 1px 8px; font-size: 11px; font-weight: 700;")
    return lb


class StartupWatchPage(W.ScrollPage):
    def __init__(self, module: StartupWatchModule) -> None:
        super().__init__()
        self.m = module
        self._opened = False
        acc = _acc()
        info = catalog.info("startupwatch")
        # ---- Hero
        self.hero = W.Hero("StartupWatch", info.tagline, info.glyph, acc)
        self.pill = W.StatusPill("", "ok")
        self.hero.add_pill(self.pill)
        self.hero.add_action(W.button("読み直す", "primary", G.REFRESH, self._rescan))
        self.add(self.hero)
        # ---- 知らせ(覚えた・記録を覚え直した・見張りを続けられない)
        self.notice = W.Card(None, padding=14)
        self.notice_label = W.label("", "Dim", wrap=True)
        self.notice.add(self.notice_label)
        self.add(self.notice)
        # ---- 新しく増えた物
        self.new_card = W.Card("新しく増えた物", "押すまで「新しい」のままです。DeskKit を起動し直しても消えません。", G.SPARKLE, acc)
        self.ack_all_btn = W.button("すべて確かめた", "secondary", G.CHECK, self._ack_all)
        self.new_card.add_header_widget(self.ack_all_btn)
        self.new_box = QVBoxLayout()
        self.new_box.setSpacing(10)
        self.new_card.add_layout(self.new_box)
        self.add(self.new_card)
        # ---- 今ある物
        self.list_card = W.Card("今ある物", "設定やタスク マネージャーでオフにした物も、ここには出ます。", G.LIST, acc)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["印", "名前", "場所", "何を起動するか", "動いているか", ""])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setMinimumHeight(260)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        hh = self.table.horizontalHeader()
        for col, mode in ((0, QHeaderView.ResizeMode.ResizeToContents), (1, QHeaderView.ResizeMode.Interactive),
                          (2, QHeaderView.ResizeMode.Interactive), (3, QHeaderView.ResizeMode.Stretch),
                          (4, QHeaderView.ResizeMode.ResizeToContents), (5, QHeaderView.ResizeMode.ResizeToContents)):
            hh.setSectionResizeMode(col, mode)
        self.table.setColumnWidth(1, 200)
        self.table.setColumnWidth(2, 190)
        self.table.itemSelectionChanged.connect(self._on_select)
        self.list_card.add(self.table)
        self.detail = W.label(DETAIL_HINT, "Mute", wrap=True)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.list_card.add(self.detail)
        self._rows: list[ViewItem] = []
        self.add(self.list_card)
        # ---- 止めたいとき(FR-10)
        stop = W.Card("止めたいとき", "DeskKit は自動起動を書き換えません。Windows の画面で止めてください。", G.POWER, acc)
        stop.add_layout(W.hbox(W.button("スタートアップの設定を開く", "secondary", G.SETTINGS, self._open_settings),
                               W.button("タスク マネージャーを開く", "secondary", G.APP, self._open_taskmgr), None))
        stop.add(W.label("タスク マネージャーでは『スタートアップ アプリ』のタブを選んでください。"
                         "スタートアップ フォルダの物は、表の『フォルダを開く』でそのフォルダを開けます。", "Mute", wrap=True))
        self.add(stop)
        # ---- ショートカットキーが取られているとき(FR-11)
        keys = W.Card("ショートカットキーが取られているとき", None, G.KEYBOARD, acc)
        keys.add(W.label(KEY_HELP, "Dim", wrap=True))
        self.add(keys)
        # ---- 見ている場所(FR-4)
        self.loc_card = W.Card("見ている場所", "Windows が変更を知らせてくれる場所は、増えるとすぐに気づけます。", G.EYE, acc)
        self.loc_box = QVBoxLayout()
        self.loc_box.setSpacing(4)
        self.loc_card.add_layout(self.loc_box)
        self.add(self.loc_card)
        # ---- 見ていない場所(FR-12)
        nw = W.Card("見ていない場所", None, G.INFO, acc)
        nw.add(W.label(NOT_WATCHED, "Dim", wrap=True))
        self.add(nw)
        # ---- 設定
        sc = W.Card("設定", None, G.SETTINGS, acc)
        self.sw_runonce = W.ToggleSwitch(bool(self.m.cfg["notify_runonce"]), acc)
        self.sw_runonce.toggled.connect(_guard(lambda v: self.m.set_option("notify_runonce", bool(v))))
        sc.add(W.SettingRow("1回だけの物が増えたときも知らせる",
                            "インストーラーが再起動の後の仕上げに使うことが多いので、ふだんは知らせません。", self.sw_runonce))
        self.sw_running = W.ToggleSwitch(bool(self.m.cfg["show_running"]), acc)
        self.sw_running.toggled.connect(_guard(lambda v: self.m.set_option("show_running", bool(v))))
        sc.add(W.SettingRow("動いているかを調べる", "画面を開いたときと『読み直す』のときだけ、動いているプログラムの名前を読みます。",
                            self.sw_running))
        self.poll = QSpinBox()
        self.poll.setRange(5, 240)
        self.poll.setSuffix(" 分")
        self.poll.setValue(int(self.m.cfg["poll_minutes"]))
        self.poll.editingFinished.connect(_guard(lambda: self._set_int("poll_minutes", self.poll)))
        sc.add(W.SettingRow("知らせを置けない場所を確かめる間隔", "変わると見張りを起動し直します。", self.poll))
        self.add(sc)
        self.finish()
        self.m.notifier.changed.connect(self._refresh)
        self._refresh()

    # ------------------------------------------------------------ 表示
    @_guard
    def _refresh(self) -> None:
        m = self.m
        snoozed = m.snoozed()
        n_new = m.new_count()
        if snoozed:
            self.pill.set_state("off", "一時停止中は見ていません")
        elif m.last_scan is None:
            self.pill.set_state("info", "読んでいます")
        elif n_new:
            self.pill.set_state("warn", f"新しい物 {n_new} 件")
        else:
            self.pill.set_state("ok", "見張っています")
        texts: list[str] = []
        if m.banner is not None:
            kind, n = m.banner
            if kind == "baseline":
                texts.append(f"今ある {n} 個を覚えました。これから増えたら知らせます。")
            else:
                texts.append("記録が読めなかったので、今ある物を覚え直しました。")
        if m.watcher.degraded:
            texts.append(f"見張りを続けられませんでした。{m.cfg['poll_minutes']} 分ごとに確かめています。")
        self.notice_label.setText("\n".join(texts))
        self.notice.setVisible(bool(texts))
        self._fill_new()
        self._fill_table()
        self._fill_locations()

    def _fill_new(self) -> None:
        _clear(self.new_box)
        rows = self.m.new_items()
        self.ack_all_btn.setEnabled(bool(rows))
        if not rows:
            row = QHBoxLayout()
            row.setSpacing(10)
            g = W.Glyph(G.CHECK, 14, T.SUCCESS)
            g.setFixedSize(26, 26)
            row.addWidget(g)
            row.addWidget(W.label("新しく増えた物はありません。増えたら、ここと通知で知らせます。", "Dim", wrap=True), 1)
            self.new_box.addLayout(row)
            return
        for v in rows:
            self.new_box.addWidget(self._new_row(v))

    def _new_row(self, v: ViewItem) -> QWidget:
        it = v.item
        box = QFrame()
        box.setObjectName("Inset")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(W.label(it.display_name, "H3"))
        for mk in v.marks:
            head.addWidget(_mark_chip(mk))
        head.addStretch(1)
        lay.addLayout(head)
        loc = LOC_BY_KEY.get(it.loc)
        if loc is not None:
            lay.addWidget(W.label(f"{loc.label} ─ {loc.official}", "Mute", wrap=True))
        cmd = W.label(it.command, "Dim", wrap=True)
        cmd.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(cmd)
        if v.first_seen:
            lay.addWidget(W.label(f"見つけた日時: {_when(v.first_seen)}", "Mute"))
        btns: list[QWidget | int | None] = [
            W.button("確かめた", "primary", G.CHECK, _guard(lambda k=v.key: self.m.ack(k))),
            W.button("コマンドをコピー", "ghost", G.COPY, _guard(lambda i=it: self._copy(i))),
        ]
        if it.folder_path:
            btns.append(W.button("フォルダを開く", "ghost", G.FOLDER, _guard(lambda i=it: self.m.open_folder(i))))
        btns.append(None)
        lay.addLayout(W.hbox(*btns))
        return box

    def _fill_table(self) -> None:
        rows = self.m.view_items()
        self._rows = rows
        t = self.table
        t.setRowCount(len(rows))
        for r, v in enumerate(rows):
            it = v.item
            loc = LOC_BY_KEY.get(it.loc)
            cells = ["・".join(v.marks), it.display_name, loc.label if loc else it.loc, it.command,
                     "動いています" if v.running else ""]
            for c, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                if c == 2 and loc is not None:
                    cell.setToolTip(loc.official)
                if c == 3:
                    cell.setToolTip(text)
                t.setItem(r, c, cell)
            if it.folder_path:
                t.setCellWidget(r, 5, W.button("フォルダを開く", "ghost", G.FOLDER, _guard(lambda i=it: self.m.open_folder(i))))
            else:
                t.removeCellWidget(r, 5)
        if not rows:
            self.detail.setText("まだ読んでいません。" if self.m.last_scan is None else "自動で起動する物はありません。")
        elif self.table.selectionModel() is None or not self.table.selectionModel().selectedRows():
            self.detail.setText(DETAIL_HINT)

    def _fill_locations(self) -> None:
        _clear(self.loc_box)
        res = self.m.last_scan
        for loc in LOCATIONS:
            mode = self.m.location_mode(loc.key)
            r = res.locs.get(loc.key) if res else None
            if mode == MODE_UNREADABLE:
                state, color = "読めませんでした(管理者でないと見られない場所かもしれません)", T.WARN
            elif mode == MODE_NOTIFY:
                state, color = "変わるとすぐに気づけます", T.SUCCESS
            else:
                state, color = f"この場所は {self.m.cfg['poll_minutes']} 分ごとに確かめています", T.TEXT_DIM
            n = f"{len(r.items)} 個" if r is not None and r.status == "ok" else ("なし" if r is not None else "")
            row = QHBoxLayout()
            row.setSpacing(10)
            view = {_win32.KEY_WOW64_64KEY: "(64 ビット)", _win32.KEY_WOW64_32KEY: "(32 ビット)"}.get(loc.view, "")
            name = W.label(loc.label + view)
            name.setToolTip(loc.official)
            row.addWidget(name, 1)
            row.addWidget(W.label(n, "Mute"))
            st = QLabel(state)
            st.setStyleSheet(f"color: {color}; font-size: 12px;")
            row.addWidget(st)
            self.loc_box.addLayout(row)

    # ------------------------------------------------------------ 操作
    @_guard
    def _on_select(self) -> None:
        sel = self.table.selectionModel().selectedRows()
        if not sel:
            return
        r = sel[0].row()
        if 0 <= r < len(self._rows):
            v = self._rows[r]
            loc = LOC_BY_KEY.get(v.item.loc)
            self.detail.setText(f"{v.item.display_name}({loc.official if loc else v.item.loc})\n{v.item.command}")

    @_guard
    def _rescan(self) -> None:
        self.m.rescan()

    @_guard
    def _ack_all(self) -> None:
        self.m.ack_all()

    @_guard
    def _open_settings(self) -> None:
        self.m.open_settings()

    @_guard
    def _open_taskmgr(self) -> None:
        self.m.open_taskmgr()

    def _copy(self, it: StartupItem) -> None:
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(self.m.copy_text(it))

    def _set_int(self, key: str, box: QSpinBox) -> None:
        if int(self.m.cfg[key]) != box.value():
            self.m.set_option(key, box.value())

    def showEvent(self, e: Any) -> None:  # noqa: N802
        super().showEvent(e)
        if not self._opened:
            self._opened = True
            try:
                self.m.page_opened()
            except Exception as ex:  # noqa: BLE001
                _log.warning("page_opened failed: %s", type(ex).__name__)
