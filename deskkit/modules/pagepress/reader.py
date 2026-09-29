# 入力の確認(FR-1・FR-2・P-3・P-6)。PDF は `open(p, "rb")` のファイルを pypdf に渡して読むだけ(INV-1)。
# 暗号化(保護だけの物も)・ページ数 0・壊れ・画像が読めない・大きすぎる画像を断り、入力欄の名前・XFA・署名の有無を覚える。
# 入力欄の名前はメモリの中だけで使い、ログに書かない(INV-5)。pypdf のログは件数だけを数える(propagate=False)。
from __future__ import annotations

import logging
import os
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PDF_EXTS = frozenset({".pdf"})
JPEG_EXTS = frozenset({".jpg", ".jpeg", ".jpe", ".jfif"})
IMAGE_EXTS = JPEG_EXTS | {".png", ".heic", ".heif"}
SUPPORTED_EXTS = PDF_EXTS | IMAGE_EXTS

MSG_ENCRYPTED = "パスワードや保護(印刷・編集の制限)が付いた PDF は扱えません"
MSG_NO_PAGES = "ページがありません"
MSG_UNREADABLE_PDF = "PDF を読めませんでした(壊れている可能性があります)"
MSG_BAD_IMAGE = "画像を読めませんでした"
MSG_TOO_BIG_IMAGE = "大きすぎる画像です"
MSG_UNREADABLE = "ファイルを読めませんでした"


# ------------------------------------------------------------------ pypdf のログ(INV-5)
class _CountHandler(logging.Handler):
    """pypdf の警告の文を捨てて、件数だけを数える。"""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.count = 0
        self._mu = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        with self._mu:
            self.count += 1

    def handle(self, record: logging.LogRecord) -> bool:  # 書式化も filter も通さない(文を作らない)
        if record.levelno >= self.level:
            self.emit(record)
        return True


_counter = _CountHandler()
_setup_mu = threading.Lock()


def quiet_pypdf_logging() -> None:
    """ロガー pypdf を DeskKit のログへ流さず、件数を数えるだけにする。何度呼んでもよい(import はしない)。"""
    with _setup_mu:
        lg = logging.getLogger("pypdf")
        lg.propagate = False
        if _counter not in lg.handlers:
            lg.addHandler(_counter)
        if lg.level == logging.NOTSET or lg.level < logging.WARNING:
            lg.setLevel(logging.WARNING)


def pypdf_warning_count() -> int:
    return _counter.count


def pypdf_version() -> str:
    try:
        from importlib.metadata import version

        return version("pypdf")
    except Exception:  # noqa: BLE001 - 版が分からなくても診断は続ける
        return "unknown"


# ------------------------------------------------------------------ 入力
@dataclass(eq=False)
class Entry:
    """一覧の1行。path・名前は画面にだけ出す(V-8)。"""

    id: int
    path: Path
    kind: str                     # "pdf" | "image"
    pages: int
    size: int
    mtime_ns: int
    field_names: frozenset[str] = field(default_factory=frozenset)
    has_xfa: bool = False
    has_signed_sig: bool = False

    @property
    def name(self) -> str:
        return self.path.name


@dataclass(frozen=True)
class Rejected:
    path: Path
    code: str       # encrypted / no_pages / unreadable / bad_image / too_big_image / too_many / too_large
    message: str


class ProbeError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def kind_of(p: Path) -> str | None:
    ext = p.suffix.lower()
    if ext in PDF_EXTS:
        return "pdf"
    if ext in IMAGE_EXTS:
        return "image"
    return None


_NUM = re.compile(r"(\d+)")


def natural_key(name: str) -> list[Any]:
    """名前の自然な順(`2` が `10` より先)。"""
    parts = _NUM.split(name.casefold())
    return [(0, int(p), p) if p.isdigit() else (1, 0, p) for p in parts]


def expand(paths: Sequence[Any]) -> tuple[list[Path], int]:
    """(対応する形式のファイル, 対応していない形式の数)。フォルダは直下のファイルだけを自然な順で見る(FR-1)。"""
    files: list[Path] = []
    unsupported = 0
    for raw in paths:
        p = Path(str(raw))
        try:
            if p.is_dir():
                children: list[Path] = []
                for c in p.iterdir():
                    try:
                        if c.is_file():
                            children.append(c)
                    except OSError:
                        continue
                for c in sorted(children, key=lambda x: natural_key(x.name)):
                    if kind_of(c) is None:
                        unsupported += 1
                    else:
                        files.append(c)
            elif p.is_file():
                if kind_of(p) is None:
                    unsupported += 1
                else:
                    files.append(p)
        except OSError:
            continue
    return files, unsupported


