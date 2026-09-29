# 文字のファイルかの判定(FR-5・FR-8)と、読み方の候補の並べ方(M-2・M-3)。先頭 256KB だけを読む。
# 読めないバイトの並び1つを1か所と数える独自のエラー処理(codecs.register_error)もここに置く(FR-11 の〓も同じ処理)。
# 判定は並べる順にしか使わない。どれで読むかは利用者が選ぶ(M-1)。外部の判定ライブラリは使わない(M-0・INV-7)。
from __future__ import annotations

import codecs
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

SAMPLE_BYTES = 256 * 1024
BINARY_PROBE = 64 * 1024
PREVIEW_LINES = 40
CARD_LINES = 8
LINE_CHARS = 200

# (key, codec, 画面の名前)。並びは M-2 (4) の表の順
CANDIDATES: tuple[tuple[str, str, str], ...] = (
    ("utf8", "utf-8-sig", "UTF-8(いまの標準の形)"),
    ("sjis", "cp932", "Shift_JIS(古い Windows の形)"),
    ("eucjp", "euc_jp", "EUC-JP(古いサーバーの形)"),
    ("u16le", "utf-16-le", "UTF-16 LE(メモ帳の Unicode)"),
    ("u16be", "utf-16-be", "UTF-16 BE"),
    ("jis", "iso2022_jp_ext", "ISO-2022-JP(古いメールの形)"),
)
CODEC_OF = {k: c for k, c, _l in CANDIDATES}
LABEL_OF = {k: lb for k, _c, lb in CANDIDATES}
BOMS: tuple[tuple[bytes, str], ...] = ((b"\xef\xbb\xbf", "utf8"), (b"\xff\xfe", "u16le"), (b"\xfe\xff", "u16be"))
MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"%PDF", b"\x89PNG", b"\xff\xd8\xff", b"MZ", b"GIF8")
LINE_BREAK = re.compile(r"\r\n|\r|\n")

MSG_EMPTY = "空のファイルです"
MSG_BINARY = "文字のファイルではないようです"
MSG_ASCII = "英数字だけのファイルです。どの形で読んでも同じです"
MSG_ALL_BAD = "どの候補にも読めない所があります"
MSG_BOM_MISMATCH = "先頭の文字の形の印と、中身が合っていません"
MSG_UNREADABLE = "読めませんでした(ほかのソフトが使っていれば、閉じてから試してください)"

# ------------------------------------------------------------------ 読めないバイトの並びの数え方
ERR_DECODE = "mojifix.undecodable"
ERR_ENCODE = "mojifix.unencodable"
GETA = "〓"
_tls = threading.local()


class DecodeState:
    """1回の読み(ファイル1つ)の、読めない所の数え方。並び(続いた読めないバイト)1つにつき repl を1つ出す。"""

    def __init__(self, repl: str) -> None:
        self.repl = repl
        self.count = 0
        self._last: tuple[int, int] = (0, -1)
        self._edge_now = False
        self._carry_edge = False
        self._first_in_chunk = True

    def begin_chunk(self) -> None:
        self._carry_edge = self._edge_now
        self._edge_now = False
        self._first_in_chunk = True

    def handle(self, exc: UnicodeDecodeError) -> tuple[str, int]:
        obj_id = id(exc.object)
        cont = (self._last == (obj_id, exc.start)) or (self._first_in_chunk and self._carry_edge and exc.start == 0)
        self._first_in_chunk = False
        self._carry_edge = False
        self._last = (obj_id, exc.end)
        self._edge_now = exc.end >= len(exc.object)
        if cont:
            return "", exc.end
        self.count += 1
        return self.repl, exc.end


class EncodeState:
    """書けない文字の位置(文字列の中の位置)を集め、1字につき〓1つにする。"""

    def __init__(self) -> None:
        self.positions: list[int] = []

    def handle(self, exc: UnicodeEncodeError) -> tuple[str, int]:
        self.positions.extend(range(exc.start, exc.end))
        return GETA * (exc.end - exc.start), exc.end


