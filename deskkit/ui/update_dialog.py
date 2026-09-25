# 更新ダイアログ。新しい版の番号・リリースノート・進捗バーと「更新して再起動」「この版をスキップ」を出す。
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QProgressBar, QTextBrowser, QWidget

from deskkit import __version__
from deskkit.ui import theme as T
from deskkit.ui.theme import G
from deskkit.ui.widgets import StatusPill, StyledDialog, button, label

if TYPE_CHECKING:
    from deskkit.update_manager import UpdateManager


class UpdateDialog(StyledDialog):
    def __init__(self, mgr: UpdateManager, parent: QWidget | None) -> None:
        info = mgr.info
        title = f"DeskKit v{info.version}" if info else "アップデート"
        super().__init__(parent, title, G.DOWNLOAD, T.ACCENT, width=560)
        self._mgr = mgr
        self.pill = StatusPill()
        self.body.addWidget(self.pill)
        self.body.addWidget(label(f"今の版: v{__version__}" + (f"   →   新しい版: v{info.version}" if info else ""), "Dim"))
        notes = QTextBrowser()
        notes.setOpenExternalLinks(False)
        notes.setMinimumHeight(220)
        notes.setMarkdown(info.notes if info and info.notes.strip() else "_リリースノートはありません_")
        notes.setStyleSheet(f"QTextBrowser {{ background: {T.SURFACE2}; border-radius: 10px; padding: 8px; }}")
        self.body.addWidget(notes)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.hide()
        self.body.addWidget(self.bar)
        self.hint = label("", "Mute", wrap=True)
        self.body.addWidget(self.hint)
        if info and info.html_url:
            self.buttons.insertWidget(0, button("リリースページ", "ghost", G.LINK,
                                                lambda: QDesktopServices.openUrl(QUrl(info.html_url))))
        self.skip_btn = button("この版をスキップ", "ghost", on_click=self._skip)
        self.later = button("後で", "secondary", on_click=self._later)
        self.go = button("更新して再起動", "primary", G.REFRESH, mgr.install)
        for b in (self.skip_btn, self.later, self.go):
            self.buttons.addWidget(b)
        mgr.changed.connect(self._sync)
        mgr.progress.connect(self._progress)
        self._sync()

    def _later(self) -> None:
        if self._mgr.state == "downloading":
            self._mgr.cancel()
        else:
            self.reject()

    def _skip(self) -> None:
        self._mgr.skip()
        self.reject()

    def _progress(self, got: int, total: int) -> None:
        self.bar.show()
        if total > 0:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(got * 1000 / total))
            self.hint.setText(f"ダウンロード中… {got / 1048576:.1f} / {total / 1048576:.1f} MB")
        else:
            self.bar.setRange(0, 0)

    def _sync(self) -> None:
        try:
            m = self._mgr
            kind = {"available": "accent", "latest": "ok", "error": "error", "downloading": "info",
                    "installing": "info", "checking": "info"}.get(m.state, "off")
            self.pill.set_state(kind, m.status_text())
            busy = m.state in ("downloading", "installing")
            self.go.setEnabled(m.state == "available" and m.can_install)
            self.skip_btn.setEnabled(not busy)
            self.later.setText("キャンセル" if m.state == "downloading" else "後で")
            if not m.can_install:
                self.hint.setText("開発版(python で実行中)では自動で入れ替えできません。リリースページから入手してください。")
            elif m.state == "installing":
                self.hint.setText("検証済みの新しい版に入れ替えて再起動します。前の版は DeskKit.previous.exe として残ります。")
            elif m.state == "error":
                self.hint.setText(m.error or "")
                self.bar.hide()
        except RuntimeError:
            pass
