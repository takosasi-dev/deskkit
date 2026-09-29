# pypdf で出力を組む(まとめる・分ける・整理)。出力はいつも新しい PdfWriter に append で組み(P-4)、しおりは各ファイルの物を
# 順に並べるだけでファイル名の見出しは足さない(P-7)。入力のファイルは `open(p, "rb")` で開き、書き終えるまで閉じない(INV-1)。
# 中止の印はページの区切り(入力ごと・画像ごと・出力ごと)で見る(FR-15)。
from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deskkit.modules.pagepress import images
from deskkit.modules.pagepress.reader import Entry, ProbeError, open_pdf
from deskkit.modules.pagepress.sanitize import resolve_named

PHASE_READ = "読み込んでいます"
PHASE_BUILD = "組み立てています"
PHASE_WRITE = "書き出しています"
PHASE_VERIFY = "確かめています"


class CancelledError(Exception):
    pass


class InputFailedError(Exception):
    """まとめる・整理で、どれかの入力が読めなかった(FR-22)。index は一覧の何番目か。"""

    def __init__(self, index: int, code: str, message: str) -> None:
        super().__init__(message)
        self.index = index
        self.code = code
        self.message = message


@dataclass
class Control:
    cancel: threading.Event
    on_progress: Callable[[str, int, int], None] = lambda _p, _d, _t: None

    def check(self) -> None:
        if self.cancel.is_set():
            raise CancelledError

    def progress(self, phase: str, done: int = 0, total: int = 0) -> None:
        self.on_progress(phase, done, total)


@dataclass(frozen=True)
class ImageOpts:
    fit: str = "paper"
    paper: str = "a4"
    margin_mm: int = 10


def new_writer() -> Any:
    from pypdf import PdfWriter

    return PdfWriter()


def open_reader(path: Path, stack: ExitStack) -> Any:
    try:
        f = stack.enter_context(open(path, "rb"))  # noqa: SIM115 - 読み取り専用(INV-1)。書き終えるまで stack が持つ
    except OSError:
        raise ProbeError("unreadable", "ファイルを読めませんでした") from None
    return open_pdf(f)


def merge(entries: Sequence[Entry], opts: ImageOpts, ctl: Control, stack: ExitStack) -> Any:
    """一覧の順に1つの PDF にする。PDF は全ページ、画像は1枚1ページ。"""
    w = new_writer()
    total = sum(e.pages for e in entries)
    done = 0
    ctl.progress(PHASE_BUILD, 0, total)
    for i, e in enumerate(entries):
        ctl.check()
        try:
            if e.kind == "pdf":
                r = open_reader(e.path, stack)
                n = len(r.pages)
                off = len(w.pages)
                w.append(r)
                resolve_named(w, r, off, list(range(n)))
                done += n
            else:
                payload = images.prepare(e.path)
                pl = images.place(payload.display_size, payload.dpi, opts.fit, opts.paper, opts.margin_mm)
                images.add_page(w, payload, pl)
                done += 1
        except ProbeError as ex:
            raise InputFailedError(i, "unreadable" if ex.code != "encrypted" else "encrypted", ex.message) from None
        except (CancelledError, MemoryError):
            raise
        except Exception:  # noqa: BLE001 - pypdf の途中の失敗は「読めない」
            raise InputFailedError(i, "unreadable", "読み込めませんでした") from None
        ctl.progress(PHASE_BUILD, done, total)
    return w


def split_spans(reader: Any, spans: Sequence[tuple[int, int]], ctl: Control) -> Iterator[tuple[int, int, Any]]:
    """範囲(1 から数えた両端を含む)ごとに新しい writer を作って返す。"""
    total = len(spans)
    for k, (a, b) in enumerate(spans):
        ctl.check()
        w = new_writer()
        picked = list(range(a - 1, b))
        w.append(reader, pages=picked)
        resolve_named(w, reader, 0, picked)
        ctl.progress(PHASE_BUILD, k + 1, total)
        yield a, b, w


def organize(reader: Any, items: Sequence[tuple[int, int]], ctl: Control) -> Any:
    """items は (元のページ番号 0 始まり, 足す回転 0/90/180/270)。今の順番と回転の新しい PDF。"""
    ctl.progress(PHASE_BUILD, 0, len(items))
    w = new_writer()
    picked = [i for i, _r in items]
    w.append(reader, pages=picked)
    resolve_named(w, reader, 0, picked)
    for k, (_i, rot) in enumerate(items):
        if k % 50 == 0:
            ctl.check()
        if rot % 360:
            w.pages[k].rotate(rot % 360)  # 元の /Rotate に足す(FR-9)
    ctl.progress(PHASE_BUILD, len(items), len(items))
    return w