def _decode_error(exc: UnicodeError) -> tuple[str, int]:
    st = getattr(_tls, "dec", None)
    if not isinstance(exc, UnicodeDecodeError):
        raise exc
    if st is None:
        return "�", exc.end
    return st.handle(exc)  # type: ignore[no-any-return]


def _encode_error(exc: UnicodeError) -> tuple[str, int]:
    st = getattr(_tls, "enc", None)
    if not isinstance(exc, UnicodeEncodeError):
        raise exc
    if st is None:
        return GETA * (exc.end - exc.start), exc.end
    return st.handle(exc)  # type: ignore[no-any-return]


codecs.register_error(ERR_DECODE, _decode_error)
codecs.register_error(ERR_ENCODE, _encode_error)


class use_decode:  # noqa: N801 - with 文で使う小さな切り替え
    def __init__(self, st: DecodeState) -> None:
        self.st = st

    def __enter__(self) -> DecodeState:
        self._old = getattr(_tls, "dec", None)
        _tls.dec = self.st
        return self.st

    def __exit__(self, *_a: object) -> None:
        _tls.dec = self._old


class use_encode:  # noqa: N801
    def __init__(self, st: EncodeState) -> None:
        self.st = st

    def __enter__(self) -> EncodeState:
        self._old = getattr(_tls, "enc", None)
        _tls.enc = self.st
        return self.st

    def __exit__(self, *_a: object) -> None:
        _tls.enc = self._old


def decode_all(data: bytes, codec: str, final: bool, repl: str = "�") -> tuple[str, int]:
    """data を codec で読み、(文字列, 読めない所の数) を返す。final でなければ末尾の途中の文字は数えない。"""
    st = DecodeState(repl)
    dec = codecs.getincrementaldecoder(codec)(errors=ERR_DECODE)
    with use_decode(st):
        st.begin_chunk()
        text = dec.decode(data, final=final)
    return text, st.count


_REPL_RUN = re.compile("�+")


def decode_quick(data: bytes, codec: str, final: bool) -> tuple[str, int]:
    """候補づくり用の速い読み(C の replace で読み、続いた U+FFFD を1か所と数える)。合わない候補は読めない所が
    数十万になり、Python の処理を1か所ずつ呼ぶと 256KB で 0.5 秒かかったため(実測)。"""
    text = codecs.getincrementaldecoder(codec)(errors="replace").decode(data, final=final)
    return text, len(_REPL_RUN.findall(text))


# ------------------------------------------------------------------ 判定
def bom_of(head: bytes) -> tuple[str | None, int]:
    for b, key in BOMS:
        if head.startswith(b):
            return key, len(b)
    return None, 0


def nul_pattern(b: bytes) -> str | None:
    """UTF-16 らしい 0x00 の並び(M-2)。奇数番目(0 始まりの添字 1, 3, …)に多ければ LE、偶数番目なら BE。"""
    if not b:
        return None
    even, odd = b[0::2], b[1::2]
    ze = even.count(0) / max(1, len(even))
    zo = odd.count(0) / max(1, len(odd))
    if zo >= 0.10 and zo >= ze * 4 and zo > 0:
        return "le"
    if ze >= 0.10 and ze >= zo * 4 and ze > 0:
        return "be"
    return None


def marks(s: str) -> int:
    """化けの印(M-2 (3)): U+E000〜F8FF・U+0080〜009F・U+007F・タブ/CR/LF 以外の U+0000〜001F。"""
    return len(_MARKS.findall(s))


_MARKS = re.compile("[-\x80-\x9f\x7f\x00-\x08\x0b\x0c\x0e-\x1f]")


def is_binary(head: bytes) -> bool:
    """FR-5: よく知られた先頭の並び、または先頭 64KB の 0x00(BOM も UTF-16 の並びも無いとき)。"""
    if head.startswith(MAGIC):
        return True
    probe = head[:BINARY_PROBE]
    if b"\x00" not in probe:
        return False
    return bom_of(head)[0] is None and nul_pattern(probe) is None


