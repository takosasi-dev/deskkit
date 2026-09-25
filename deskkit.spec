# -*- mode: python ; coding: utf-8 -*-
# DeskKit を単体 exe(onefile・windowed)にする PyInstaller 設定。build.ps1 から使う。
# マニフェストは既定の asInvoker(管理者昇格を要求しない。INV-3)。
from PyInstaller.utils.hooks import collect_submodules

hidden = collect_submodules("deskkit") + collect_submodules("overlaykit")

a = Analysis(
    ["run_deskkit.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtPdf",
              "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtOpenGL", "PySide6.QtDBus", "PySide6.QtDesigner"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="DeskKit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon="build/deskkit.ico",
    version="build/version_info.txt",
    uac_admin=False,
)
