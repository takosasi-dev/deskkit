# zip の中の名前: 元のバイト列(M-7)・読み方の候補(M-8)・安全の規則(FR-19・FR-20)・zip 爆弾の上限(M-10)。
# 名前は zipfile の filename を使わず、orig_filename から元のバイト列に戻して読む(P-7〜P-9)。0x7075 は自前で読み、CRC が合うときだけ使う。
# 決めた名前は展開先の中にしか向かない(INV-3)。ログにも ops にも名前は渡さない(INV-5)。
from __future__ import annotations

import os
import re
import stat
import struct
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from deskkit.modules.mojifix import sniff as S
from deskkit.modules.mojifix.compose import compose

MAX_ENTRIES = 50_000
ALLOWED_METHODS = frozenset({0, 8, 12, 14})
SAMPLE_NAMES = 30
MB = 1024 * 1024
GB = 1024 * MB
MAX_PATH = 260
ENTRY_RATIO = 1000
TOTAL_RATIO = 200

# (key, codec, 画面の名前)。並びは M-8 の順
NAME_CANDIDATES: tuple[tuple[str, str, str], ...] = (
    ("utf8", "utf-8", "UTF-8(Mac などの形)"),
    ("sjis", "cp932", "Shift_JIS(日本語の Windows の形)"),
    ("cp437", "cp437", "英語の Windows の形"),
)
NAME_CODEC = {k: c for k, c, _l in NAME_CANDIDATES}

MSG_BROKEN = "zip を読めませんでした(壊れているか、分割された zip です)"
MSG_BAD_NAMES = "名前の記録が壊れているため開けません"
MSG_ENCRYPTED = "パスワード付きの zip は扱えません"
MSG_METHOD = "MojiFix では開けない圧縮の形です"
MSG_EMPTY = "空の zip です"
MSG_TOO_MANY = "中身が多すぎます(5 万件まで)"
MSG_NO_MOJIBAKE = "名前に化けはありません"
MSG_BOMB = "中身が異常に大きく広がる zip です。安全のため展開しません"

_BAD_CHARS = re.compile(r'[<>:"|?*\x01-\x1f]')
_TRAIL = re.compile(r"[ .]+$")
_DRIVE = re.compile(r"^[A-Za-z]:")
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.IGNORECASE)
_SEP = re.compile(r"[/\\]")

# 飛ばす理由(画面の文と、数えるための鍵)
SKIP_LINK = ("link", "リンクの項目")
SKIP_MAC = ("mac", "Mac の管理用ファイル")
SKIP_ABS = ("absolute", "場所を決め打ちした名前")
SKIP_UP = ("parent", "上のフォルダを指す名前")
SKIP_NUL = ("nul", "名前に NUL が入っている")
SKIP_DEVICE = ("device", "Windows で使えない名前")
SKIP_LONG_PART = ("long_part", "名前が長すぎる(255 字超)")
SKIP_EMPTY = ("empty", "名前が空")
SKIP_CLASH = ("clash", "同じ名前のファイルとフォルダがぶつかる")
SKIP_LONG_PATH = ("long_path", "場所の名前が長すぎる(260 字以上)")
SKIP_OUTSIDE = ("outside", "展開先の外を指す名前")
SKIP_EXISTS = ("exists", "同じ名前がすでにある")
SKIP_BROKEN = ("broken", "壊れていた")


