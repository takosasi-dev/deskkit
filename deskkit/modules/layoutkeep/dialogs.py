# 「今開いているウィンドウから選ぶ」ダイアログ。見えているトップレベルウィンドウを exe・クラス・タイトルで一覧し、
# チェックしたものを targets の候補として返す。タイトルはこのダイアログの画面にだけ出し、ログには書かない(D-14)。
from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QAbstractItemView, QCheckBox, QDialog, QHeaderView, QLineEdit, QTableWidget, QTableWidgetItem

from deskkit.ui import theme as T
from deskkit.ui import widgets as W
from deskkit.ui.theme import G

from .model import WindowInfo
from .windows import EXCLUDE_LABELS


def _cell(t: QTableWidget, r: int, c: int) -> str:
    it = t.item(r, c)
    return it.text() if it is not None else ""


class WindowPicker(W.StyledDialog):
    def __init__(self, parent: object, wins: Sequence[WindowInfo], existing: set[tuple[str, str]], accent: str) -> None:
        super().__init__(parent, "今開いているウィンドウから選ぶ", G.APP, accent, width=760)  # type: ignore[arg-type]
        self._rows: list[tuple[str, str]] = []
        self.body.addWidget(W.label("復元したいウィンドウにチェックを入れてください。exe 名とクラスが targets に追加されます"
                                    "(タイトルは保存しません。区別が要るときは後で title_regex を書きます)。", "Dim", wrap=True))
        self.search = QLineEdit()
        self.search.setPlaceholderText("exe・クラス・タイトルで絞り込み")
        self.search.textChanged.connect(self._filter)
        self.body.addWidget(self.search)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["", "exe", "クラス", "タイトル(この画面だけに表示)", "状態"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setMinimumHeight(360)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        seen: set[tuple[str, str, str]] = set()
        for w in sorted(wins, key=lambda x: (x.exe_name.lower(), x.cls)):
            key = (w.exe_name.lower(), w.cls, w.title)
            if key in seen:
                continue
            seen.add(key)
            r = self.table.rowCount()
            self.table.insertRow(r)
            cb = QCheckBox()
            already = (w.exe_name.lower(), w.cls) in existing
            cb.setChecked(already)
            cb.setEnabled(not already)
            self.table.setCellWidget(r, 0, cb)
            self.table.setItem(r, 1, QTableWidgetItem(w.exe_name))
            self.table.setItem(r, 2, QTableWidgetItem(w.cls))
            self.table.setItem(r, 3, QTableWidgetItem(w.title or "(タイトルなし)"))
            state = "登録済み" if already else ("" if w.excluded is None else EXCLUDE_LABELS.get(w.excluded, w.excluded))
            it = QTableWidgetItem(state)
            if state:
                it.setForeground(QColor(T.TEXT_MUTE))
            self.table.setItem(r, 4, it)
            self._rows.append((w.exe_name, w.cls))
        self.table.cellClicked.connect(self._toggle)
        self.body.addWidget(self.table)
        self.count = W.label("", "Mute")
        self.buttons.insertWidget(0, self.count)
        self.buttons.addWidget(W.button("キャンセル", "ghost", on_click=self.reject))
        self.ok = W.button("追加", "primary", G.ADD, on_click=self.accept)
        self.buttons.addWidget(self.ok)
        for r in range(self.table.rowCount()):
            w_ = self.table.cellWidget(r, 0)
            if isinstance(w_, QCheckBox):
                w_.toggled.connect(lambda _c=False: self._update_count())
        self._update_count()
        self.table.setStyleSheet(f"QTableWidget {{ background: {T.SURFACE}; }}")

    def _toggle(self, row: int, col: int) -> None:
        try:
            if col == 0:
                return
            w = self.table.cellWidget(row, 0)
            if isinstance(w, QCheckBox) and w.isEnabled():
                w.setChecked(not w.isChecked())
        except Exception:  # noqa: BLE001
            pass

    def _filter(self, text: str) -> None:
        try:
            q = text.strip().lower()
            for r in range(self.table.rowCount()):
                hay = " ".join(_cell(self.table, r, c) for c in (1, 2, 3)).lower()
                self.table.setRowHidden(r, bool(q) and q not in hay)
        except Exception:  # noqa: BLE001
            pass

    def _update_count(self) -> None:
        n = len(self.selected())
        self.count.setText(f"{n} 件を追加" if n else "チェックしてください")
        self.ok.setEnabled(n > 0)

    def selected(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for r, key in enumerate(self._rows):
            w = self.table.cellWidget(r, 0)
            if isinstance(w, QCheckBox) and w.isChecked() and w.isEnabled() and key not in out:
                out.append(key)
        return out


def pick_windows(parent: object, wins: Sequence[WindowInfo], existing: set[tuple[str, str]], accent: str) -> list[tuple[str, str]]:
    dlg = WindowPicker(parent, wins, existing, accent)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return []
    return dlg.selected()
