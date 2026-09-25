# 開発用: venv の各ライブラリの dist-info にあるライセンスファイルと、ffmpeg の配布物の LICENSE.txt を読んで、
# リポジトリ直下の THIRD_PARTY_LICENSES.txt(exe に同梱し、設定画面の「ライセンス」で表示する)を作り直す。
# ライブラリの版を上げたら、このスクリプトの表(版・入手先)を直してから実行する。
from __future__ import annotations

import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "THIRD_PARTY_LICENSES.txt"
FFMPEG_ZIP = ROOT / "third_party" / "ffmpeg" / "ffmpeg-n8.1-latest-win64-lgpl-8.1.zip"
FFMPEG_ZIP_SHA256 = "f2d3aa0b81f580454ed771160d8b71b23c9376ba65999be3acab435ac1bfdc9c"
FFMPEG_VERSION = "n8.1.3-20260924"  # `ffmpeg -version` の1行目。make_ffmpeg_bundle.py がこの文字列の有無を確かめる
RULE = "=" * 72
SUB = "-" * 72

MIT_BODY = """Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE."""


@dataclass
class Component:
    name: str
    version: str
    license: str
    url: str
    notes: list[str] = field(default_factory=list)
    texts: list[tuple[str, str]] = field(default_factory=list)  # (見出し, 全文)


def site_packages() -> Path:
    import PySide6

    return Path(PySide6.__file__).resolve().parents[1]


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").rstrip() + "\n"


def dist_licenses(sp: Path, dist: str) -> list[tuple[str, str]]:
    d = sp / f"{dist}.dist-info"
    if not d.is_dir():
        raise SystemExit(f"{d} がありません(venv の版と表の版が違います)")
    files = sorted(p for p in d.rglob("*") if p.is_file() and (p.parent.name == "licenses" or "licenses" in p.parts
                                                              or p.name.upper().startswith(("LICENSE", "COPYING"))))
    return [(str(p.relative_to(d)).replace("\\", "/"), read(p)) for p in files]


def gpl3_text(numpy_license: str) -> str:
    i = numpy_license.index("                    GNU GENERAL PUBLIC LICENSE\n                       Version 3")
    return numpy_license[i:]