def is_zip_head(head: bytes) -> bool:
    return head.startswith((b"PK\x03\x04", b"PK\x05\x06"))


def split_lines(text: str, n: int, width: int = LINE_CHARS) -> list[str]:
    out: list[str] = []
    for i, line in enumerate(LINE_BREAK.split(text)):
        if i >= n:
            break
        out.append(line[:width])
    return out


@dataclass
class Candidate:
    key: str
    codec: str
    label: str
    errors: int
    marks: int
    demoted: bool
    bom: bool                      # このファイルの先頭の印(BOM)がこの形のもの
    lines: list[str] = field(default_factory=list)

    def state_text(self) -> str:
        return "読めました" if self.errors == 0 else f"読めない所が {self.errors:,} か所"


@dataclass
class Sniff:
    kind: str                      # "text" | "ascii" | "empty" | "binary"
    size: int
    mtime_ns: int
    bom: str | None = None
    candidates: list[Candidate] = field(default_factory=list)
    is_zip: bool = False

    @property
    def all_bad(self) -> bool:
        return bool(self.candidates) and all(c.errors > 0 for c in self.candidates)

    @property
    def bom_mismatch(self) -> bool:
        return any(c.bom and c.errors > 0 for c in self.candidates)

    def candidate(self, key: str) -> Candidate | None:
        return next((c for c in self.candidates if c.key == key), None)


def _candidate(key: str, sample: bytes, final: bool, bom: str | None, bom_len: int, pat: str | None) -> Candidate:
    codec = CODEC_OF[key]
    data = sample if key == "utf8" else sample[bom_len:]
    text, errors = decode_quick(data, codec, final)
    if key == "u16le":
        demoted = bom != "u16le" and pat != "le"
    elif key == "u16be":
        demoted = bom != "u16be" and pat != "be"
    elif key == "jis":
        demoted = b"\x1b" not in sample or not sample.isascii()
    else:
        demoted = False
    return Candidate(key, codec, LABEL_OF[key], errors, marks(text), demoted, bom == key,
                     split_lines(text, PREVIEW_LINES))


def rank(sample: bytes, final: bool) -> tuple[str | None, list[Candidate]]:
    """M-2 の順に並べた候補(M-3 の6つ)と BOM の種類。"""
    bom, bom_len = bom_of(sample)
    pat = nul_pattern(sample)
    cands = [_candidate(k, sample, final, bom, bom_len, pat) for k, _c, _l in CANDIDATES]
    order = {k: i for i, (k, _c, _l) in enumerate(CANDIDATES)}
    cands.sort(key=lambda c: (c.demoted, c.errors, c.marks, order[c.key]))
    top = next((c for c in cands if c.bom and c.errors == 0), None)
    if top is not None:  # BOM の候補は、読めない所が 0 なら先頭に置く
        cands.remove(top)
        cands.insert(0, top)
    return bom, cands


def sniff_bytes(sample: bytes, size: int, mtime_ns: int) -> Sniff:
    if size == 0:
        return Sniff("empty", 0, mtime_ns)
    if is_binary(sample):
        return Sniff("binary", size, mtime_ns, is_zip=is_zip_head(sample))
    final = len(sample) >= size
    if sample.isascii() and b"\x1b" not in sample and b"\x00" not in sample:
        text, errors = decode_all(sample, "utf-8-sig", final)
        c = Candidate("utf8", "utf-8-sig", LABEL_OF["utf8"], errors, marks(text), False, False,
                      split_lines(text, PREVIEW_LINES))
        return Sniff("ascii", size, mtime_ns, None, [c])
    bom, cands = rank(sample, final)
    return Sniff("text", size, mtime_ns, bom, cands)


def sniff_file(path: Path) -> Sniff:
    """先頭 256KB を読み取り専用で読んで判定する。読めなければ OSError(呼ぶ側が MSG_UNREADABLE を出す)。"""
    with open(path, "rb") as f:
        st = os.fstat(f.fileno())
        sample = f.read(SAMPLE_BYTES)
    return sniff_bytes(sample, st.st_size, st.st_mtime_ns)
