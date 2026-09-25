# 設定画面の「ライセンス」から開く、同梱したサードパーティのライセンスの表示(H-5)。
# 左に一覧(名前・版・ライセンス)、右に全文。一覧を選ぶとその節へ飛び、検索欄で全文を探せる。
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QTextCursor, QTextDocument
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QWidget

from deskkit import licenses
from deskkit.ui import theme as T
from deskkit.ui.theme import G
from deskkit.ui.widgets import StyledDialog, button, label


class LicenseDialog(StyledDialog):
    def __init__(self, parent: QWidget | None) -> None:
        super().__init__(parent, "ライセンス", G.INFO, T.ACCENT, width=800)
        self.body.addWidget(label("DeskKit.exe に含まれているほかのソフトウェアと、そのライセンスです。"
                                  "ffmpeg などのソースの入手先も書いてあります。", "Dim", wrap=True))
        text = licenses.read_text()
        self._entries = licenses.summary(text)

        self.search = QLineEdit()
        self.search.setPlaceholderText("本文を検索(例: ffmpeg、LGPL)")
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(self.find_next)
        find_btn = button("次を検索", "secondary", G.SEARCH, self.find_next)
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(self.search, 1)
        top.addWidget(find_btn)
        self.body.addLayout(top)

        self.list = QListWidget()
        self.list.setFixedWidth(230)
        for e in self._entries:
            it = QListWidgetItem(f"{e.name}\n{e.version}\n{e.license}")
            it.setToolTip(f"{e.name}\n版: {e.version}\nライセンス: {e.license}")
            self.list.addItem(it)
        self.list.setWordWrap(True)
        self.list.currentRowChanged.connect(self._jump)

        self.viewer = QPlainTextEdit()
        self.viewer.setReadOnly(True)
        self.viewer.setPlainText(text)
        f = QFont()
        f.setFamilies(["Cascadia Mono", "Consolas", "BIZ UDGothic", "MS Gothic"])
        f.setPixelSize(12)
        self.viewer.setFont(f)
        self.viewer.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.viewer.setMinimumHeight(240)  # 1366x768 の画面でも収まるように。初期の高さは _fit_screen で広げる
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(self.list)
        row.addWidget(self.viewer, 1)
        self.body.addLayout(row)

        self.status = label("", "Mute")
        self.buttons.insertWidget(0, self.status)
        self.buttons.addWidget(button("メモ帳で開く", "ghost", G.OPEN, self._open_external))
        close = button("閉じる", "primary", on_click=self.accept)
        close.setDefault(True)
        self.buttons.addWidget(close)
        self._fit_screen()

    def _fit_screen(self) -> None:
        """画面の作業領域の 85% までの高さで開く(小さい画面ではみ出さない)。"""
        from PySide6.QtGui import QGuiApplication

        parent = self.parentWidget()
        scr = (parent.screen() if parent is not None else None) or QGuiApplication.primaryScreen()
        hint = self.sizeHint()
        if scr is None:
            self.resize(hint)
            return
        avail = scr.availableGeometry()
        self.resize(min(max(hint.width(), 1000), int(avail.width() * 0.9)), min(760, int(avail.height() * 0.85)))

    def _jump(self, row: int) -> None:
        if not (0 <= row < len(self._entries)) or self._entries[row].anchor < 0:
            return
        c = self.viewer.textCursor()
        c.setPosition(self._entries[row].anchor)
        # 一度いちばん下まで送ってからカーソルを見せると、見出しが表示域の上端に来る
        self.viewer.verticalScrollBar().setValue(self.viewer.verticalScrollBar().maximum())
        self.viewer.setTextCursor(c)
        self.viewer.ensureCursorVisible()

    def find_next(self) -> None:
        q = self.search.text().strip()
        if not q:
            return
        if self.viewer.find(q):
            self.status.setText("")
            return
        c = self.viewer.textCursor()  # 末尾まで来たら先頭から探し直す
        c.movePosition(QTextCursor.MoveOperation.Start)
        self.viewer.setTextCursor(c)
        found = self.viewer.find(q, QTextDocument.FindFlag(0))
        self.status.setText("" if found else "見つかりません")

    def _open_external(self) -> None:
        try:
            p = licenses.export_copy()
            if p is not None:
                os.startfile(str(p))  # 既定のテキストエディタで開く
                return
            self.status.setText("ファイルが見つかりません")
        except OSError:
            self.status.setText("開けませんでした")


def show_licenses(parent: QWidget | None) -> None:
    dlg = LicenseDialog(parent)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dlg.exec()
