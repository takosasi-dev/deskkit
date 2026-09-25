# 「実行中のアプリから選ぶ」ダイアログ。プロセス一覧(exe 名と PID のみ)を CreateToolhelp32Snapshot で取る。
# プロセスのメモリやウィンドウには触れない。選んだ exe 名(小文字)の一覧を返す。
from __future__ import annotations

import ctypes
from ctypes import wintypes as w

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLineEdit, QListWidget, QListWidgetItem, QWidget

from deskkit.ui import theme as T
from deskkit.ui.theme import G
from deskkit.ui.widgets import StyledDialog, button, label

TH32CS_SNAPPROCESS = 0x00000002  # tlhelp32.h


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
        ("pcPriClassBase", ctypes.c_long), ("dwFlags", w.DWORD), ("szExeFile", w.WCHAR * 260),
    ]


def running_exes() -> list[str]:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    k32.CreateToolhelp32Snapshot.restype = w.HANDLE
    k32.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32FirstW.restype = w.BOOL
    k32.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.restype = w.BOOL
    k32.CloseHandle.argtypes = [w.HANDLE]
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == w.HANDLE(-1).value:
        return []
    names: set[str] = set()
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            n = e.szExeFile.lower()
            if n and n not in ("[system process]", "system"):
                names.add(n)
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return sorted(names)


def pick_running_exes(parent: QWidget | None, title: str = "実行中のアプリから選ぶ", exclude: set[str] | None = None) -> list[str]:
    dlg = StyledDialog(parent, title, G.APP, T.ACCENT, width=460)
    dlg.body.addWidget(label("チェックを付けたアプリの exe 名を追加します。", "Dim", wrap=True))
    search = QLineEdit()
    search.setPlaceholderText("絞り込み…")
    dlg.body.addWidget(search)
    lst = QListWidget()
    lst.setMinimumHeight(320)
    for n in running_exes():
        if exclude and n in exclude:
            continue
        it = QListWidgetItem(n)
        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        it.setCheckState(Qt.CheckState.Unchecked)
        lst.addItem(it)
    dlg.body.addWidget(lst)

    def filt(text: str) -> None:
        for i in range(lst.count()):
            it = lst.item(i)
            it.setHidden(text.lower() not in it.text())

    search.textChanged.connect(filt)
    lst.itemDoubleClicked.connect(lambda it: it.setCheckState(
        Qt.CheckState.Unchecked if it.checkState() == Qt.CheckState.Checked else Qt.CheckState.Checked))
    dlg.buttons.addWidget(button("キャンセル", "ghost", on_click=dlg.reject))
    dlg.buttons.addWidget(button("追加", "primary", on_click=dlg.accept))
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return []
    return [lst.item(i).text() for i in range(lst.count()) if lst.item(i).checkState() == Qt.CheckState.Checked]
