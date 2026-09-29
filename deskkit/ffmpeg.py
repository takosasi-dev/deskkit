# 同梱の ffmpeg の展開・照合(v0.3 共通 V-5、v0.4 で SendPrep から本体へ移した。SendPrep と ClipTrim が同じ物を使う)。
# 展開先は %LOCALAPPDATA%\DeskKit\ffmpeg\<sha 先頭16桁>\ffmpeg.exe。同じ展開先を使う管理役どうしは、プロセスの中で1つの鍵を共有する。
# PATH 上の ffmpeg は使わない。ログにはファイル名を書かない(理由コードだけ)。
from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import sys
import threading
import time
import zipfile
from collections.abc import Callable
from pathlib import Path

BUNDLE_ZIP = "ffmpeg.zip"
BUNDLE_SHA = "ffmpeg.sha256"
EXE_NAME = "ffmpeg.exe"
CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000

STATE_MISSING = "missing"
STATE_NOT_EXTRACTED = "not_extracted"
STATE_EXTRACTED = "extracted"
STATE_VERIFY_FAILED = "verify_failed"

# FfmpegError.code
CODE_MISSING = "ffmpeg_missing"
CODE_BROKEN = "ffmpeg_broken"
CODE_DISK_FULL = "disk_full"

_ROOT_LOCKS: dict[str, threading.Lock] = {}
_ROOT_LOCKS_GUARD = threading.Lock()
_SHARED_GUARD = threading.Lock()
_shared: FfmpegManager | None = None


