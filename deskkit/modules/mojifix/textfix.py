# テキストの確認の読みと書き出し(M-5・FR-10〜FR-16)。どちらも 1MB ずつの流し読みで、全体をメモリに載せない。
# 変えるのは (1) 文字の形 (2) 改行 (3) くっつける (4) 〓への置き換え だけ(FR-13・INV-4)。CSV は解釈しない(M-6)。
# 元のファイルは読み取り専用で開き、出力は O_EXCL で新しく作った名前にだけ書く(INV-1)。中止・失敗では自分の出力だけを消す(FR-4)。
from __future__ import annotations

import bisect
import codecs
import os
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from deskkit.modules.mojifix import sniff as S
from deskkit.modules.mojifix.compose import compose
from deskkit.modules.mojifix.owned import OutputError, Owned, is_denied, is_disk_full, reserve_file_in

CHUNK = 1024 * 1024
CONTEXT = 60
MAX_ISSUE_LINES = 20
SPACE_MARGIN = 100 * 1024 * 1024
OUT_SUFFIX = "_文字直し"   # Q-3 の回答: <元の名前>_文字直し.<拡張子>
SENTINEL = "﷐"        # 確認の読みで、読めない所の印(書くときは〓)

# 書き出しの形(M-4): key → (codec, 画面の名前)
OUTPUTS: dict[str, tuple[str, str]] = {
    "excel": ("utf-8-sig", "Excel で開ける形"),
    "sjis": ("cp932", "古いソフト用(Shift_JIS)"),
    "utf8": ("utf-8", "UTF-8(印なし)"),
}
NEWLINES: dict[str, str | None] = {"keep": None, "crlf": "\r\n", "lf": "\n"}
NEWLINE_LABEL = {"keep": "改行はそのまま", "crlf": "CRLF", "lf": "LF"}

MSG_CHANGED = "読み込んだあとにファイルが変わりました"
MSG_VERIFY = "書き出しを確かめられませんでした"
MSG_DISK_FULL = "空き容量が足りません"
MSG_NAMES = "同じ名前のファイルが多すぎます"
MSG_FAILED = "書き出せませんでした"
MSG_UNCHANGED = "変えるところがありません"


class CancelledError(Exception):
    pass