def components(sp: Path) -> tuple[list[Component], str, str]:
    with zipfile.ZipFile(FFMPEG_ZIP) as z:
        name = next(n for n in z.namelist() if n.endswith("/LICENSE.txt") and n.count("/") == 1)
        lgpl3 = z.read(name).decode("utf-8").replace("\r\n", "\n").rstrip() + "\n"
    numpy_lic = dist_licenses(sp, "numpy-2.5.3")
    gpl3 = gpl3_text(dict(numpy_lic)["licenses/LICENSE.txt"])
    py_base = Path(sys.base_prefix)
    comps = [
        Component(
            "Qt 6 / Qt for Python (PySide6・Shiboken6)", "Qt 6.11.2 / PySide6-Essentials 6.11.2 / shiboken6 6.11.2",
            "GNU Lesser General Public License v3.0(LGPL-3.0-only を選択)", "https://www.qt.io/ / https://doc.qt.io/qtforpython-6/",
            [
                "DeskKit は Qt と PySide6 を LGPL v3 の条件で利用しています(DeskKit は Qt を改変していません)。",
                "LGPL v3 の全文は付録 A、LGPL v3 が参照する GNU GPL v3 の全文は付録 B にあります。",
                "ソースの入手先:",
                "  Qt 6.11.2          https://download.qt.io/official_releases/qt/6.11/6.11.2/",
                "  PySide6 6.11.2     https://download.qt.io/official_releases/QtForPython/pyside6/",
                "                     https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v6.11.2",
                "Qt に含まれる第三者の部品とそのライセンス: https://doc.qt.io/qt-6/licenses-used-in-qt.html",
                "差し替えについて: DeskKit のソースは https://github.com/takosasi-dev/deskkit で公開しており、",
                "build.ps1 で、利用者が用意した別の版・改変した版の Qt / PySide6 を使って DeskKit.exe を作り直せます。",
            ],
        ),
        Component(
            "FFmpeg(ffmpeg.exe。SendPrep の動画の処理に使用)", FFMPEG_VERSION,
            "GNU Lesser General Public License v3.0 or later(--enable-version3 の LGPL ビルド)", "https://ffmpeg.org/",
            [
                "同梱物: ffmpeg.exe(静的リンクの単体 exe)。DeskKit は ffmpeg を改変せず、別のプロセスとして起動するだけです。",
                "ビルド: BtbN/FFmpeg-Builds の ffmpeg-n8.1-latest-win64-lgpl-8.1.zip(2026-09-24 版)",
                f"  配布物の SHA-256: {FFMPEG_ZIP_SHA256}",
                "  配布元: https://github.com/BtbN/FFmpeg-Builds/releases",
                "GPL の部品(x264・x265 など)を含まない LGPL 版です(configure で --disable-libx264 --disable-libx265)。",
                "ソースの入手先(対応するソース):",
                "  FFmpeg n8.1.3  https://ffmpeg.org/releases/ffmpeg-8.1.3.tar.xz",
                "                 https://git.ffmpeg.org/ffmpeg.git(タグ n8.1.3)",
                "                 https://github.com/FFmpeg/FFmpeg/tree/n8.1.3",
                "  ビルドの手順と、静的にリンクされた各ライブラリ(dav1d・libvpx・libopus・libwebp・OpenH264・zimg など)の",
                "  取得元と版: https://github.com/BtbN/FFmpeg-Builds(scripts.d フォルダ)",
                "上の URL から入手できなくなった場合は、https://github.com/takosasi-dev/deskkit の Issues で依頼して",
                "ください。同じ版のソースを提供します(DeskKit の配布から少なくとも 3 年間)。",
                "差し替えについて: 初めて動画を扱うときに %LOCALAPPDATA%\\DeskKit\\sendprep\\ffmpeg\\ へ展開します。",
                "DeskKit は起動のたびに同梱物との一致(SHA-256)を確かめるため、別の ffmpeg を使うには DeskKit.exe を",
                "作り直してください(build.ps1 が third_party\\ffmpeg\\ の配布物から同梱物を作ります)。",
                "LGPL v3 の全文は付録 A(ffmpeg の配布物の LICENSE.txt そのもの)、GNU GPL v3 の全文は付録 B にあります。",
            ],
        ),
        Component(
            "Pillow", "12.3.0", "MIT-CMU(HPND)。同梱のライブラリはそれぞれのライセンス", "https://python-pillow.org/",
            ["Pillow の wheel に含まれるライブラリ(brotli・FreeType・HarfBuzz・lcms2・libavif・libjpeg-turbo・libpng・",
             "libtiff・libwebp・OpenJPEG・zlib など)のライセンスも、下の LICENSE に含まれています。",
             "ソース: https://github.com/python-pillow/Pillow/tree/12.3.0"],
            dist_licenses(sp, "pillow-12.3.0"),
        ),
        Component(
            "pi-heif(HEIC の読み込み)", "1.4.0(libheif 1.23.0 / libde265 1.1.1)",
            "pi-heif 本体: BSD-3-Clause。同梱の libheif・libde265: LGPL v3", "https://github.com/bigcat88/pillow_heif",
            [
                "pi-heif は読み込み専用で、GPL のエンコーダー(x265)を含みません。",
                "同梱の DLL と版(実物の libheif_info() で確認): libheif 1.23.0、libde265 1.1.1",
                "  libheif  LGPL v3  ソース: https://github.com/strukturag/libheif/tree/v1.23.0",
                "  libde265 LGPL v3  ソース: https://github.com/strukturag/libde265/releases(1.1.1)",
                "  libgcc_s_seh・libstdc++(MinGW の GCC 実行時ライブラリ): GPL v3 + GCC Runtime Library Exception 3.1",
                "    (例外の全文は numpy の節、GPL v3 の全文は付録 B)ソース: https://gcc.gnu.org/",
                "  libwinpthread(mingw-w64): MIT License ほか。",
                "    全文: https://github.com/mingw-w64/mingw-w64/blob/master/mingw-w64-libraries/winpthreads/COPYING",
                "注: 下の LICENSES_bundled.txt は wheel に入っていたものそのままで、記載の版(libheif 1.18.1 等)は古いままです。",
                "LGPL v3 の全文は付録 A にあります。",
                "ソース: https://github.com/bigcat88/pillow_heif(pi-heif は同じリポジトリから作られます)",
            ],
            dist_licenses(sp, "pi_heif-1.4.0"),
        ),
        Component(
            "NumPy", "2.5.3", "BSD-3-Clause ほか(同梱の OpenBLAS・LAPACK・GCC 実行時ライブラリを含む)", "https://numpy.org/",
            ["下の LICENSE.txt に、同梱の OpenBLAS(BSD-3-Clause)・LAPACK・GCC 実行時ライブラリ(GPL v3 + GCC Runtime",
             "Library Exception 3.1)の説明と、例外と GPL v3 の全文が含まれています。",
             "ソース: https://github.com/numpy/numpy/tree/v2.5.3"],
            numpy_lic,
        ),
        Component("psutil", "7.2.2", "BSD-3-Clause", "https://github.com/giampaolo/psutil",
                  ["ソース: https://github.com/giampaolo/psutil/tree/v7.2.2"], dist_licenses(sp, "psutil-7.2.2")),
        Component(
            "PyWinRT(winrt-runtime と winrt-Windows.* の各パッケージ)", "3.2.1", "MIT License", "https://github.com/pywinrt/pywinrt",
            ["含まれるパッケージ: winrt-runtime, winrt-Windows.Foundation, winrt-Windows.Foundation.Collections,",
             "winrt-Windows.Globalization, winrt-Windows.Graphics.Imaging, winrt-Windows.Media.Ocr, winrt-Windows.Networking,",
             "winrt-Windows.Networking.Connectivity, winrt-Windows.Storage.Streams(いずれも 3.2.1)",
             "wheel にライセンスファイルが入っていないため、リポジトリの LICENSE の内容を載せます。"],
            [("LICENSE(https://github.com/pywinrt/pywinrt/blob/main/LICENSE)",
              "MIT License\n\nCopyright (c) Microsoft Corporation. All rights reserved.\n"
              "Copyright (c) 2021-2025 David Lechner <david@pybricks.com>\n\n" + MIT_BODY + "\n")],
        ),
        Component("typing_extensions(PyWinRT が使用)", "4.16.0", "Python Software Foundation License 2.0",
                  "https://github.com/python/typing_extensions", [], dist_licenses(sp, "typing_extensions-4.16.0")),
        Component("Python", f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                  "Python Software Foundation License 2.0", "https://www.python.org/",
                  ["DeskKit.exe は Python の実行環境を含みます。"],
                  [("LICENSE.txt", read(py_base / "LICENSE.txt"))]),
        Component("PyInstaller のブートローダー", "6.22.3", "GPL v2 or later + Bootloader Exception(実行時フックは Apache 2.0)",
                  "https://pyinstaller.org/",
                  ["DeskKit.exe の起動部分です。Bootloader Exception により、DeskKit には GPL の条件はかかりません。"],
                  dist_licenses(sp, "pyinstaller-6.22.3")),
    ]
    return comps, lgpl3, gpl3


