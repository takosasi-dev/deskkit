# Control Center の ClipShelf 画面。モード切替・状態・件数・直近の判定(理由コード/コピー元/時刻のみ)・定型文の管理・
# 除外設定・短命記録・モード中の停止・ホットキー・保存上限・自動貼り付け・全消去。履歴の本文はここに出さない(パレットだけ)。
# 設定を変える操作は即保存する。シグナルから呼ぶ処理はすべて guard で例外を握る。
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from deskkit.modules.clipshelf import policy
from deskkit.modules.clipshelf.config import SHORT_LIVED_MAX_MINUTES, normalize_exe
from deskkit.modules.clipshelf.editor import SnippetEditor
from deskkit.modules.clipshelf.module import REASON_LABELS
from deskkit.modules.clipshelf.palette import guard
from deskkit.ui import theme as T
from deskkit.ui import widgets as W
from deskkit.ui.theme import G

if TYPE_CHECKING:
    from deskkit.modules.clipshelf.module import ClipShelfModule

RETENTION_DEBOUNCE_MS = 700
_REASON_KIND = {
    policy.RECORDED: "ok", policy.OBSERVE_ONLY: "info", policy.SELF_ORIGIN: "off", policy.DUPLICATE: "off",
}


def _spin(value: int, lo: int, hi: int, suffix: str = "", special: str | None = None) -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setValue(value)
    s.setSuffix(suffix)
    s.setMinimumWidth(130)
    s.setMinimumHeight(34)
    s.setAlignment(Qt.AlignmentFlag.AlignRight)
    s.setKeyboardTracking(False)
    if special:
        s.setSpecialValueText(special)
    return s


class _Note(QFrame):
    """色付きの注意書き。set_note で色と文言を変えられる。"""

    def __init__(self, text: str, color: str, glyph: str) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(10)
        self.glyph = W.Glyph(glyph, 15, color)
        lay.addWidget(self.glyph, 0, Qt.AlignmentFlag.AlignTop)
        self.label = W.label(text, None, wrap=True)
        lay.addWidget(self.label, 1)
        self.set_note(color, text)

    def set_note(self, color: str, text: str) -> None:
        self.setStyleSheet(f"QFrame {{ background: {T.alpha(color, 0.08)}; border: 1px solid {T.alpha(color, 0.30)}; border-radius: 10px; }}")
        self.glyph.setStyleSheet(f"color: {color}; background: transparent; border: none;")
        self.label.setStyleSheet(f"color: {T.TEXT_DIM}; background: transparent; border: none; font-size: 12px;")
        self.label.setText(text)


def _note(text: str, color: str, glyph: str) -> _Note:
    return _Note(text, color, glyph)