class TextError(Exception):
    """利用者に出す文と ops の reason。"""

    def __init__(self, reason: str | None, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class Options:
    in_key: str                  # sniff.CANDIDATES の key
    out: str                     # OUTPUTS の key
    newline: str = "keep"        # NEWLINES の key
    compose: bool = False
    geta_undecodable: bool = False
    geta_unencodable: bool = False

    @property
    def in_codec(self) -> str:
        return S.CODEC_OF[self.in_key]

    @property
    def out_codec(self) -> str:
        return OUTPUTS[self.out][0]

    def same_text(self, other: Options) -> bool:
        """確認の結果をそのまま使えるか(〓の選択と、書き出しの形だけが違う)。"""
        return (self.in_key, self.newline, self.compose) == (other.in_key, other.newline, other.compose)


@dataclass(frozen=True)
class IssueLine:
    line_no: int                              # 1 始まり
    segments: tuple[tuple[str, bool], ...]    # (文字列, 強調するか)。前後 60 字で切る


@dataclass
class CheckResult:
    options: Options
    in_bytes: int
    size: int
    mtime_ns: int
    lines: int = 0
    out_chars: int = 0
    undecodable: int = 0
    undecodable_lines: list[IssueLine] = field(default_factory=list)
    unencodable: int = 0
    unencodable_lines: list[IssueLine] = field(default_factory=list)
    newline_changes: int = 0
    composed: int = 0
    had_bom: str | None = None

    @property
    def first_undecodable_line(self) -> int | None:
        return self.undecodable_lines[0].line_no if self.undecodable_lines else None

    def unencodable_for(self, opts: Options) -> int:
        """書けない文字の数は「古いソフト用」のときだけ意味がある。"""
        return self.unencodable if opts.out == "sjis" else 0

    def unchanged(self, opts: Options | None = None) -> bool:
        """FR-14: 読み方と書き出しの形が同じで、改行・くっつける・〓で変わる所も無い。"""
        o = opts or self.options
        same = ((o.in_key == "utf8" and self.had_bom == "utf8" and o.out == "excel")
                or (o.in_key == "utf8" and self.had_bom != "utf8" and o.out == "utf8")
                or (o.in_key == "sjis" and o.out == "sjis"))
        return (same and self.newline_changes == 0 and self.composed == 0 and self.undecodable == 0
                and self.unencodable_for(o) == 0)


@dataclass(frozen=True)
class WriteResult:
    path: Path
    fallback: bool
    out_bytes: int
    lines: int
    chars: int
    replaced: int
    composed: int


# ------------------------------------------------------------------ 流し読みの芯
def _safe_cut(text: str) -> int:
    """区切りの最後で切ってよい位置(改行の後ろ)。CR は次の区切りへ持ち越す(FR-13)。改行が無ければ最後の ASCII の前。"""
    i = text.rfind("\n")
    if i >= 0:
        return i + 1
    for j in range(len(text) - 1, max(-1, len(text) - 4096), -1):
        ch = text[j]
        if ch < "\x80" and ch != "\r":
            return j
    return max(0, len(text) - 16)


def _pieces(f: BinaryIO, opts: Options, st: S.DecodeState, total: int, cancel: Callable[[], bool],
            progress: Callable[[int, int], None]) -> Iterator[tuple[str, int, int]]:
    """(改行とくっつけを当てた後の文字列, 改行を変えた数, くっつけた数) を区切りごとに出す。"""
    dec = codecs.getincrementaldecoder(opts.in_codec)(errors=S.ERR_DECODE)
    target = NEWLINES[opts.newline]
    carry = ""
    done = 0
    with S.use_decode(st):
        while True:
            if cancel():
                raise CancelledError()
            data = f.read(CHUNK)
            final = not data
            done += len(data)
            st.begin_chunk()
            text = carry + dec.decode(data, final=final)
            if final:
                piece, carry = text, ""
            else:
                cut = _safe_cut(text)
                piece, carry = text[:cut], text[cut:]
            nl_changes = 0
            if target is not None and piece:
                crlf = piece.count("\r\n")
                cr = piece.count("\r") - crlf
                lf = piece.count("\n") - crlf
                nl_changes = (cr + lf) if target == "\r\n" else (crlf + cr)
                if nl_changes:
                    piece = S.LINE_BREAK.sub(target, piece)
            composed = 0
            if opts.compose and piece and not piece.isascii():
                c = compose(piece)
                composed = len(piece) - len(c)
                piece = c
            progress(done, total)
            yield piece, nl_changes, composed
            if final:
                return


def _count_breaks(piece: str) -> int:
    crlf = piece.count("\r\n")
    return piece.count("\r") + piece.count("\n") - crlf


def _issue_lines(piece: str, positions: list[int], line_base: int, out: list[IssueLine], mark: Callable[[str], str]) -> None:
    """positions(piece の中の位置)がある行を、行番号と前後 60 字で out に足す(全体で 20 行まで)。"""
    if not positions or len(out) >= MAX_ISSUE_LINES:
        return
    starts = [0] + [m.end() for m in S.LINE_BREAK.finditer(piece)]
    seen = {x.line_no for x in out}
    pos_set = set(positions)
    for pos in sorted(positions):
        li = bisect.bisect_right(starts, pos) - 1
        no = line_base + li + 1
        if no in seen:
            continue
        seen.add(no)
        ls = starts[li]
        le = starts[li + 1] if li + 1 < len(starts) else len(piece)
        line = piece[ls:le].rstrip("\r\n")
        col = pos - ls
        a, b = max(0, col - CONTEXT), min(len(line), col + 1 + CONTEXT)
        segs: list[tuple[str, bool]] = []
        for k in range(a, b):
            hit = (ls + k) in pos_set
            ch = mark(line[k]) if hit else line[k]
            if segs and segs[-1][1] == hit:
                segs[-1] = (segs[-1][0] + ch, hit)
            else:
                segs.append((ch, hit))
        if a > 0:
            segs.insert(0, ("…", False))
        if b < len(line):
            segs.append(("…", False))
        out.append(IssueLine(no, tuple(segs)))
        if len(out) >= MAX_ISSUE_LINES:
            return


def _open_source(path: Path, opts: Options) -> tuple[BinaryIO, int, int, str | None]:
    f = open(path, "rb")  # noqa: SIM115 - 呼ぶ側で閉じる(読み取り専用)
    try:
        st = os.fstat(f.fileno())
        head = f.read(4)
        bom, bom_len = S.bom_of(head)
        f.seek(0 if opts.in_key == "utf8" else bom_len)  # UTF-8 以外は BOM を除いてから読む(M-3)
    except BaseException:
        f.close()
        raise
    return f, st.st_size, st.st_mtime_ns, bom


# ------------------------------------------------------------------ 確認の読み(M-5 の1回目)
def check(path: Path, opts: Options, cancel: Callable[[], bool] = lambda: False,
          progress: Callable[[int, int], None] = lambda _d, _t: None) -> CheckResult:
    try:
        f, size, mtime_ns, bom = _open_source(path, opts)
    except OSError:
        raise TextError("unreadable", S.MSG_UNREADABLE) from None
    res = CheckResult(opts, size, size, mtime_ns, had_bom=bom)
    dst = S.DecodeState(SENTINEL)
    enc_codec = opts.out_codec
    last = ""
    with f:
        try:
            for piece, nl, comp in _pieces(f, opts, dst, size, cancel, progress):
                res.newline_changes += nl
                res.composed += comp
                if not piece:
                    continue
                if SENTINEL in piece:
                    pos = [i for i, ch in enumerate(piece) if ch == SENTINEL]
                    _issue_lines(piece, pos, res.lines, res.undecodable_lines, lambda _c: "�")
                if enc_codec == "cp932" and not piece.isascii():
                    est = S.EncodeState()
                    with S.use_encode(est):
                        piece.encode("cp932", errors=S.ERR_ENCODE)
                    if est.positions:
                        res.unencodable += len(est.positions)
                        _issue_lines(piece, est.positions, res.lines, res.unencodable_lines, lambda c: c)
                res.lines += _count_breaks(piece)
                res.out_chars += len(piece)
                last = piece
        except OSError:
            raise TextError("unreadable", S.MSG_UNREADABLE) from None
    res.undecodable = dst.count
    if res.out_chars and not last.endswith(("\r", "\n")):
        res.lines += 1  # 最後の行(改行で終わらない)
    return res


# ------------------------------------------------------------------ 書き出し(M-5 の2回目・FR-15・FR-16)
def output_stem(src: Path) -> tuple[str, str]:
    return src.stem + OUT_SUFFIX, src.suffix


def write(path: Path, chk: CheckResult, opts: Options, owned: Owned, fallback: Callable[[], Path],
          cancel: Callable[[], bool] = lambda: False, progress: Callable[[str, int, int], None] = lambda _s, _d, _t: None,
          free_bytes: Callable[[Path], int] | None = None) -> WriteResult:
    """chk(確認の結果)どおりに書き出す。opts は chk.options と同じ文字列の扱い(same_text)で、〓の選択と形だけが違ってよい。"""
    if not opts.same_text(chk.options):
        raise ValueError("確認と違う読み方では書けない")
    try:
        st = os.stat(path)
    except OSError:
        raise TextError("unreadable", S.MSG_UNREADABLE) from None
    if st.st_size != chk.size or st.st_mtime_ns != chk.mtime_ns:
        raise TextError("changed", MSG_CHANGED)  # FR-15
    stem, ext = output_stem(path)
    holder: list[BinaryIO] = []

    def create(p: Path) -> None:
        holder.append(open(p, "xb"))  # noqa: SIM115 - O_EXCL で新しく作った名前にだけ書く(INV-1)

    try:
        out_path, used_fb = _reserve_with(path.parent, stem, ext, owned, fallback, create)
    except OutputError as e:
        raise TextError(e.code, MSG_DISK_FULL if e.code == "disk_full" else MSG_NAMES if e.code == "names_exhausted"
                        else MSG_FAILED) from None
    out_f = holder[-1]
    try:
        free = (free_bytes or _free)(out_path.parent)
        if free < chk.size * 2 + SPACE_MARGIN:
            raise TextError("disk_full", MSG_DISK_FULL)
        result = _write_body(path, out_f, out_path, chk, opts, cancel, progress)
        out_f.close()
        progress("verify", 0, result.out_bytes)
        _verify(out_path, opts, chk.out_chars, cancel, progress)
        return WriteResult(out_path, used_fb, result.out_bytes, result.lines, result.chars, result.replaced, result.composed)
    except BaseException:
        out_f.close()
        owned.discard()
        raise


def _reserve_with(folder: Path, stem: str, ext: str, owned: Owned, fallback: Callable[[], Path],
                  create: Callable[[Path], None]) -> tuple[Path, bool]:
    try:
        return reserve_file_in(folder, stem, ext, owned, create), False
    except OSError as e:
        if is_disk_full(e):
            raise OutputError("disk_full") from None
        if not is_denied(e) and not isinstance(e, FileNotFoundError):
            raise OutputError(None) from None
    fb = fallback()
    try:
        fb.mkdir(parents=True, exist_ok=True)
        return reserve_file_in(fb, stem, ext, owned, create), True
    except OSError as e:
        raise OutputError("disk_full" if is_disk_full(e) else None) from None


def _free(folder: Path) -> int:
    try:
        return int(shutil.disk_usage(folder).free)
    except OSError:
        return 1 << 62  # 分からなければ止めない(書き込みの失敗で FR-4 になる)


@dataclass
class _Body:
    out_bytes: int = 0
    lines: int = 0
    chars: int = 0
    replaced: int = 0
    composed: int = 0


def _write_body(path: Path, out_f: BinaryIO, out_path: Path, chk: CheckResult, opts: Options,
                cancel: Callable[[], bool], progress: Callable[[str, int, int], None]) -> _Body:
    body = _Body()
    try:
        f, _size, _m, _bom = _open_source(path, opts)
    except OSError:
        raise TextError("unreadable", S.MSG_UNREADABLE) from None
    dst = S.DecodeState(S.GETA if opts.geta_undecodable else SENTINEL)
    est = S.EncodeState()
    enc = codecs.getincrementalencoder(opts.out_codec)(errors=S.ERR_ENCODE if opts.geta_unencodable else "strict")
    last = ""
    with f:
        try:
            with S.use_encode(est):
                for piece, _nl, comp in _pieces(f, opts, dst, chk.size, cancel, lambda d, t: progress("write", d, t)):
                    body.composed += comp
                    if not piece:
                        continue
                    if not opts.geta_undecodable and SENTINEL in piece:
                        raise TextError("changed", MSG_CHANGED)  # 確認のあとに中身が変わった
                    try:
                        data = enc.encode(piece)
                    except UnicodeEncodeError:
                        raise TextError("changed", MSG_CHANGED) from None
                    out_f.write(data)
                    body.out_bytes += len(data)
                    body.lines += _count_breaks(piece)
                    body.chars += len(piece)
                    last = piece
                tail = enc.encode("", final=True)
                out_f.write(tail)
                body.out_bytes += len(tail)
            out_f.flush()
            os.fsync(out_f.fileno())
        except OSError as e:
            if is_disk_full(e):
                raise TextError("disk_full", MSG_DISK_FULL) from None
            raise TextError(None, MSG_FAILED) from None
    if body.chars and not last.endswith(("\r", "\n")):
        body.lines += 1
    body.replaced = dst.count + len(est.positions)
    return body


def _verify(out_path: Path, opts: Options, expected_chars: int, cancel: Callable[[], bool],
            progress: Callable[[str, int, int], None]) -> None:
    """FR-16: 出力をその形で最後まで読み直し、文字数が確認で数えた「書く文字数」と同じか確かめる。"""
    n = 0
    try:
        total = out_path.stat().st_size
        dec = codecs.getincrementaldecoder(opts.out_codec)(errors="strict")
        done = 0
        with open(out_path, "rb") as f:
            while True:
                if cancel():
                    raise CancelledError()
                data = f.read(CHUNK)
                done += len(data)
                n += len(dec.decode(data, final=not data))
                progress("verify", done, total)
                if not data:
                    break
    except (OSError, UnicodeDecodeError):
        raise TextError("verify_failed", MSG_VERIFY) from None
    if n != expected_chars:
        raise TextError("verify_failed", MSG_VERIFY)