def _field_info(reader: Any) -> tuple[frozenset[str], bool, bool]:
    """(入力欄の完全な名前, XFA を持つか, 値の入った署名欄があるか)。"""
    root = reader.trailer["/Root"]
    af = root.get("/AcroForm")
    if af is None:
        return frozenset(), False, False
    af = af.get_object()
    has_xfa = "/XFA" in af
    names: set[str] = set()
    signed = False
    try:
        fields = reader.get_fields() or {}
    except Exception:  # noqa: BLE001 - 入力欄の木が壊れていても読み込みは続ける
        fields = {}
    for name, f in fields.items():
        names.add(str(name))
        try:
            if f.get("/FT") == "/Sig" and f.get("/V") is not None:
                signed = True
        except Exception:  # noqa: BLE001
            continue
    return frozenset(names), has_xfa, signed


def open_pdf(f: Any) -> Any:
    """開いたファイルから PdfReader(strict=False)。暗号化・0 ページ・壊れは ProbeError。"""
    quiet_pypdf_logging()
    from pypdf import PdfReader

    try:
        r = PdfReader(f, strict=False)
        if r.is_encrypted:
            raise ProbeError("encrypted", MSG_ENCRYPTED)
        n = len(r.pages)
    except ProbeError:
        raise
    except Exception:  # noqa: BLE001 - pypdf の例外は種類が多い。どれも「読めない」
        raise ProbeError("unreadable", MSG_UNREADABLE_PDF) from None
    if n == 0:
        raise ProbeError("no_pages", MSG_NO_PAGES)
    return r


def probe_pdf(path: Path) -> tuple[int, frozenset[str], bool, bool]:
    try:
        with open(path, "rb") as f:
            r = open_pdf(f)
            n = len(r.pages)
            try:
                names, xfa, signed = _field_info(r)
            except Exception:  # noqa: BLE001
                names, xfa, signed = frozenset(), False, False
            return n, names, xfa, signed
    except OSError:
        raise ProbeError("unreadable", MSG_UNREADABLE) from None


_heif_mu = threading.Lock()
_heif_registered = False


def register_heif() -> None:
    global _heif_registered
    with _heif_mu:
        if not _heif_registered:
            import pi_heif

            pi_heif.register_heif_opener()
            _heif_registered = True


def open_image(path: Path) -> Any:
    """Pillow の画像(まだ load していない)。HEIC は pi-heif。読めなければ ProbeError。"""
    from PIL import Image

    if path.suffix.lower() in (".heic", ".heif"):
        register_heif()
    try:
        im = Image.open(path)
    except Image.DecompressionBombError:
        raise ProbeError("too_big_image", MSG_TOO_BIG_IMAGE) from None
    except OSError:
        if not path.is_file():
            raise ProbeError("unreadable", MSG_UNREADABLE) from None
        raise ProbeError("bad_image", MSG_BAD_IMAGE) from None
    except (ValueError, SyntaxError, EOFError, IndexError, TypeError, KeyError, MemoryError):
        raise ProbeError("bad_image", MSG_BAD_IMAGE) from None
    fmt = (im.format or "").upper()
    if fmt not in ("JPEG", "PNG", "HEIF", "HEIC", "AVIF"):
        im.close()
        raise ProbeError("bad_image", MSG_BAD_IMAGE)
    return im


def probe_image(path: Path) -> None:
    from PIL import Image

    im = open_image(path)
    try:
        if (im.format or "").upper() == "JPEG":
            im.draft("RGB", (64, 64))  # 縮小デコードで壊れていないかだけを見る
        im.load()
    except Image.DecompressionBombError:
        raise ProbeError("too_big_image", MSG_TOO_BIG_IMAGE) from None
    except (OSError, ValueError, SyntaxError, EOFError, IndexError, TypeError, KeyError, MemoryError):
        raise ProbeError("bad_image", MSG_BAD_IMAGE) from None
    finally:
        im.close()


def probe(entry_id: int, path: Path) -> Entry:
    """1件を確かめて Entry にする。断る物は ProbeError。"""
    kind = kind_of(path)
    try:
        st = path.stat()
    except OSError:
        raise ProbeError("unreadable", MSG_UNREADABLE) from None
    abspath = Path(os.path.abspath(path))
    if kind == "pdf":
        n, names, xfa, signed = probe_pdf(abspath)
        return Entry(entry_id, abspath, "pdf", n, st.st_size, st.st_mtime_ns, names, xfa, signed)
    if kind == "image":
        probe_image(abspath)
        return Entry(entry_id, abspath, "image", 1, st.st_size, st.st_mtime_ns)
    raise ProbeError("unsupported", MSG_BAD_IMAGE)