class ClipShelfPage(W.ScrollPage):
    def __init__(self, module: ClipShelfModule) -> None:
        super().__init__()
        self.m = module
        self.accent = module.accent
        self._current_snippet: int | None = None
        self._dirty = False
        self._build_hero()
        self._build_tiles()
        self._build_decisions()
        self._build_snippets()
        self._build_exclusions()
        self._build_short_lived()
        self._build_mode_pause()
        self._build_hotkeys()
        self._build_retention()
        self._build_auto_paste()
        self._build_danger()
        self.finish()
        self.m.notifier.changed.connect(guard(self._refresh))
        self._clock = QTimer(self)
        self._clock.setInterval(30_000)
        self._clock.timeout.connect(guard(self._refresh))
        self._clock.start()
        self._refresh()
        self._reload_snippet_list()

    # ================================================================ 共通
    def _parent(self) -> QWidget | None:
        return self.window()

    def _save(self, mutate: Callable[[dict[str, Any]], None], *, restart: bool = False, text: str = "保存しました") -> bool:
        err = self.m.update_settings(mutate, restart=restart)
        if err:
            W.message(self._parent(), "保存できませんでした", err, kind="error")
            self._refresh()
            return False
        self._flash(text)
        return True

    def _flash(self, text: str, kind: str = "ok") -> None:
        self.saved_pill.set_state(kind, text)
        self.saved_pill.setVisible(True)
        self._saved_fx.setOpacity(1.0)
        self._saved_anim.stop()
        self._saved_timer.start()

    def _fade_saved(self) -> None:
        self._saved_anim.setStartValue(1.0)
        self._saved_anim.setEndValue(0.0)
        self._saved_anim.start()

    # ================================================================ ヒーロー
    def _build_hero(self) -> None:
        hero = W.Hero("ClipShelf", "暗号化したクリップボード履歴と定型文。パスワード類は除外形式・除外アプリ・一時停止で記録しません。",
                      G.CLIPBOARD, self.accent)
        self.mode_pill = W.StatusPill("", "warn")
        self.pause_pill = W.StatusPill("一時停止中", "off")
        self.store_pill = W.StatusPill("DB エラー", "error")
        self.saved_pill = W.StatusPill("保存しました", "ok")
        self._saved_fx = QGraphicsOpacityEffect(self.saved_pill)
        self.saved_pill.setGraphicsEffect(self._saved_fx)
        self._saved_anim = QPropertyAnimation(self._saved_fx, b"opacity", self)
        self._saved_anim.setDuration(600)
        self._saved_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._saved_timer = QTimer(self)
        self._saved_timer.setSingleShot(True)
        self._saved_timer.setInterval(1600)
        self._saved_timer.timeout.connect(guard(self._fade_saved))
        self.saved_pill.setVisible(False)
        for p in (self.mode_pill, self.pause_pill, self.store_pill, self.saved_pill):
            hero.add_pill(p)
        self.mode_seg = W.Segmented([("observe", "観察(記録しない)"), ("record", "記録する")], self.m.config.mode, self.accent)
        self.mode_seg.changed.connect(guard(self._on_mode))
        hero.add_action(self.mode_seg)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.pause_toggle = W.ToggleSwitch(self.m.paused, T.WARN)
        self.pause_toggle.toggled.connect(guard(lambda v: self.m.set_paused(bool(v))))
        row.addWidget(W.label("一時停止", "Dim"))
        row.addWidget(self.pause_toggle)
        row.addSpacing(6)
        row.addWidget(W.button("パレットを開く", "primary", G.SEARCH, on_click=guard(self.m.open_palette)))
        holder = QWidget()
        holder.setLayout(row)
        hero.add_action(holder)
        self.add(hero)

    def _on_mode(self, mode: str) -> None:
        def mut(s: dict[str, Any]) -> None:
            s["mode"] = mode

        self._save(mut, text="記録を始めました" if mode == "record" else "観察モードにしました")

    # ================================================================ 数値タイル
    def _build_tiles(self) -> None:
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        self.t_hist = W.StatTile("履歴", "0", G.CLIPBOARD, self.accent)
        self.t_pin = W.StatTile("ピン留め", "0", G.PIN, T.ACCENT)
        self.t_snip = W.StatTile("定型文", "0", G.SPARKLE, T.ACCENT_2)
        self.t_excl = W.StatTile("今日の除外(起動後)", "0", G.SHIELD, T.SUCCESS)
        for t in (self.t_hist, self.t_pin, self.t_snip, self.t_excl):
            lay.addWidget(t, 1)
        self.add(box)

    # ================================================================ 直近の判定
    def _build_decisions(self) -> None:
        card = W.Card("直近の判定", "コピーのたびの判定結果です。本文は表示しません(時刻・判定・コピー元アプリだけ)。", G.LIST, self.accent)
        self.excl_chips = QHBoxLayout()
        self.excl_chips.setSpacing(6)
        chips_holder = QWidget()
        chips_holder.setLayout(self.excl_chips)
        card.add(chips_holder)
        self.dec_table = QTableWidget(0, 4)
        self.dec_table.setHorizontalHeaderLabels(["時刻", "判定", "理由コード", "コピー元"])
        self.dec_table.verticalHeader().setVisible(False)
        self.dec_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.dec_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.dec_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.dec_table.setShowGrid(False)
        self.dec_table.setAlternatingRowColors(True)
        hh = self.dec_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.dec_table.setFixedHeight(230)
        self.dec_empty = W.label("まだ判定はありません。何かコピーするとここに出ます。", "Mute")
        card.add(self.dec_table)
        card.add(self.dec_empty)
        self.add(card)

    # ================================================================ 定型文
    def _build_snippets(self) -> None:
        card = W.Card("定型文", "パレットの「定型文」タブから呼び出します。本文は暗号化して保存します。", G.SPARKLE, self.accent)
        body = QHBoxLayout()
        body.setSpacing(14)
        left = QVBoxLayout()
        left.setSpacing(8)
        self.snip_list = QListWidget()
        self.snip_list.setFixedWidth(230)
        self.snip_list.setMinimumHeight(300)
        self.snip_list.currentItemChanged.connect(guard(lambda *_a: self._on_snippet_selected()))
        left.addWidget(self.snip_list, 1)
        left.addWidget(W.button("新しい定型文", "secondary", G.ADD, on_click=guard(self._new_snippet)))
        body.addLayout(left)
        right = QVBoxLayout()
        right.setSpacing(8)
        self.snip_editor = SnippetEditor(self.m.snippet_preview, self.accent)
        self.snip_editor.edited.connect(guard(self._on_snippet_edited))
        right.addWidget(self.snip_editor, 1)
        btns = QHBoxLayout()
        self.snip_state = W.label("", "Mute")
        btns.addWidget(self.snip_state)
        btns.addStretch(1)
        self.snip_del = W.button("削除", "danger", G.DELETE, on_click=guard(self._delete_snippet))
        self.snip_save = W.button("保存", "primary", G.SAVE, on_click=guard(self._save_snippet))
        btns.addWidget(self.snip_del)
        btns.addWidget(self.snip_save)
        right.addLayout(btns)
        body.addLayout(right, 1)
        card.add_layout(body)
        self.add(card)

    def _reload_snippet_list(self, select_id: int | None = None) -> None:
        store = self.m.store
        items = sorted(store.snippets(), key=lambda i: (i.name or "").casefold()) if store else []
        self.snip_list.blockSignals(True)
        self.snip_list.clear()
        target_row = -1
        for row, it in enumerate(items):
            li = QListWidgetItem(it.name or "無題の定型文")
            li.setData(Qt.ItemDataRole.UserRole, it.id)
            self.snip_list.addItem(li)
            if it.id == (select_id if select_id is not None else self._current_snippet):
                target_row = row
        self.snip_list.blockSignals(False)
        if target_row >= 0:
            self.snip_list.setCurrentRow(target_row)
        elif items and self._current_snippet is None and select_id is None:
            self.snip_list.setCurrentRow(0)
        else:
            self._on_snippet_selected()
        enabled = store is not None
        self.snip_editor.setEnabled(enabled)
        self.snip_save.setEnabled(enabled)

    def _on_snippet_selected(self) -> None:
        li = self.snip_list.currentItem()
        store = self.m.store
        item = store.get(int(li.data(Qt.ItemDataRole.UserRole))) if (li is not None and store is not None) else None
        if item is None:
            self._current_snippet = None
            if not self._dirty:
                self.snip_editor.set_values("", "")
            self.snip_state.setText("新しい定型文を入力して「保存」")
            self.snip_del.setEnabled(False)
        else:
            self._current_snippet = item.id
            self.snip_editor.set_values(item.name or "", item.text)
            self.snip_state.setText(f"最終使用: {item.last_used_at.strftime('%Y-%m-%d %H:%M')}")
            self.snip_del.setEnabled(True)
        self._dirty = False

    def _on_snippet_edited(self) -> None:
        self._dirty = True
        self.snip_state.setText("未保存の変更があります")

    def _new_snippet(self) -> None:
        self.snip_list.blockSignals(True)
        self.snip_list.clearSelection()
        self.snip_list.setCurrentRow(-1)
        self.snip_list.blockSignals(False)
        self._current_snippet = None
        self._dirty = False
        self.snip_editor.set_values("", "")
        self.snip_editor.name.setFocus()
        self.snip_state.setText("新しい定型文を入力して「保存」")
        self.snip_del.setEnabled(False)

    def _save_snippet(self) -> None:
        name, text = self.snip_editor.values()
        if not text.strip():
            W.message(self._parent(), "保存できません", "本文が空です。", kind="warn")
            return
        item = self.m.save_snippet(self._current_snippet, name, text)
        if item is None:
            W.message(self._parent(), "保存できませんでした", "定型文を保存できませんでした(DB または暗号化のエラー。詳細はログ)。", kind="error")
            return
        self._dirty = False
        self._current_snippet = item.id
        self._reload_snippet_list(item.id)
        self._flash("定型文を保存しました")

    def _delete_snippet(self) -> None:
        if self._current_snippet is None or self.m.store is None:
            return
        item = self.m.store.get(self._current_snippet)
        if item is None:
            return
        ok, _ = W.confirm(self._parent(), "定型文を削除", f"「{item.name or '無題の定型文'}」を削除します。元に戻せません。",
                          ok_text="削除", danger=True)
        if not ok:
            return
        self.m.delete_item(item)
        self._current_snippet = None
        self._dirty = False
        self._reload_snippet_list()
        self._flash("定型文を削除しました")

    # ================================================================ 除外設定
    def _build_exclusions(self) -> None:
        cfg = self.m.config
        card = W.Card("記録しないもの", "パスワードマネージャーなど、履歴に残したくないコピーを除外します。", G.SHIELD, self.accent)
        card.add(_note("除外形式(ExcludeClipboardContentFromMonitorProcessing / CanIncludeInClipboardHistory)が付いたコピーは、"
                       "設定に関係なく常に記録しません。本文を読む前に判定します。", T.SUCCESS, G.LOCK))
        pick = W.button("実行中のアプリから選ぶ", "secondary", G.APP, on_click=guard(self._pick_running))
        self.excl_editor = W.StringListEditor(sorted(cfg.exclude_exes), "exe 名(例: someapp.exe)", normalize_exe,
                                              height=130, extra_buttons=[pick])
        self.excl_editor.changed.connect(guard(self._on_exclude_changed))
        card.add(W.SettingRow("除外するアプリ", "コピー元(クリップボードの所有者)の exe 名が一致したら記録しません。大文字小文字は区別しません。",
                              None, G.APP))
        card.add(self.excl_editor)
        self.owner_seg = W.Segmented([("skip", "記録しない"), ("record", "記録する")], cfg.unknown_owner_policy, self.accent)
        self.owner_seg.changed.connect(guard(self._on_owner_policy))
        card.add(W.SettingRow("コピー元が分からないとき", "所有者ウィンドウが無い・管理者権限のアプリ等で exe 名が取れない場合。"
                              "安全のため既定は「記録しない」です。", self.owner_seg, G.WARNING))
        self.add(card)

    def _on_exclude_changed(self, items: list[str]) -> None:
        def mut(s: dict[str, Any]) -> None:
            s["exclude_exes"] = sorted({normalize_exe(x) for x in items if normalize_exe(x)})

        self._save(mut)

    def _on_owner_policy(self, v: str) -> None:
        def mut(s: dict[str, Any]) -> None:
            s["unknown_owner_policy"] = v

        self._save(mut)

    def _pick_running(self) -> None:
        names = self.m.running_exe_names()
        chosen = pick_exes_dialog(self._parent(), names, set(self.excl_editor.items()), self.accent)
        for n in chosen:
            self.excl_editor.add_value(n)

    # ================================================================ 短命記録(アプリ別、C3)
    def _build_short_lived(self) -> None:
        cfg = self.m.config
        card = W.Card("短命記録(アプリ別)", "指定したアプリからのコピーは記録しますが、決めた時間が過ぎると自動で消します。",
                      G.CLOCK, self.accent)
        card.add(_note("判定はコピー元の exe 名だけで、内容は見ません。期限は記録したときに項目ごとに決まり、"
                       "後から設定を変えても記録済みの項目の期限は変わりません。ピン留めした項目は消しません。",
                       T.INFO, G.INFO))
        pick = W.button("実行中のアプリから選ぶ", "secondary", G.APP, on_click=guard(self._pick_running_short))
        self.short_editor = W.StringListEditor(sorted(cfg.short_lived_exes), "exe 名(例: someapp.exe)", normalize_exe,
                                               height=100, extra_buttons=[pick])
        self.short_editor.changed.connect(guard(self._on_short_exes_changed))
        card.add(W.SettingRow("対象のアプリ", "空なら短命記録は使いません(既定)。", None, G.APP))
        card.add(self.short_editor)
        self.sp_short = _spin(cfg.short_lived_minutes, 1, SHORT_LIVED_MAX_MINUTES, " 分")
        self.sp_short.valueChanged.connect(guard(self._on_short_minutes))
        card.add(W.SettingRow("消すまでの時間", "記録してからこの時間が過ぎたら消します(1分ごとに確認)。", self.sp_short, G.CLOCK))
        self.short_state = W.label("", "Mute")
        card.add(self.short_state)
        self.add(card)

    def _on_short_exes_changed(self, items: list[str]) -> None:
        def mut(s: dict[str, Any]) -> None:
            s.setdefault("short_lived", {})["exes"] = sorted({normalize_exe(x) for x in items if normalize_exe(x)})

        self._save(mut)

    def _on_short_minutes(self, v: int) -> None:
        def mut(s: dict[str, Any]) -> None:
            s.setdefault("short_lived", {})["minutes"] = int(v)

        self._save(mut)

    def _pick_running_short(self) -> None:
        chosen = pick_exes_dialog(self._parent(), self.m.running_exe_names(), set(self.short_editor.items()), self.accent,
                                  prompt="短命記録にしたいアプリを選んでください(複数可)。exe 名だけを保存します。")
        for n in chosen:
            self.short_editor.add_value(n)

    # ================================================================ モード中は止める(M3)
    def _build_mode_pause(self) -> None:
        card = W.Card("自動で記録を止める", "ModeShift のモード中や、DeskKit のスヌーズ中は記録しません。止めた・再開したは"
                      "通知せず、こことトレイの状態にだけ表示します。", G.PAUSE, self.accent)
        self.mode_list = QListWidget()
        self.mode_list.itemChanged.connect(guard(lambda _it: self._on_mode_list_changed()))
        card.add(W.SettingRow("このモード中は記録しない", "チェックしたモードに ModeShift が切り替えたら記録を止め、"
                              "元に戻したら再開します。", None, G.PAUSE))
        card.add(self.mode_list)
        self.mode_empty = W.label("ModeShift にモードがありません。ModeShift でモードを作るとここで選べます。", "Mute", wrap=True)
        card.add(self.mode_empty)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.mode_state = W.label("", "Dim", wrap=True)
        row.addWidget(self.mode_state, 1)
        self.mode_clear_btn = W.button("停止を解除", "ghost", G.PLAY, on_click=guard(self._clear_mode_pause),
                                       tooltip="ModeShift の復帰を取りこぼしたときに、モードによる停止を手で解除します")
        row.addWidget(self.mode_clear_btn)
        card.add_layout(row)
        self.add(card)
        self._reload_mode_list()

    def _reload_mode_list(self) -> None:
        chosen = list(self.m.config.pause_in_modes)
        choices = self.m.mode_choices()
        known = {n for n, _ in choices}
        self.mode_list.blockSignals(True)
        self.mode_list.clear()
        rows = [(n, lb if lb == n else f"{lb}({n})") for n, lb in choices]
        rows += [(n, f"{n}(ModeShift に見当たらないモード)") for n in chosen if n not in known]
        for name, text in rows:
            li = QListWidgetItem(text)
            li.setData(Qt.ItemDataRole.UserRole, name)
            li.setFlags(li.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            li.setCheckState(Qt.CheckState.Checked if name in chosen else Qt.CheckState.Unchecked)
            self.mode_list.addItem(li)
        self.mode_list.blockSignals(False)
        self.mode_list.setFixedHeight(min(200, 14 + 30 * max(1, len(rows))))  # 行数に合わせる(空白を作らない)
        self.mode_list.setVisible(bool(rows))
        self.mode_empty.setVisible(not rows)

    def _on_mode_list_changed(self) -> None:
        names: list[str] = []
        for i in range(self.mode_list.count()):
            li = self.mode_list.item(i)
            if li.checkState() == Qt.CheckState.Checked:
                names.append(str(li.data(Qt.ItemDataRole.UserRole)))

        def mut(s: dict[str, Any]) -> None:
            s["pause_in_modes"] = names

        self._save(mut)

    def _clear_mode_pause(self) -> None:
        self.m.clear_mode_pause()
        self._flash("モードによる停止を解除しました")

    def _refresh_mode_state(self) -> None:
        m = self.m
        mode = m.active_mode()
        texts: list[str] = []
        if m.mode_paused():
            texts.append(f"今はモード「{mode}」中のため記録を止めています。")
        elif mode:
            texts.append(f"今のモード: {mode}(記録は止めません)")
        if m.is_snoozed():
            texts.append("DeskKit がスヌーズ中のため記録を止めています。")
        self.mode_state.setText(" ".join(texts) or "今は自動で止めていません。")
        self.mode_clear_btn.setVisible(m.mode_paused())

    # ================================================================ ホットキー
    def _build_hotkeys(self) -> None:
        cfg = self.m.config
        card = W.Card("ホットキー", "競合して登録できなかったキーは起動時に通知します。変更するとモジュールを再起動します。", G.KEYBOARD, self.accent)
        rows = [
            ("open_palette", "パレットを開く", "検索パレットを開閉します。ゲーム・全画面の上では開きません。"),
            ("plain_text", "書式なしにする", "今のクリップボードをテキストだけに書き直します(除外形式は引き継ぎます)。"),
            ("toggle_pause", "記録の一時停止", "記録の一時停止と再開を切り替えます。"),
        ]
        for name, title, desc in rows:
            ed = W.HotkeyEdit(cfg.hotkeys.get(name, ""))
            ed.changed.connect(guard(lambda v, n=name: self._on_hotkey(n, v)))
            holder = QWidget()
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setSpacing(8)
            if cfg.hotkeys.get(name) and self.m.hotkey_ok.get(name) is False:
                hl.addWidget(W.StatusPill("競合・未登録", "error"))
            hl.addWidget(ed)
            card.add(W.SettingRow(title, desc, holder))
        self.add(card)

    def _on_hotkey(self, name: str, value: str) -> None:
        def mut(s: dict[str, Any]) -> None:
            s.setdefault("hotkeys", {})[name] = value

        self._save(mut, restart=True, text="ホットキーを保存しました")

    # ================================================================ 保存と上限
    def _build_retention(self) -> None:
        cfg = self.m.config
        card = W.Card("保存と上限", "上限を超えた履歴は古い順に自動で削除します(ピンと定型文は対象外)。", G.ARCHIVE, self.accent)
        self.sp_items = _spin(cfg.max_items, 0, 1_000_000, " 件", "無制限")
        self.sp_days = _spin(cfg.max_days, 0, 36500, " 日", "無制限")
        self.sp_chars = _spin(cfg.max_chars, 1, 10_000_000, " 文字")
        self.sp_debounce = _spin(cfg.debounce_ms, 0, 5000, " ms")
        self.sp_retry = _spin(cfg.open_retry, 1, 50, " 回")
        # 保持上限は下げると履歴が消える。ホイールや押し間違いで即削除しないよう、値が落ち着いてから(RETENTION_DEBOUNCE_MS)
        # まとめて判定し、消える件数があれば確認する(取消で元の値に戻す)。本体のホイール対策には頼らない。
        self._ret_timer = QTimer(self)
        self._ret_timer.setSingleShot(True)
        self._ret_timer.setInterval(RETENTION_DEBOUNCE_MS)
        self._ret_timer.timeout.connect(guard(self._apply_retention))
        self.sp_items.valueChanged.connect(guard(lambda _v: self._ret_timer.start()))
        self.sp_days.valueChanged.connect(guard(lambda _v: self._ret_timer.start()))
        self.sp_chars.valueChanged.connect(guard(lambda v: self._save_simple("max_chars", int(v))))
        self.sp_debounce.valueChanged.connect(guard(lambda v: self._save_simple("debounce_ms", int(v))))
        self.sp_retry.valueChanged.connect(guard(lambda v: self._save_simple("open_retry", int(v))))
        card.add(W.SettingRow("保持する件数", "ピン以外の履歴の上限。0 で無制限。", self.sp_items, G.LIST))
        card.add(W.SettingRow("保持する日数", "最後に使ってからの日数。0 で無制限。", self.sp_days, G.CLOCK))
        card.add(W.SettingRow("記録する最大文字数", "これを超えるコピーは切り詰めずに記録しません。", self.sp_chars, G.TEXT))
        card.add(W.divider())
        card.add(W.SettingRow("まとめる間隔(デバウンス)", "連続した更新通知をまとめて1回だけ処理します。", self.sp_debounce, G.LIGHTNING))
        card.add(W.SettingRow("クリップボードを開く再試行", "他のアプリが使用中のときに再試行する回数。", self.sp_retry, G.REFRESH))
        card.add(W.divider())
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        self.fmt_edits: dict[str, tuple[QLineEdit, QLabel]] = {}
        for row, (key, title) in enumerate((("date_format", "{date} の書式"), ("time_format", "{time} の書式"))):
            ed = QLineEdit(getattr(cfg, key))
            ed.setMaximumWidth(200)
            ed.setMinimumHeight(34)
            sample = W.label("", "Mute")
            ed.textChanged.connect(guard(lambda _t, k=key: self._update_fmt_sample(k)))
            ed.editingFinished.connect(guard(lambda k=key: self._save_fmt(k)))
            grid.addWidget(W.label(title), row, 0)
            grid.addWidget(ed, row, 1)
            grid.addWidget(sample, row, 2)
            self.fmt_edits[key] = (ed, sample)
            self._update_fmt_sample(key)
        grid.setColumnStretch(2, 1)
        card.add_layout(grid)
        card.add(W.label("strftime 形式(例: %Y-%m-%d、%H:%M、%Y年%m月%d日)。", "Mute"))
        self.add(card)

    def _apply_retention(self) -> None:
        """保持上限のスピンボックスの値が落ち着いたら呼ばれる。消える履歴があれば件数を示して確認する。"""
        self._ret_timer.stop()
        cfg = self.m.config
        items, days = int(self.sp_items.value()), int(self.sp_days.value())
        if (items, days) == (cfg.max_items, cfg.max_days):
            return
        n = self.m.retention_preview(items, days)
        if n > 0:
            ok, _ = W.confirm(self._parent(), "保持の上限を下げる",
                              f"この上限にすると、今ある履歴のうち {n:,} 件(ピン以外の古い順)がすぐに削除されます。"
                              "元に戻せません。", ok_text=f"{n:,} 件を削除して保存", danger=True)
            if not ok:
                self._revert_retention()
                return

        def mut(s: dict[str, Any]) -> None:
            r = s.setdefault("retention", {})
            r["max_items"] = items
            r["max_days"] = days

        if self._save(mut):
            self.m.monitor.trim(self.m.config)
        else:
            self._revert_retention()

    def _revert_retention(self) -> None:
        cfg = self.m.config
        for sp, v in ((self.sp_items, cfg.max_items), (self.sp_days, cfg.max_days)):
            sp.blockSignals(True)
            sp.setValue(v)
            sp.blockSignals(False)

    def _save_simple(self, key: str, value: Any) -> None:
        def mut(s: dict[str, Any]) -> None:
            s[key] = value

        self._save(mut)

    def _update_fmt_sample(self, key: str) -> None:
        ed, sample = self.fmt_edits[key]
        try:
            sample.setText("例: " + datetime.now().strftime(ed.text()) if ed.text() else "(空にはできません)")
        except ValueError:
            sample.setText("書式が不正です")

    def _save_fmt(self, key: str) -> None:
        ed, _ = self.fmt_edits[key]
        v = ed.text()
        if not v or v == getattr(self.m.config, key):
            return
        try:
            datetime.now().strftime(v)
        except ValueError:
            return
        self._save_simple(key, v)

    # ================================================================ 自動貼り付け
    def _build_auto_paste(self) -> None:
        cfg = self.m.config
        card = W.Card("自動貼り付け", "選んだ後に Ctrl+V を送るかどうか。既定は送らない(クリップボードに置いて元のウィンドウへ戻すだけ)。",
                      G.PASTE, self.accent)
        self.ap_seg = W.Segmented([("off", "送らない"), ("dry_run", "試運転(ログのみ)"), ("on", "送る")], cfg.auto_paste, self.accent)
        self.ap_seg.changed.connect(guard(self._on_auto_paste))
        card.add(W.SettingRow("動作", None, self.ap_seg, G.PASTE))
        self.ap_note = _note("", T.INFO, G.INFO)
        card.add(self.ap_note)
        self.deny_editor = W.StringListEditor(sorted(cfg.auto_paste_deny_exes), "送らないアプリの exe 名(例: WindowsTerminal.exe)",
                                              normalize_exe, height=100)
        self.deny_editor.changed.connect(guard(self._on_deny_changed))
        card.add(W.SettingRow("送らないアプリ", "Ctrl+V が別の意味になるアプリ(ターミナル等)を入れます。", None, G.TERMINAL))
        card.add(self.deny_editor)
        self._update_ap_note(cfg.auto_paste)
        self.add(card)

    def _update_ap_note(self, mode: str) -> None:
        texts = {
            "off": (T.INFO, "クリップボードに置いて、パレットを開く前のウィンドウへ戻すだけです。貼り付けは自分で Ctrl+V します。"),
            "dry_run": (T.WARN, "キー入力は送らず、「送るはずだった / 送らなかった理由」だけを操作ログ(ops.jsonl)に残します。"
                                "しばらく試して問題が無ければ「送る」にしてください。"),
            "on": (T.DANGER, "元のウィンドウに Ctrl+V を送ります。ゲーム・全画面・管理者権限(不明を含む)のウィンドウ、"
                             "フォーカスが変わった場合、送らないアプリには送りません。誤送信に注意してください。"),
        }
        color, text = texts.get(mode, texts["off"])
        self.ap_note.set_note(color, text)

    def _on_auto_paste(self, mode: str) -> None:
        if mode == "on":
            ok, _ = W.confirm(self._parent(), "自動貼り付けを有効にする",
                              "選んだ後、元のウィンドウに Ctrl+V を送ります。チャット等への誤送信に注意してください。"
                              "先に「試運転」で操作ログを確認することをおすすめします。", ok_text="有効にする", danger=True)
            if not ok:
                self.ap_seg.set_value(self.m.config.auto_paste)
                return

        def mut(s: dict[str, Any]) -> None:
            s["auto_paste"] = mode

        if self._save(mut):
            self._update_ap_note(mode)
        else:
            self.ap_seg.set_value(self.m.config.auto_paste)

    def _on_deny_changed(self, items: list[str]) -> None:
        def mut(s: dict[str, Any]) -> None:
            s["auto_paste_deny_exes"] = sorted({normalize_exe(x) for x in items if normalize_exe(x)})

        self._save(mut)

    # ================================================================ 危険な操作
    def _build_danger(self) -> None:
        card = W.Card("危険な操作", "取り消せない操作です。", G.WARNING, T.DANGER)
        card.setStyleSheet(f"QFrame#Card {{ background: {T.SURFACE}; border: 1px solid {T.alpha(T.DANGER, 0.45)};"
                           f" border-radius: 14px; }}")
        btn = W.button("履歴を全消去…", "danger", G.DELETE, on_click=guard(lambda: self.m.clear_all_interactive(self._parent())))
        card.add(W.SettingRow("履歴を全消去", "履歴を完全に削除して DB を圧縮します(VACUUM)。定型文は残ります。"
                              "ピンを消すか、今のクリップボードを空にするかは確認画面で選べます。", btn, G.DELETE))
        self.add(card)

    # ================================================================ 表示の更新
    def _refresh(self) -> None:
        m = self.m
        c = m.counts()
        self.t_hist.set_value(f"{c['history']:,}")
        self.t_pin.set_value(f"{c['pinned']:,}")
        self.t_snip.set_value(f"{c['snippets']:,}")
        excl = m.today_exclusions()
        self.t_excl.set_value(f"{sum(excl.values()):,}")
        if m.config.mode == "observe":
            self.mode_pill.set_state("warn", "観察モード(記録しない)")
        else:
            self.mode_pill.set_state("ok", "記録中")
        reasons = m.pause_reasons()
        if reasons:
            self.pause_pill.set_state("off", m.status_text())
        self.pause_pill.setVisible(bool(reasons))
        self._refresh_mode_state()
        n_short = m.store.short_lived_count() if m.store is not None else 0
        self.short_state.setText(f"自動で消える予定の履歴: {n_short:,} 件" if n_short else "")
        self.short_state.setVisible(bool(n_short))
        if m.store is None:
            self.store_pill.set_state("error", "DB エラー")
            self.store_pill.setToolTip(m.store_error or "")
            self.store_pill.setVisible(True)
        elif c["undecryptable"]:
            self.store_pill.set_state("warn", f"復号できない行 {c['undecryptable']} 件")
            self.store_pill.setToolTip("履歴の分は全消去で削除できます(定型文の分は残ります)")
            self.store_pill.setVisible(True)
        else:
            self.store_pill.setVisible(False)  # 全消去で復号できない行が無くなったら警告を消す
        if self.mode_seg.value() != m.config.mode:
            self.mode_seg.set_value(m.config.mode)
        if self.pause_toggle.isChecked() != m.paused:
            self.pause_toggle.set_checked_silent(m.paused)
        self._refresh_chips(excl)
        self._refresh_decisions()
        if m.store is not None and not self._dirty:
            ids = {int(self.snip_list.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self.snip_list.count())}
            if ids != {s.id for s in m.store.snippets()}:
                self._reload_snippet_list()

    def _refresh_chips(self, excl: dict[str, int]) -> None:
        while self.excl_chips.count():
            it = self.excl_chips.takeAt(0)
            wdg = it.widget() if it is not None else None
            if wdg is not None:
                wdg.hide()
                wdg.setParent(None)  # 次のイベントループを待たずに見えなくする
                wdg.deleteLater()
        if not excl:
            self.excl_chips.addWidget(W.label("今日はまだ除外がありません。", "Mute"))
        for reason, n in sorted(excl.items(), key=lambda kv: -kv[1]):
            pill = W.StatusPill(f"{REASON_LABELS.get(reason, reason)}  {n}", "warn" if reason != policy.PAUSED else "off")
            pill.setToolTip(reason)
            self.excl_chips.addWidget(pill)
        self.excl_chips.addStretch(1)

    def _refresh_decisions(self) -> None:
        decs = self.m.decisions()[:40]
        self.dec_table.setRowCount(len(decs))
        for row, d in enumerate(decs):
            cells = [
                d.ts.strftime("%H:%M:%S"),
                REASON_LABELS.get(d.reason, d.reason),
                d.reason,
                d.owner_exe or "(不明)",
            ]
            for col, text in enumerate(cells):
                it = QTableWidgetItem(text)
                if col == 1:
                    kind = _REASON_KIND.get(d.reason, "warn")
                    color = {"ok": T.SUCCESS, "info": T.INFO, "off": T.TEXT_DIM, "warn": T.WARN}[kind]
                    it.setForeground(QColor(color))
                if col == 2:
                    it.setForeground(QColor(T.TEXT_MUTE))
                self.dec_table.setItem(row, col, it)
        self.dec_table.setVisible(bool(decs))
        self.dec_empty.setVisible(not decs)


# ------------------------------------------------------------------ 実行中アプリから選ぶダイアログ
def pick_exes_dialog(parent: QWidget | None, names: list[str], already: set[str], accent: str,
                     prompt: str = "除外したいアプリを選んでください(複数可)。exe 名だけを保存します。") -> list[str]:
    dlg = W.StyledDialog(parent, "実行中のアプリから選ぶ", G.APP, accent, width=460)
    dlg.body.addWidget(W.label(prompt, "Dim", wrap=True))
    flt = QLineEdit()
    flt.setPlaceholderText("絞り込み")
    dlg.body.addWidget(flt)
    lst = QListWidget()
    lst.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
    lst.setMinimumHeight(300)
    for n in names:
        if n in already:
            continue
        lst.addItem(QListWidgetItem(n))
    dlg.body.addWidget(lst)

    def apply_filter(text: str) -> None:
        try:
            t = text.strip().lower()
            for i in range(lst.count()):
                it = lst.item(i)
                it.setHidden(bool(t) and t not in it.text())
        except Exception:  # noqa: BLE001
            pass

    flt.textChanged.connect(apply_filter)
    dlg.buttons.addWidget(W.button("キャンセル", "ghost", on_click=dlg.reject))
    ok: QPushButton = W.button("追加", "primary", G.ADD, on_click=dlg.accept)
    dlg.buttons.addWidget(ok)
    flt.setFocus()
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return []
    return [it.text() for it in lst.selectedItems()]

