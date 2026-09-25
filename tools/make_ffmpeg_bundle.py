# 開発・ビルド用: third_party\ffmpeg の BtbN 配布物(LGPL 版)から、exe に同梱する deskkit/_bundled/ffmpeg.zip
# (LZMA、中身は ffmpeg.exe と LICENSE.txt だけ)と ffmpeg.sha256(ffmpeg.exe の SHA-256、16進小文字1行)を作る。
# 同じ中身が既にあれば作り直さない。配布物の SHA-256 は同じフォルダの checksums.sha256 と照合する(ダウンロードはしない)。
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_NAME = "ffmpeg-n8.1-latest-win64-lgpl-8.1.zip"
SOURCE_DIR = ROOT / "third_party" / "ffmpeg"
OUT_DIR = ROOT / "deskkit" / "_bundled"
LICENSES_FILE = ROOT / "THIRD_PARTY_LICENSES.txt"
RELEASES_URL = "https://github.com/BtbN/FFmpeg-Builds/releases"
_CHUNK = 1 << 20


class BundleError(Exception):
    pass


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while b := f.read(_CHUNK):
            h.update(b)
    return h.hexdigest()


def expected_sha256(checksums: Path, name: str) -> str | None:
    """BtbN の checksums.sha256(`<hex>  <名前>` の行の並び)から name の行を探す。"""
    for line in checksums.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            return parts[0].lower()
    return None


def find_members(zf: zipfile.ZipFile) -> tuple[zipfile.ZipInfo, zipfile.ZipInfo]:
    exe = lic = None
    for info in zf.infolist():
        n = info.filename
        if n.endswith("/bin/ffmpeg.exe") and n.count("/") == 2:
            exe = info
        elif n.endswith("/LICENSE.txt") and n.count("/") == 1:
            lic = info
    if exe is None or lic is None:
        raise BundleError("配布物の中に <版>/bin/ffmpeg.exe と <版>/LICENSE.txt が見つかりません")
    return exe, lic


def is_up_to_date(out_zip: Path, out_sha: Path, exe: zipfile.ZipInfo, lic: zipfile.ZipInfo) -> bool:
    """出力が同じ ffmpeg.exe・LICENSE.txt から作ったものか(zip の目次の CRC32 と大きさで比べる。展開しない)。"""
    if not (out_zip.is_file() and out_sha.is_file()):
        return False
    if not re.fullmatch(r"[0-9a-f]{64}", out_sha.read_text(encoding="ascii", errors="replace").strip()):
        return False
    try:
        with zipfile.ZipFile(out_zip) as z:
            got = {i.filename: (i.CRC, i.file_size, i.compress_type) for i in z.infolist()}
    except (OSError, zipfile.BadZipFile):
        return False
    want = {"ffmpeg.exe": (exe.CRC, exe.file_size, zipfile.ZIP_LZMA), "LICENSE.txt": (lic.CRC, lic.file_size, zipfile.ZIP_LZMA)}
    return got == want


def ffmpeg_version(exe_path: Path) -> str:
    """`ffmpeg -version` の1行目から版(例: n8.1.3-20260924)を読む。"""
    r = subprocess.run([str(exe_path), "-hide_banner", "-version"], capture_output=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
    m = re.match(rb"ffmpeg version (\S+)", r.stdout)
    if not m:
        raise BundleError("ffmpeg.exe の版を読めません")
    return m.group(1).decode("ascii", "replace")


def build(source: Path, out_dir: Path, *, verify_checksum: bool = True, licenses_file: Path | None = LICENSES_FILE,
          force: bool = False) -> tuple[bool, str]:
    """(作り直したか, ffmpeg.exe の SHA-256) を返す。"""
    if not source.is_file():
        raise BundleError(f"{source} がありません")
    if verify_checksum:
        checksums = source.with_name("checksums.sha256")
        if not checksums.is_file():
            raise BundleError(f"{checksums} がありません(配布物と同じリリースから入手してください)")
        want = expected_sha256(checksums, source.name)
        if want is None:
            raise BundleError(f"checksums.sha256 に {source.name} の行がありません")
        got = sha256_file(source)
        if got != want:
            raise BundleError(f"SHA-256 が一致しません: {got} (期待値 {want})")
        print(f"配布物の SHA-256 を照合しました: {got}")
    out_zip, out_sha = out_dir / "ffmpeg.zip", out_dir / "ffmpeg.sha256"
    with zipfile.ZipFile(source) as src:
        exe, lic = find_members(src)
        if not force and is_up_to_date(out_zip, out_sha, exe, lic):
            digest = out_sha.read_text(encoding="ascii").strip()
            print(f"同じ版の同梱物があるので作り直しません(ffmpeg.exe sha256 {digest})")
            return False, digest
        with tempfile.TemporaryDirectory(prefix="deskkit-ffmpeg-") as td:
            tmp = Path(td)
            exe_path, lic_path = tmp / "ffmpeg.exe", tmp / "LICENSE.txt"
            for info, dst in ((exe, exe_path), (lic, lic_path)):
                with src.open(info) as r, dst.open("wb") as w:
                    shutil.copyfileobj(r, w, _CHUNK)
            digest = sha256_file(exe_path)
            print(f"ffmpeg.exe sha256 {digest}")
            if licenses_file is not None:  # テストでは None(偽の exe を実行しない)
                version = ffmpeg_version(exe_path)
                print(f"ffmpeg の版 {version}")
                text = licenses_file.read_text(encoding="utf-8") if licenses_file.is_file() else ""
                if version not in text:
                    raise BundleError(f"{licenses_file.name} に ffmpeg の版 {version} が書かれていません。"
                                      "版とソースの入手先を更新してから、もう一度実行してください")
            out_dir.mkdir(parents=True, exist_ok=True)
            part = out_zip.with_suffix(".zip.part")
            print("LZMA で圧縮しています(1〜2 分かかります)...")
            with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_LZMA) as z:
                z.write(exe_path, "ffmpeg.exe")
                z.write(lic_path, "LICENSE.txt")
            with zipfile.ZipFile(part) as z:  # 書いたものを読み直して確かめる
                if sorted(z.namelist()) != ["LICENSE.txt", "ffmpeg.exe"]:
                    raise BundleError("作った zip の中身が想定と違います")
                h = hashlib.sha256()
                with z.open("ffmpeg.exe") as r:
                    while b := r.read(_CHUNK):
                        h.update(b)
                if h.hexdigest() != digest:
                    raise BundleError("作った zip の ffmpeg.exe の SHA-256 が元と一致しません")
            part.replace(out_zip)
            out_sha.write_text(digest + "\n", encoding="ascii", newline="\n")
    print(f"作成: {out_zip} ({out_zip.stat().st_size:,} バイト)")
    return True, digest


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", type=Path, default=SOURCE_DIR / SOURCE_NAME)
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument("--force", action="store_true", help="同じ版でも作り直す")
    a = ap.parse_args(argv)
    try:
        build(a.source, a.out, force=a.force)
    except BundleError as e:
        print(f"エラー: {e}", file=sys.stderr)
        print(f"入手先: {RELEASES_URL} の {SOURCE_NAME} と checksums.sha256 を {SOURCE_DIR} に置いてください",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
