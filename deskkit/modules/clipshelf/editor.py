# 定型文の編集部品(名前・本文・プレースホルダのチップ・展開プレビューと警告)と、それを載せた編集ダイアログ。
# プレビューは {clipboard} を本文ではなく目印で表示する(プレビューのためにクリップボードを読まない)。
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from deskkit.modules.clipshelf.snippets import Expansion
from deskkit.ui import theme as T
from deskkit.ui import widgets as W
from deskkit.ui.theme import G

PreviewFn = Callable[[str], Expansion]
CHIPS: tuple[tuple[str, str], ...] = (
    ("{date}", "今日の日付"),
    ("{time}", "現在の時刻"),
    ("{clipboard}", "今のクリップボードのテキスト"),
    ("{{", "「{」の文字"),
    ("}}", "「}」の文字"),
)


def _chip(text: str, tip: str, accent: str) -> QPushButton:
    b = QPushButton(text)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setToolTip(tip)
    b.setStyleSheet(
        f"QPushButton {{ background: {T.alpha(accent, 0.10)}; color: {accent}; border: 1px solid {T.alpha(accent, 0.35)};"
        f" border-radius: 11px; padding: 3px 10px; font-family: Consolas, 'Cascadia Mono', monospace; font-size: 12px; }}"
        f"QPushButton:hover {{ background: {T.alpha(accent, 0.22)}; }}"
    )
    return b


class SnippetEditor(QWidget):
    """名前+本文+チップ+ライブプレビュー。edited() は内容が変わるたびに出る。"""

    edited = Signal()

    def __init__(self, preview: PreviewFn, accent: str, *, compact: bool = False) -> None:
        super().__init__()
        self._preview = preview
        self._accent = accent
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.name = QLineEdit()
        self.name.setMinimumHeight(34)
        self.name.setPlaceholderText("名前(例: 住所・署名・定例の挨拶)")
        lay.addWidget(W.label("名前", "Eyebrow"))
        lay.addWidget(self.name)
        lay.addWidget(W.label("本文", "Eyebrow"))
        self.body = QPlainTextEdit()
        mono = QFont("Cascadia Mono")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setFamilies(["Cascadia Mono", "Consolas", "BIZ UDGothic", "MS Gothic"])
        mono.setPixelSize(13)
        self.body.setFont(mono)
        self.body.setPlaceholderText("本文。{date} {time} {clipboard} が使えます")
        self.body.setMinimumHeight(110 if compact else 150)
        lay.addWidget(self.body)
        chips = QHBoxLayout()
        chips.setSpacing(6)
        chips.addWidget(W.label("挿入:", "Mute"))
        for text, tip in CHIPS:
            b = _chip(text, tip, accent)
            b.clicked.connect(lambda _=False, t=text: self._insert(t))
            chips.addWidget(b)
        chips.addStretch(1)
        lay.addLayout(chips)
        pv = QFrame()
        pv.setObjectName("Inset")
        pl = QVBoxLayout(pv)
        pl.setContentsMargins(12, 10, 12, 10)
        pl.setSpacing(6)
        head = QHBoxLayout()
        head.addWidget(W.Glyph(G.EYE, 13, T.TEXT_DIM))
        head.addWidget(W.label("展開プレビュー", "Eyebrow"))
        head.addStretch(1)
        pl.addLayout(head)
        self.preview_label = QLabel()
        self.preview_label.setWordWrap(True)
        self.preview_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.preview_label.setTextFormat(Qt.TextFormat.PlainText)
        self.preview_label.setStyleSheet(f"color: {T.TEXT}; font-size: 13px;")
        self.preview_label.setMinimumHeight(48)
        pl.addWidget(self.preview_label)
        self.warn_label = QLabel()
        self.warn_label.setWordWrap(True)
        self.warn_label.setTextFormat(Qt.TextFormat.PlainText)
        self.warn_label.setStyleSheet(f"color: {T.WARN}; font-size: 12px;")
        pl.addWidget(self.warn_label)
        pv.setMinimumHeight(110)
        lay.addWidget(pv)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._refresh)
        self.name.textChanged.connect(self._changed)
        self.body.textChanged.connect(self._changed)
        self._refresh()

    def set_values(self, name: str, text: str) -> None:
        self.name.blockSignals(True)
        self.body.blockSignals(True)
        self.name.setText(name)
        self.body.setPlainText(text)
        self.name.blockSignals(False)
        self.body.blockSignals(False)
        self._refresh()

    def values(self) -> tuple[str, str]:
        return self.name.text(), self.body.toPlainText()

    def _insert(self, token: str) -> None:
        try:
            self.body.insertPlainText(token)
            self.body.setFocus()
        except Exception:  # noqa: BLE001 - シグナル処理から例外を漏らさない
            pass

    def _changed(self, *_a: Any) -> None:
        try:
            self._timer.start()
            self.edited.emit()
        except Exception:  # noqa: BLE001
            pass

    def _refresh(self) -> None:
        try:
            text = self.body.toPlainText()
            if not text:
                self.preview_label.setText("(本文が空です)")
                self.preview_label.setStyleSheet(f"color: {T.TEXT_MUTE}; font-size: 13px;")
                self.warn_label.setVisible(False)
                return
            exp = self._preview(text)
            shown = exp.text if len(exp.text) <= 1200 else exp.text[:1200] + "…"
            self.preview_label.setText(shown)
            self.preview_label.setStyleSheet(f"color: {T.TEXT}; font-size: 13px;")
            self.warn_label.setText("\n".join("⚠ " + w for w in exp.warnings))
            self.warn_label.setVisible(bool(exp.warnings))
        except Exception:  # noqa: BLE001 - 仮想メソッド・シグナル経路から例外を漏らさない
            self.warn_label.setText("⚠ プレビューを作れませんでした")
            self.warn_label.setVisible(True)


def edit_snippet_dialog(parent: QWidget | None, title: str, name: str, text: str, preview: PreviewFn,
                        accent: str) -> tuple[str, str] | None:
    """定型文の編集ダイアログ。保存なら (名前, 本文)、取消なら None。"""
    dlg = W.StyledDialog(parent, title, G.EDIT, accent, width=560)
    ed = SnippetEditor(preview, accent, compact=True)
    ed.set_values(name, text)
    dlg.body.addWidget(ed)
    save = W.button("保存", "primary", G.SAVE, on_click=dlg.accept)
    dlg.buttons.addWidget(W.button("キャンセル", "ghost", on_click=dlg.reject))
    dlg.buttons.addWidget(save)
    ed.name.setFocus()
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    return ed.values()