class FfmpegError(Exception):
    """ffmpeg を用意できなかった。code は CODE_* のどれか(画面の文はモジュールが決める)。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def bundled_dir() -> Path | None:
    """同梱の ffmpeg.zip と ffmpeg.sha256 がある場所(exe の中 → ソースの順)。"""
    cands: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(Path(meipass) / "deskkit" / "_bundled")
    cands.append(Path(__file__).parent / "_bundled")
    for c in cands:
        if (c / BUNDLE_ZIP).is_file() and (c / BUNDLE_SHA).is_file():
            return c
    return None


def shared_root() -> Path:
    """本番の展開先(%LOCALAPPDATA%\\DeskKit\\ffmpeg。DESKKIT_HOME があればその下)。"""
    from deskkit import paths

    return paths.local_dir() / "ffmpeg"


def shared(log: logging.Logger | None = None) -> FfmpegManager:
    """プロセスで1つの管理役(本番の展開先)。どのモジュールから呼んでも同じ物が返る。"""
    global _shared
    with _SHARED_GUARD:
        if _shared is None:
            _shared = FfmpegManager(shared_root(), log=log or logging.getLogger("deskkit.ffmpeg"))
        return _shared


def sha256_file(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def arg_path(p: Path) -> str:
    """ffmpeg に渡すパス。常に file: を付ける(- で始まる名前をオプションと取り違えない)。"""
    return "file:" + str(p)


def is_disk_full(e: OSError) -> bool:
    return e.errno == 28 or getattr(e, "winerror", None) in (39, 112)


def _root_lock(root: Path) -> threading.Lock:
    key = os.path.normcase(os.path.abspath(root))
    with _ROOT_LOCKS_GUARD:
        return _ROOT_LOCKS.setdefault(key, threading.Lock())


def cleanup_legacy(old_root: Path) -> None:
    """v0.3 の SendPrep の展開先(<sendprep の data_dir>\\ffmpeg)に残った DeskKit 自身の展開物を消す。"""
    _remove_extracted(old_root, keep=None)
    try:
        old_root.rmdir()
    except OSError:
        pass


def _remove_extracted(root: Path, keep: Path | None) -> None:
    """展開物(自分のキャッシュ)を消す。名前が 16 桁の16進のフォルダの、既知の名前のファイルだけ。"""
    try:
        for d in root.iterdir():
            if d == keep or not d.is_dir() or not re.fullmatch(r"[0-9a-f]{16}", d.name):
                continue
            for f in d.iterdir():
                if f.is_file() and f.name in (EXE_NAME, EXE_NAME + ".part", "LICENSE.txt"):
                    f.unlink(missing_ok=True)
            d.rmdir()
    except OSError:
        pass


class FfmpegManager:
    """初めて使うときに zip から展開し、起動後の初回の使用時に SHA-256 を照合する。"""

    def __init__(self, root: Path, bundle: Callable[[], Path | None] = bundled_dir,
                 log: logging.Logger | None = None) -> None:
        self.root = root
        self._bundle = bundle
        self.log = log or logging.getLogger("deskkit.ffmpeg")
        self._lock = _root_lock(root)
        self._exe: Path | None = None
        self._failed = False
        self.h264: bool | None = None  # None = まだ確かめていない

    # ---- 状態(診断・画面用。重い処理はしない)
    def state(self) -> str:
        if self._exe is not None:
            return STATE_EXTRACTED
        if self._failed:
            return STATE_VERIFY_FAILED
        bd = self._bundle()
        if bd is None:
            return STATE_MISSING
        sha = self._read_sha(bd)
        if sha and (self.root / sha[:16] / EXE_NAME).is_file():
            return STATE_EXTRACTED
        return STATE_NOT_EXTRACTED

    def h264_state(self) -> str:
        return "untested" if self.h264 is None else ("available" if self.h264 else "unavailable")

    @staticmethod
    def _read_sha(bd: Path) -> str | None:
        try:
            s = (bd / BUNDLE_SHA).read_text(encoding="ascii").split()[0].strip().lower()
        except (OSError, IndexError, UnicodeDecodeError):
            return None
        return s if re.fullmatch(r"[0-9a-f]{64}", s) else None

    # ---- 展開と照合
    def ensure(self, on_preparing: Callable[[], None] | None = None) -> Path:
        """ffmpeg.exe のパスを返す。用意できなければ FfmpegError。展開するときだけ先に on_preparing を呼ぶ。"""
        with self._lock:
            if self._exe is not None:
                return self._exe
            bd = self._bundle()
            if bd is None:
                self.log.warning("ffmpeg bundle missing")
                raise FfmpegError(CODE_MISSING)
            sha = self._read_sha(bd)
            if sha is None:
                self._failed = True
                self.log.warning("ffmpeg sha file broken")
                raise FfmpegError(CODE_BROKEN)
            exe = self.root / sha[:16] / EXE_NAME
            if exe.is_file():
                try:
                    ok = sha256_file(exe) == sha
                except OSError:
                    ok = False
                if ok:
                    self._exe = exe
                    self._failed = False
                    self.log.info("ffmpeg verified")
                    return exe
                self.log.warning("ffmpeg verify failed; extracting again")
            if on_preparing is not None:
                on_preparing()
            t0 = time.monotonic()
            try:
                got = self._extract(bd / BUNDLE_ZIP, exe)
            except OSError as e:
                self._failed = True
                self.log.warning("ffmpeg extract failed: %s", type(e).__name__)
                raise FfmpegError(CODE_DISK_FULL if is_disk_full(e) else CODE_BROKEN) from None
            except (zipfile.BadZipFile, KeyError, EOFError, RuntimeError, NotImplementedError) as e:
                self._failed = True
                self.log.warning("ffmpeg extract failed: %s", type(e).__name__)
                raise FfmpegError(CODE_BROKEN) from None
            if got != sha:
                self._failed = True
                exe.unlink(missing_ok=True)  # 自分が展開した照合失敗の exe(自分のキャッシュ)
                self.log.warning("ffmpeg verify failed after extract")
                raise FfmpegError(CODE_BROKEN)
            self._exe = exe
            self._failed = False
            self.log.info("ffmpeg extracted ms=%d", int((time.monotonic() - t0) * 1000))
            _remove_extracted(self.root, keep=exe.parent)
            return exe

    def _extract(self, zpath: Path, exe: Path) -> str:
        exe.parent.mkdir(parents=True, exist_ok=True)
        tmp = exe.with_name(EXE_NAME + ".part")
        h = hashlib.sha256()
        with zipfile.ZipFile(zpath) as zf:
            names = [n for n in zf.namelist() if n.replace("\\", "/").rsplit("/", 1)[-1].lower() == EXE_NAME]
            if not names:
                raise KeyError(EXE_NAME)
            try:
                with zf.open(names[0]) as src, open(tmp, "wb") as dst:
                    while True:
                        b = src.read(4 * 1024 * 1024)
                        if not b:
                            break
                        h.update(b)
                        dst.write(b)
                lic = [n for n in zf.namelist() if n.replace("\\", "/").rsplit("/", 1)[-1].lower() == "license.txt"]
                if lic:
                    (exe.parent / "LICENSE.txt").write_bytes(zf.read(lic[0]))
            except BaseException:
                tmp.unlink(missing_ok=True)  # 書きかけの展開物(自分のキャッシュ)
                raise
        os.replace(tmp, exe)
        return h.hexdigest()

    # ---- H.264 エンコーダー(h264_mf)が使えるか。パイプから数フレームだけ流して確かめる
    def check_h264(self, exe: Path) -> bool:
        if self.h264 is not None:
            return self.h264
        w, h, n = 64, 64, 5
        frames = bytes([128]) * (w * h * 3 // 2 * n)
        args = [str(exe), "-hide_banner", "-nostdin", "-loglevel", "error", "-protocol_whitelist", "file,pipe",
                "-f", "rawvideo", "-pix_fmt", "nv12", "-s", f"{w}x{h}", "-r", "10", "-i", "pipe:0",
                "-frames:v", str(n), "-c:v", "h264_mf", "-b:v", "200k", "-f", "null", "pipe:1"]
        try:
            r = subprocess.run(args, input=frames, capture_output=True, timeout=30,
                               creationflags=CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS)  # ClipTrim T-12 と揃える
            ok = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        self.h264 = ok
        self.log.info("h264_mf available=%s", ok)
        return ok
