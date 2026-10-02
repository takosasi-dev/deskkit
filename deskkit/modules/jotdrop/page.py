# Control Center の JotDrop 画面: ヒーロー(キーの状態・「メモを書く」)→ 今日の件数・預かり中 → キー → 書き込み先 → 書式 →
# 今日の書き込み先と足される内容のプレビュー → 書けていないメモ(FR-19)。画面には本文とパスを出してよい(V-8)。
# 色は theme(T.*)と catalog のアクセント色を実行時に読む。設定は変えたらすぐ保存する(キーだけは起動し直す。FR-1)。
from __future__ import annotations

import functools
import logging
import re
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypeVar

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from deskkit import catalog
from deskkit.modules.jotdrop import compose, target, writer
from deskkit.modules.jotdrop.module import PRESET_BULLET, PRESET_HEADING, format_kind, hotkey_problem
from deskkit.ui import theme as T
from deskkit.ui import widgets as W
from deskkit.ui.theme import G

if TYPE_CHECKING:
    from deskkit.modules.jotdrop.module import JotDropModule

_log = logging.getLogger("deskkit.jotdrop")
F = TypeVar("F", bound=Callable[..., Any])
SAMPLE_TEXT = "(ここに書いた1行)"


def _guard(fn: F) -> F:
    """シグナルから呼ぶ処理の例外を Qt へ漏らさない。ログには型名だけ(本文・パスを含みうる。INV-3)。"""

    @functools.wraps(fn)
    def wrapper(*a: Any, **k: Any) -> Any:
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001
            _log.warning("page handler %s failed: %s", getattr(fn, "__name__", "?"), type(e).__name__)
            return None

    return wrapper  # type: ignore[return-value]


def _acc() -> str:
    return catalog.info("jotdrop").accent


def preview_text(data: bytes) -> str:
    """足すバイト列を、改行の形と空行が見える文にする。"""
    out: list[str] = []
    for m in re.finditer(r"([^\r\n]*)(\r\n|\n)?", data.decode("utf-8")):
        body, end = m.group(1), m.group(2)
        if not body and not end:
            continue
        mark = "↵(CRLF)" if end == "\r\n" else "↵" if end else ""
        out.append(f"{body or '(空行)'}  {mark}".rstrip())
    return "\n".join(out)


def pattern_hint(today: datetime) -> str:
    """「ファイル名」の欄の説明。{date} を今日の日付で見せる(固定の日付にしない)。"""
    return f"{{date}} が今日の日付({compose.expand('{date}', today, allowed=('date',))} の形)になります。.md か .txt。"