def render() -> str:
    sp = site_packages()
    comps, lgpl3, gpl3 = components(sp)
    out: list[str] = [
        RULE,
        "DeskKit — 同梱しているサードパーティのソフトウェアとライセンス",
        "Third-party software included in DeskKit",
        RULE,
        "",
        "DeskKit 本体は MIT License です(https://github.com/takosasi-dev/deskkit の LICENSE)。",
        "DeskKit.exe には、次のソフトウェアが含まれています。それぞれの著作権は各作者にあり、",
        "それぞれのライセンスの条件で配布しています。このファイルは tools/make_third_party_licenses.py で作っています。",
        "",
        "一覧(名前 / 版 / ライセンス)",
        SUB,
    ]
    for i, c in enumerate(comps, 1):
        out.append(f"{i:>2}. {c.name}")
        out.append(f"    版: {c.version}")
        out.append(f"    ライセンス: {c.license}")
    out += ["付録 A. GNU Lesser General Public License v3.0 の全文", "付録 B. GNU General Public License v3.0 の全文", ""]
    for i, c in enumerate(comps, 1):
        out += ["", RULE, f"{i}. {c.name}", RULE, f"版: {c.version}", f"ライセンス: {c.license}", f"ホームページ: {c.url}"]
        if c.notes:
            out.append("")
            out += c.notes
        for title, text in c.texts:
            out += ["", SUB, f"[{c.name}] {title}", SUB, text.rstrip("\n")]
    out += ["", RULE, "付録 A. GNU Lesser General Public License v3.0", RULE, lgpl3.rstrip("\n"),
            "", RULE, "付録 B. GNU General Public License v3.0", RULE, gpl3.rstrip("\n"), ""]
    return "\n".join(out)


def main() -> int:
    text = render()
    # 古いメモ帳でも文字化けしないよう BOM を付ける(改行はリポジトリの決まり .gitattributes に合わせて LF)
    OUT.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    print(f"作成: {OUT} ({OUT.stat().st_size:,} バイト)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