class ZipLoadError(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class Entry:
    index: int
    raw: bytes                 # 名前の元のバイト列(M-7)
    fixed: str | None          # 印(ビット 11)のある名前か、CRC の合う 0x7075 の名前。候補によらず固定
    is_dir: bool
    is_link: bool
    file_size: int
    compress_size: int
    date_time: tuple[int, int, int, int, int, int]


@dataclass
class ZipScan:
    path: Path
    size: int
    mtime_ns: int
    entries: list[Entry]

    @property
    def needs_candidates(self) -> bool:
        """FR-18: 固定の名前が無い項目のバイト列に 0x80 以上があるときだけ候補を出す。"""
        return any(e.fixed is None and not e.raw.isascii() for e in self.entries)

    @property
    def declared_total(self) -> int:
        return sum(e.file_size for e in self.entries)

    @property
    def compressed_total(self) -> int:
        return sum(e.compress_size for e in self.entries)


def unicode_path(extra: bytes, raw: bytes) -> str | None:
    """Info-ZIP Unicode Path(0x7075)。版 1 で、CRC32 が元のバイト列と合うときだけ使う(M-7)。"""
    i = 0
    while i + 4 <= len(extra):
        tp, ln = struct.unpack_from("<HH", extra, i)
        data = extra[i + 4:i + 4 + ln]
        if tp == 0x7075 and len(data) >= 5:
            ver, crc = struct.unpack_from("<BI", data, 0)
            if ver == 1 and crc == (zlib.crc32(raw) & 0xFFFFFFFF):
                try:
                    name = data[5:].decode("utf-8")
                except UnicodeDecodeError:
                    return None
                return name or None
        i += 4 + ln
    return None


def raw_name(info: zipfile.ZipInfo) -> bytes:
    """M-7: 印の無い項目は orig_filename.encode("cp437")(256 通りが往復する)、印のある項目は UTF-8。"""
    if info.flag_bits & 0x800:
        return info.orig_filename.encode("utf-8", errors="surrogatepass")
    return info.orig_filename.encode("cp437")


def _is_link(info: zipfile.ZipInfo) -> bool:
    return info.create_system == 3 and stat.S_ISLNK(info.external_attr >> 16)


def entry_of(i: int, info: zipfile.ZipInfo) -> Entry:
    raw = raw_name(info)
    fixed = info.orig_filename if info.flag_bits & 0x800 else unicode_path(info.extra, raw)
    is_dir = raw.endswith((b"/", b"\\")) or (fixed is not None and fixed.endswith(("/", "\\")))
    return Entry(i, raw, fixed, is_dir, _is_link(info), int(info.file_size), int(info.compress_size),
                 tuple(info.date_time))  # type: ignore[arg-type]


def load(path: Path) -> ZipScan:
    """FR-17 の順に確かめて読む。当てはまれば ZipLoadError(reason, 画面の文)。"""
    try:
        st = os.stat(path)
        zf = zipfile.ZipFile(path)
    except UnicodeDecodeError:
        raise ZipLoadError("bad_names", MSG_BAD_NAMES) from None
    except NotImplementedError:
        raise ZipLoadError("unsupported_method", MSG_METHOD) from None
    except (zipfile.BadZipFile, OSError, ValueError, EOFError, struct.error, zlib.error):
        raise ZipLoadError("broken_zip", MSG_BROKEN) from None
    with zf:
        infos = zf.infolist()
    if any(i.flag_bits & 0x41 for i in infos):  # ビット 0(暗号化)・ビット 6(強い暗号化)
        raise ZipLoadError("encrypted", MSG_ENCRYPTED)
    if any(i.compress_type not in ALLOWED_METHODS for i in infos):
        raise ZipLoadError("unsupported_method", MSG_METHOD)
    if not infos:
        raise ZipLoadError("zip_empty", MSG_EMPTY)
    if len(infos) > MAX_ENTRIES:
        raise ZipLoadError("too_many", MSG_TOO_MANY)
    return ZipScan(path, st.st_size, st.st_mtime_ns, [entry_of(i, info) for i, info in enumerate(infos)])


# ------------------------------------------------------------------ 候補(M-8・FR-18)
def decode_name(e: Entry, key: str) -> str:
    if e.fixed is not None:
        return e.fixed
    return e.raw.decode(NAME_CODEC[key], errors="replace")


@dataclass
class NameCandidate:
    key: str
    label: str
    bad_names: int         # 読めない名前の数
    errors: int            # 読めない所(U+FFFD)の数
    marks: int             # 化けの印の数
    sample: list[str] = field(default_factory=list)


def candidates(scan: ZipScan) -> list[NameCandidate]:
    out: list[NameCandidate] = []
    for key, codec, label in NAME_CANDIDATES:
        bad = errors = mk = 0
        sample: list[str] = []
        for e in scan.entries:
            if e.fixed is not None:
                name = e.fixed
            else:
                name, n = S.decode_all(e.raw, codec, True)
                if n:
                    bad += 1
                    errors += n
                mk += S.marks(name)
            if len(sample) < SAMPLE_NAMES:
                sample.append(name)
        out.append(NameCandidate(key, label, bad, errors, mk, sample))
    order = {k: i for i, (k, _c, _l) in enumerate(NAME_CANDIDATES)}
    out.sort(key=lambda c: (c.errors, c.marks, order[c.key]))
    return out


# ------------------------------------------------------------------ 名前の規則(FR-19・FR-20)
@dataclass(frozen=True)
class PlanItem:
    index: int
    original: str              # 選んだ候補で読んだ名前(画面用)
    rel: str | None            # 展開先からの相対パス(\ 区切り)。飛ばすなら None
    is_dir: bool
    status: str                # "extract" | "rename" | "skip"
    reason: str = ""           # 画面の文
    code: str = ""             # 飛ばした理由の鍵(数える用)
    composed: bool = False
    size: int = 0

    def status_text(self) -> str:
        if self.status == "extract":
            return "展開する"
        if self.status == "rename":
            return f"名前を変えて展開する: {self.reason}"
        return f"飛ばす: {self.reason}"


def _skip(e: Entry, name: str, why: tuple[str, str]) -> PlanItem:
    return PlanItem(e.index, name, None, e.is_dir, "skip", why[1], why[0], size=e.file_size)


def _sanitize(part: str) -> str:
    p = _BAD_CHARS.sub("_", part)
    m = _TRAIL.search(p)
    if m:
        p = p[:m.start()] + "_" * (m.end() - m.start())
    return p


def _numbered(part: str, n: int) -> str:
    stem, dot, ext = part.rpartition(".")
    if not dot or not stem:
        return f"{part} ({n})"
    return f"{stem} ({n}).{ext}"


def _inside(dest: Path, rel: str) -> bool:
    root = os.path.normcase(os.path.normpath(str(dest)))
    full = os.path.normcase(os.path.normpath(os.path.join(str(dest), rel)))
    return full.startswith(root.rstrip("\\/") + os.sep)


def plan(scan: ZipScan, key: str, dest: Path, *, compose_names: bool = True, skip_mac: bool = True) -> list[PlanItem]:
    """全項目の最終的な名前と状態(FR-19 の (1)〜(8) の順・FR-20)。"""
    files: set[str] = set()
    dirs: set[str] = set()
    out: list[PlanItem] = []
    dest_len = len(str(dest))
    for e in scan.entries:
        name = decode_name(e, key)
        if e.is_link:
            out.append(_skip(e, name, SKIP_LINK))
            continue
        parts = _SEP.split(name)                                                   # (1)
        named = [p for p in parts if p]
        if skip_mac and (any(p == "__MACOSX" for p in named) or (named and named[-1] == ".DS_Store")):
            out.append(_skip(e, name, SKIP_MAC))                                   # FR-20
            continue
        if name[:1] in ("/", "\\") or _DRIVE.match(parts[0]):                      # (2)
            out.append(_skip(e, name, SKIP_ABS))
            continue
        if any(p == ".." for p in parts):
            out.append(_skip(e, name, SKIP_UP))
            continue
        if "\x00" in name:
            out.append(_skip(e, name, SKIP_NUL))
            continue
        if any(_RESERVED.match(p.split(".", 1)[0].rstrip(" ")) for p in named):
            out.append(_skip(e, name, SKIP_DEVICE))
            continue
        if any(len(p) > 255 for p in parts):
            out.append(_skip(e, name, SKIP_LONG_PART))
            continue
        parts = [p for p in parts if p not in ("", ".")]                            # (3)
        if not parts:
            out.append(_skip(e, name, SKIP_EMPTY))
            continue
        reasons: list[str] = []
        clean = [_sanitize(p) for p in parts]                                       # (4)
        if clean != parts:
            reasons.append("使えない文字を _ に替えた")
        composed = False
        if compose_names:                                                           # (5)
            c2 = [compose(p) for p in clean]
            if c2 != clean:
                composed = True
                reasons.append("分かれた濁点をくっつけた")
            clean = c2
        anc_keys = ["\\".join(clean[:k]).casefold() for k in range(1, len(clean))]
        if any(k in files for k in anc_keys):
            out.append(_skip(e, name, SKIP_CLASH))
            continue
        k = "\\".join(clean).casefold()                                             # (6)
        if e.is_dir:
            if k in files:
                out.append(_skip(e, name, SKIP_CLASH))
                continue
        elif k in files or k in dirs:
            base = clean[-1]
            n = 2
            while True:
                clean[-1] = _numbered(base, n)
                k = "\\".join(clean).casefold()
                if k not in files and k not in dirs:
                    break
                n += 1
            reasons.append("同じ名前があるので番号を付けた")
        rel = "\\".join(clean)
        if dest_len + 1 + len(rel) >= MAX_PATH:                                     # (7)
            out.append(_skip(e, name, SKIP_LONG_PATH))
            continue
        if not _inside(dest, rel):                                                  # (8)
            out.append(_skip(e, name, SKIP_OUTSIDE))
            continue
        dirs.update(anc_keys)
        if e.is_dir:
            dirs.add(k)
        else:
            files.add(k)
        status = "rename" if reasons else "extract"
        out.append(PlanItem(e.index, name, rel, e.is_dir, status, "・".join(reasons), composed=composed, size=e.file_size))
    return out


# ------------------------------------------------------------------ 爆弾の上限(M-10・FR-21)
def bomb_reason(scan: ZipScan, max_total_gb: int) -> bool:
    """展開を始める前に止めるか((b)〜(d))。"""
    total = scan.declared_total
    if total > max_total_gb * GB:
        return True
    for e in scan.entries:
        if e.file_size > MB and (e.compress_size == 0 or e.file_size / e.compress_size > ENTRY_RATIO):
            return True
    comp = scan.compressed_total
    return total > GB and (comp == 0 or total / comp > TOTAL_RATIO)