class JotDropPage(W.ScrollPage):
    def __init__(self, module: JotDropModule) -> None:
        super().__init__()
        self.m = module
        self._build_hero()
        self._build_key_banner()
        self._build_stats()
        self._build_key()
        self._build_target()
        self._build_format()
        self._build_preview()
        self._build_pending()
        self.finish()
        module.notifier.changed.connect(self._refresh)
        self._refresh()

    # ================================================================ 構築
    def _build_hero(self) -> None:
        hero = W.Hero("JotDrop", "どのアプリの上でも、キー1つで1行のメモを決めたファイルの末尾に書き足します。",
                      catalog.info("jotdrop").glyph, _acc())
        self.key_pill = W.StatusPill("", "off")
        hero.add_pill(self.key_pill)
        self.write_btn = W.button("メモを書く", "primary", G.EDIT, on_click=_guard(lambda: self.m.open_popup(None)))
        hero.add_action(self.write_btn)
        self.add(hero)

    def _build_key_banner(self) -> None:
        """J-1: キーが未設定・使えないときに先頭で知らせる。"""
        c = W.Card("メモ欄を出すキーを決めてください", "キーが無くても、トレイの「メモを書く」から使えます。", G.KEYBOARD, _acc())
        self.banner_edit = W.HotkeyEdit(str(self.m.cfg["hotkey"]))
        self.banner_edit.changed.connect(self._on_hotkey)
        c.add(W.SettingRow("キー", "Ctrl・Alt・Shift と一緒に押します。F12 と Windows キーは選べません。", self.banner_edit))
        self.banner_msg = W.label("", None, wrap=True)
        c.add(self.banner_msg)
        self.banner = c
        self.add(c)

    def _build_stats(self) -> None:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        self.tile_today = W.StatTile("今日書いたメモ", "0", G.EDIT, _acc())
        self.tile_pending = W.StatTile("書けていないメモ", "0", G.CLOCK, _acc())
        lay.addWidget(self.tile_today, 1)
        lay.addWidget(self.tile_pending, 1)
        self.add(row)

    def _build_key(self) -> None:
        c = W.Card("キー", "押すと入力欄が出ます。もう一度押すか Esc で閉じます(書きかけは残ります)。", G.KEYBOARD, _acc())
        self.key_edit = W.HotkeyEdit(str(self.m.cfg["hotkey"]))
        self.key_edit.changed.connect(self._on_hotkey)
        c.add(W.SettingRow("メモ欄を出すキー", "変えると JotDrop を起動し直して登録します。", self.key_edit))
        self.key_msg = W.label("", None, wrap=True)
        c.add(self.key_msg)
        self.key_card = c
        self.add(c)

    def _line_edit(self, value: str, placeholder: str = "") -> QLineEdit:
        e = QLineEdit(value)
        e.setPlaceholderText(placeholder)
        e.setMinimumWidth(260)
        return e

    def _build_target(self) -> None:
        c = W.Card("書き込み先", "決めたフォルダのファイルの末尾に足します。フォルダは作りません(既定のフォルダだけは作ります)。",
                   G.FOLDER, _acc())
        self.folder_edit = self._line_edit(str(self.m.cfg["folder"]), target.default_folder(self.m.documents()))
        self.folder_edit.editingFinished.connect(_guard(self._save_target))
        pick = W.button("選ぶ", "secondary", G.FOLDER, on_click=_guard(self._pick_folder))
        frow = QWidget()
        fl = QHBoxLayout(frow)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(8)
        fl.addWidget(self.folder_edit, 1)
        fl.addWidget(pick)
        c.add(W.SettingRow("フォルダ", "空なら「ドキュメント\\JotDrop」。ネットワーク上の場所には書きません。", None))
        c.add(frow)
        self.pattern_edit = self._line_edit(str(self.m.cfg["file_pattern"]), "{date}.md")
        self.pattern_edit.editingFinished.connect(_guard(self._save_target))
        hint = pattern_hint(self.m.now())
        pattern_row = W.SettingRow("ファイル名", hint, self.pattern_edit)
        # 説明の日付はモジュールの時計の今日(v0.4.1)。日付が変わったら _refresh_preview で書き直す
        self.pattern_hint = next(lb for lb in pattern_row.findChildren(QLabel) if lb.text() == hint)
        c.add(pattern_row)
        self.create_toggle = W.ToggleSwitch(bool(self.m.cfg["create_file"]), _acc())
        self.create_toggle.toggled.connect(_guard(lambda on: self._save({"create_file": bool(on)})))
        c.add(W.SettingRow("ファイルが無ければ作る", "日付が変わると新しいファイルになります。", self.create_toggle))
        self.header_edit = self._line_edit(str(self.m.cfg["new_file_header"]), "(なし)例: # {date} のメモ")
        self.header_edit.editingFinished.connect(_guard(lambda: self._save_text("new_file_header", self.header_edit,
                                                                                 compose.validate_header)))
        c.add(W.SettingRow("新しいファイルの先頭の行", "作ったときだけ先頭に書きます。{date} を使えます。", self.header_edit))
        self.target_msg = W.label("", None, wrap=True)
        c.add(self.target_msg)
        open_btn = W.button("書き込み先を開く", "secondary", G.OPEN, on_click=_guard(self.m.open_target))
        c.add(open_btn)
        self.add(c)

    def _build_format(self) -> None:
        c = W.Card("書式", "足す1行の形です。{time} は時刻(14:05)、{text} は書いた文。", G.TEXT, _acc())
        self.preset = W.Segmented([("bullet", "箇条書き"), ("heading", "見出し"), ("custom", "独自")], format_kind(self.m.cfg),
                                  _acc())
        self.preset.changed.connect(_guard(self._on_preset))
        c.add(W.SettingRow("形", "箇条書きは「- 14:05 本文」、見出しは「### [14:05] 本文」(前に空行)。", self.preset))
        self.format_edit = self._line_edit(str(self.m.cfg["line_format"]), "- {time} {text}")
        self.format_edit.editingFinished.connect(_guard(lambda: self._save_text("line_format", self.format_edit,
                                                                                 compose.validate_line_format)))
        c.add(W.SettingRow("1行の書式", "形の記号は YYYY MM DD HH mm。例: {date:YYYY年MM月DD日} {time:HH時mm分}", self.format_edit))
        self.blank_toggle = W.ToggleSwitch(bool(self.m.cfg["blank_line_before"]), _acc())
        self.blank_toggle.toggled.connect(_guard(lambda on: self._save({"blank_line_before": bool(on)})))
        c.add(W.SettingRow("前に空行を入れる", "末尾がすでに空行なら足しません。", self.blank_toggle))
        self.sep_edit = self._line_edit(str(self.m.cfg["separator"]), "(なし)例: ---")
        self.sep_edit.editingFinished.connect(_guard(lambda: self._save_text("separator", self.sep_edit,
                                                                              compose.validate_separator)))
        c.add(W.SettingRow("区切りの行", "書く前のファイルが空でなければ、1行の前に区切りと空行を入れます。", self.sep_edit))
        self.nl = W.Segmented([("auto", "ファイルに合わせる"), ("lf", "LF"), ("crlf", "CRLF")], str(self.m.cfg["newline"]), _acc())
        self.nl.changed.connect(_guard(lambda v: self._save({"newline": v})))
        c.add(W.SettingRow("改行の形", "ふつうは「ファイルに合わせる」(最後の改行と同じ形)。", self.nl))
        self.max_spin = QSpinBox()
        self.max_spin.setRange(20, 5000)
        self.max_spin.setValue(int(self.m.cfg["max_chars"]))
        self.max_spin.editingFinished.connect(_guard(lambda: self._save({"max_chars": self.max_spin.value()})))
        c.add(W.SettingRow("1行の長さの上限(文字)", None, self.max_spin))
        self.undo_spin = QSpinBox()
        self.undo_spin.setRange(1, 60)
        self.undo_spin.setValue(int(self.m.cfg["undo_minutes"]))
        self.undo_spin.editingFinished.connect(_guard(lambda: self._save({"undo_minutes": self.undo_spin.value()})))
        c.add(W.SettingRow("取り消せる時間(分)", "直前の1行だけを、書いてからこの分数のあいだ取り消せます。", self.undo_spin))
        self.mark_toggle = W.ToggleSwitch(bool(self.m.cfg["show_done_mark"]), _acc())
        self.mark_toggle.toggled.connect(_guard(lambda on: self._save({"show_done_mark": bool(on)})))
        c.add(W.SettingRow("「書きました」の印を出す", "1.5 秒だけ小さく出します。本文は出しません。", self.mark_toggle))
        self.format_msg = W.label("", None, wrap=True)
        c.add(self.format_msg)
        self.add(c)

    def _build_preview(self) -> None:
        c = W.Card("プレビュー", "今日の書き込み先と、足される内容です(↵ は改行)。", G.EYE, _acc())
        self.pv_path = W.label("", "Dim", wrap=True)
        self.pv_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        c.add(self.pv_path)
        grid = QHBoxLayout()
        grid.setSpacing(12)
        self.pv_existing = self._pv_box("ファイルがすでにあるとき")
        self.pv_new = self._pv_box("新しいファイルのとき")
        grid.addWidget(self.pv_existing[0], 1)
        grid.addWidget(self.pv_new[0], 1)
        c.add_layout(grid)
        self.add(c)

    def _pv_box(self, title: str) -> tuple[QWidget, QLabel]:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        v.addWidget(W.label(title, "Eyebrow"))
        lb = QLabel("")
        lb.setObjectName("Inset")
        lb.setWordWrap(True)
        lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lb.setStyleSheet(f"font-family: Consolas, 'Cascadia Mono', monospace; padding: 8px 10px; color: {T.TEXT};")
        v.addWidget(lb)
        return box, lb

    def _build_pending(self) -> None:
        c = W.Card("書けていないメモ", "書けなかったメモを預かっています。使用中・開けなかったものは 60 秒おきに自動で書き直します。",
                   G.CLOCK, _acc())
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["時刻", "本文", "書き込み先", "理由", ""])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMinimumHeight(120)
        c.add(self.table)
        self.pending_empty = W.label("預かっているメモはありません。", "Mute")
        c.add(self.pending_empty)
        self.add(c)

    # ================================================================ 反映
    @_guard
    def _refresh(self) -> None:
        cfg = self.m.cfg
        ok, err = self.m.hotkey_ok, self.m.hotkey_error
        if not cfg["hotkey"]:
            self.key_pill.set_state("off", "キー未設定")
        elif ok:
            self.key_pill.set_state("ok", f"{cfg['hotkey']} で書けます")
        else:
            self.key_pill.set_state("error", "キーが使えません")
        # v0.4.1: キーの欄は同時に 2 つ出さない。未設定なら先頭の呼びかけのカードだけ、設定済みなら「キー」のカードだけ
        # (登録できなかったときも「キー」のカードに理由を出す)
        has_key = bool(cfg["hotkey"])
        self.banner.setVisible(not has_key)
        self.key_card.setVisible(has_key)
        for e in (self.banner_edit, self.key_edit):  # 切り替わった先の欄にも今のキーを出す
            if not e.hasFocus() and e.text() != str(cfg["hotkey"]):
                e.setText(str(cfg["hotkey"]))
        msg = err or ""
        for lb in (self.banner_msg, self.key_msg):
            lb.setText(msg)
            lb.setStyleSheet(f"color: {T.DANGER};")
            lb.setVisible(bool(msg))
        self.tile_today.set_value(str(self.m.today_count()))
        self.tile_pending.set_value(str(self.m.pending.count()))
        self._refresh_preview()
        self._refresh_pending()

    def _refresh_preview(self) -> None:
        now = self.m.now()
        self.pattern_hint.setText(pattern_hint(now))
        path = self.m.target_path(now)
        self.pv_path.setText(f"今日の書き込み先: {path}")
        line = compose.expand(str(self.m.cfg["line_format"]), now, SAMPLE_TEXT)
        fr = self.m.framing()
        existing, _ = writer.build_bytes("(前の行)\n".encode(), False, False, line, fr, now)
        new, _ = writer.build_bytes(b"", True, True, line, fr, now)
        self.pv_existing[1].setText("(前の行)\n" + preview_text(existing))
        self.pv_new[1].setText(preview_text(new))

    def _refresh_pending(self) -> None:
        items = self.m.pending.items()
        self.table.setRowCount(len(items))
        for i, it in enumerate(items):
            dt = it.created_dt()
            self.table.setItem(i, 0, QTableWidgetItem(dt.strftime("%m/%d %H:%M") if dt else "—"))
            self.table.setItem(i, 1, QTableWidgetItem(it.line))
            p = QTableWidgetItem(it.path)
            p.setToolTip(it.path)
            self.table.setItem(i, 2, p)
            self.table.setItem(i, 3, QTableWidgetItem(target.REASON_TEXT.get(it.reason, it.reason)))
            ops = QWidget()
            ol = QHBoxLayout(ops)
            ol.setContentsMargins(4, 0, 4, 0)
            ol.setSpacing(4)
            ol.addWidget(W.icon_button(G.REFRESH, "もう一度書く", _guard(functools.partial(self.m.rewrite, it.id))))
            ol.addWidget(W.icon_button(G.COPY, "コピー", _guard(functools.partial(self._copy, it.line))))
            ol.addWidget(W.icon_button(G.DELETE, "捨てる", _guard(functools.partial(self._discard, it.id))))
            self.table.setCellWidget(i, 4, ops)
        self.table.setVisible(bool(items))
        self.pending_empty.setVisible(not items)

    # ================================================================ 操作
    def _say(self, lb: QLabel, text: str, ok: bool) -> None:
        lb.setText(text)
        lb.setStyleSheet(f"color: {T.SUCCESS if ok else T.DANGER};")
        lb.setVisible(bool(text))

    def _save(self, changes: dict[str, Any], lb: QLabel | None = None, restart: bool = False) -> bool:
        err = self.m.save_settings(changes, restart=restart)
        if err:
            W.message(self.window(), "保存できませんでした", err, kind="error")
            return False
        if lb is not None:
            self._say(lb, "保存しました", True)
        self._refresh_preview()
        return True

    @_guard
    def _on_hotkey(self, text: str) -> None:
        problem = hotkey_problem(text)
        if problem:
            self._say(self.key_msg, problem, False)
            self._say(self.banner_msg, problem, False)
            for e in (self.key_edit, self.banner_edit):
                e.setText(str(self.m.cfg["hotkey"]))
            return
        self._save({"hotkey": text}, restart=True)   # FR-1: 起動し直して host に登録し直させる

    @_guard
    def _pick_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self.window(), "書き込み先のフォルダを選ぶ", self.m.folder())
        if d:
            self.folder_edit.setText(d.replace("/", "\\"))
            self._save_target()

    def _save_target(self) -> None:
        folder = self.folder_edit.text().strip()
        pattern = self.pattern_edit.text().strip() or "{date}.md"
        err = target.validate_folder(folder) or compose.validate_pattern(pattern, self.m.now())
        if err:
            self._say(self.target_msg, err, False)
            return
        if folder == self.m.cfg["folder"] and pattern == self.m.cfg["file_pattern"]:
            return
        if self._save({"folder": folder, "file_pattern": pattern}):
            self._say(self.target_msg, "保存しました。次に書くときは、最初の1回だけ書き込み先を確かめます。", True)

    def _save_text(self, key: str, edit: QLineEdit, check: Callable[[str], str | None]) -> None:
        v = edit.text()
        err = check(v)
        lb = self.target_msg if key == "new_file_header" else self.format_msg
        if err:
            self._say(lb, err, False)
            return
        if v == self.m.cfg[key]:
            return
        if self._save({key: v}, lb):
            self.preset.set_value(format_kind(self.m.cfg))

    @_guard
    def _on_preset(self, v: str) -> None:
        pair = {"bullet": PRESET_BULLET, "heading": PRESET_HEADING}.get(v)
        if pair is None:
            self.format_edit.setFocus()
            return
        if self._save({"line_format": pair[0], "blank_line_before": pair[1]}, self.format_msg):
            self.format_edit.setText(pair[0])
            self.blank_toggle.set_checked_silent(pair[1])

    def _copy(self, line: str) -> None:
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(line)

    def _discard(self, item_id: str) -> None:
        ok, _ = W.confirm(self.window(), "このメモを捨てますか?", "預かっているメモを消します。ファイルには書きません。",
                          ok_text="捨てる", glyph=G.DELETE)
        if ok:
            self.m.discard(item_id)
