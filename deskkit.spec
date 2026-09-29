# -*- mode: python ; coding: utf-8 -*-
# DeskKit を単体 exe(onefile・windowed)にする PyInstaller 設定。build.ps1 から使う。
# マニフェストは既定の asInvoker(管理者昇格を要求しない。INV-3)。
# v0.3: 追加ライブラリ(pi-heif・winrt・psutil・numpy)の隠れた import と、ffmpeg の同梱物・ライセンス表示を足す(H-7)。
# v0.4: PagePress の pypdf と pypdfium2(pypdfium2_raw の pdfium.dll と version.json、設定の pypdfium2_cfg)を足す(H4-11)。
import glob
import importlib.util

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

# 3モジュールは追加ライブラリを関数の中で import する(NFR-5)。静的解析の取りこぼしが無いよう名前で挙げる
WINRT_NAMESPACES = [
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.globalization",
    "winrt.windows.graphics.imaging",
    "winrt.windows.media.ocr",
    "winrt.windows.networking",
    "winrt.windows.networking.connectivity",
    "winrt.windows.storage.streams",
]
# winrt.windows.media などの途中の階層は __init__.py の無い名前空間パッケージで、collect_submodules では辿れない
PDF_PACKAGES = ["pypdf", "pypdfium2", "pypdfium2_raw", "pypdfium2_cfg"]
missing = [m for m in WINRT_NAMESPACES + ["pi_heif", "psutil", "numpy", "PIL"] + PDF_PACKAGES if importlib.util.find_spec(m) is None]
if missing:
    raise SystemExit(f"venv に無いモジュールがあります: {missing}(pip install -e . などで入れてください)")

hidden = (
    collect_submodules("deskkit")
    + collect_submodules("overlaykit")
    + collect_submodules("pi_heif")
    + collect_submodules("winrt")  # winrt.runtime・winrt.system と各名前空間の拡張モジュール(_winrt_*.pyd)
    + WINRT_NAMESPACES
    + collect_submodules("psutil", filter=lambda n: ".tests" not in n)
    + ["numpy", "PIL.Image", "PIL.ImageOps", "PIL.ExifTags"]
    # PagePress は関数の中で import する(NFR-5)。pypdfium2_cli(コマンドライン)は使わないので入れない
    + collect_submodules("pypdf")
    + collect_submodules("pypdfium2", filter=lambda n: n != "pypdfium2.__main__")
    + collect_submodules("pypdfium2_raw")
    + collect_submodules("pypdfium2_cfg")
)
# pdfium.dll(pypdfium2_raw の中)と version.json。hooks-contrib のフックでも入るが、無い環境でも入るように名前で挙げる
pdf_binaries = collect_dynamic_libs("pypdfium2_raw")
if not any(src.lower().endswith("pdfium.dll") for src, _dst in pdf_binaries):
    raise SystemExit("pypdfium2_raw に pdfium.dll がありません(pypdfium2 の Windows 用 wheel を入れてください)")
pdf_datas = [(src, dst) for src, dst in collect_data_files("pypdfium2_raw") + collect_data_files("pypdfium2")
             if not src.lower().endswith(".dll")]  # DLL は binaries の方で入れる(同じ物を二重に入れない)

# ffmpeg の同梱物(tools/make_ffmpeg_bundle.py が作る。build.ps1 が無ければ止まる)とライセンス表示(H-5)
bundled = sorted(p for p in glob.glob("deskkit/_bundled/*") if not p.endswith(".part"))
if not {"deskkit/_bundled/ffmpeg.zip", "deskkit/_bundled/ffmpeg.sha256"} <= {p.replace("\\", "/") for p in bundled}:
    raise SystemExit("deskkit/_bundled/ffmpeg.zip と ffmpeg.sha256 がありません(build.ps1 から実行してください)")
datas = [(p, "deskkit/_bundled") for p in bundled] + [("THIRD_PARTY_LICENSES.txt", ".")] + pdf_datas

a = Analysis(
    ["run_deskkit.py"],
    pathex=["."],
    binaries=pdf_binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc", "pypdfium2_cli", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtPdf",
              "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtOpenGL", "PySide6.QtDBus", "PySide6.QtDesigner",
              "psutil.tests", "PIL.ImageQt", "PIL.ImageTk"],
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
