# クリップボードへの書き込み(利用者の明示操作のときだけ。INV-8)。自己印 DeskKit.ClipShelf.Origin に項目 ID を入れ、
# 元の内容に除外形式があれば値ごと引き継ぐ(D-11 / INV-7)。書式なし化(FR-13)・{clipboard} 用の読み取り・全消去時の空化。
# OpenClipboard の所有者は host の隠しウィンドウ。開けなければ短く再試行してあきらめる。
from __future__ import annotations

import struct
import time
from collections.abc import Callable
from enum import Enum

from deskkit.modules.clipshelf._win32 import (
    CF_UNICODETEXT,
    EXCLUSION_FORMATS,
    FMT_ORIGIN,
    Win32Api,
)

_ORIGIN_MAGIC = b"CSO1"
_FALLBACK_FLAG = b"\x00\x00\x00\x00"  # 値が読めない除外形式は DWORD 0(=許可しない)で書く(安全側)
Marks = dict[str, bytes]  # 除外形式名 → 値


def encode_origin(item_id: int) -> bytes:
    return _ORIGIN_MAGIC + struct.pack("<q", int(item_id))


def decode_origin(data: bytes | None) -> int | None:
    """自己印から項目 ID を取り出す。0 や壊れた印は None(項目なし)。"""
    if not data or len(data) < 12 or not data.startswith(_ORIGIN_MAGIC):
        return None
    (item_id,) = struct.unpack("<q", data[4:12])
    return int(item_id) if item_id > 0 else None


class PlainResult(Enum):
    OK = "ok"
    NO_TEXT = "no_text"
    BUSY = "busy"
    FAILED = "failed"


class ClipWriter:
    def __init__(self, api: Win32Api, owner_hwnd: Callable[[], int], retry: Callable[[], int],
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._api = api
        self._hwnd = owner_hwnd
        self._retry = retry
        self._sleep = sleep
        self._fmt_cache: dict[str, int] = {}

    def fmt(self, name: str) -> int:
        if name not in self._fmt_cache:
            self._fmt_cache[name] = self._api.register_format(name)
        return self._fmt_cache[name]

    def open(self) -> bool:
        n = max(1, self._retry())
        for attempt in range(n):
            if self._api.open_clipboard(self._hwnd()):
                return True
            if attempt + 1 < n:
                self._sleep(0.015 * (attempt + 1))
        return False

    def _read_marks_locked(self) -> Marks:
        """開いている間に、存在する除外形式の値を読む(本文形式は読まない)。"""
        present = set(self._api.enum_formats())
        marks: Marks = {}
        for name in EXCLUSION_FORMATS:
            fid = self.fmt(name)
            if fid and fid in present:
                data = self._api.get_data_bytes(fid)
                marks[name] = data if data else _FALLBACK_FLAG
        return marks

    def _write_locked(self, text: str, origin_id: int, marks: Marks) -> bool:
        if not self._api.empty_clipboard():
            return False
        ok = self._api.set_unicode_text(text)
        ok = self._api.set_data_bytes(self.fmt(FMT_ORIGIN), encode_origin(origin_id)) and ok
        for name, value in marks.items():
            # 除外形式を落とさない(INV-7)。書けなければ失敗として扱う
            ok = self._api.set_data_bytes(self.fmt(name), value or _FALLBACK_FLAG) and ok
        return ok

    def write(self, text: str, origin_id: int, marks: Marks | None = None) -> bool:
        """text を CF_UNICODETEXT と自己印つきで置く。marks があれば除外形式も書く。"""
        if not self.open():
            return False
        try:
            ok = self._write_locked(text, origin_id, dict(marks or {}))
            if not ok and marks:
                # 除外形式を付けられなかった内容を残さない(INV-7 を守れないなら空にする)
                self._api.empty_clipboard()
            return ok
        finally:
            self._api.close_clipboard()

    def plain_text(self) -> PlainResult:
        """現在のテキストだけを残して書き直す。除外形式は引き継ぐ(FR-13 / D-11)。"""
        if not self.open():
            return PlainResult.BUSY
        try:
            if CF_UNICODETEXT not in set(self._api.enum_formats()):
                return PlainResult.NO_TEXT
            marks = self._read_marks_locked()
            text = self._api.get_unicode_text(2**31 - 3)
            if text is None:
                return PlainResult.NO_TEXT
            ok = self._write_locked(text, 0, marks)
            if not ok and marks:
                self._api.empty_clipboard()
            return PlainResult.OK if ok else PlainResult.FAILED
        finally:
            self._api.close_clipboard()

    def read_for_expansion(self) -> tuple[str | None, Marks] | None:
        """{clipboard} 展開用に現在のテキストと除外形式を読む。開けなければ None。"""
        if not self.open():
            return None
        try:
            marks = self._read_marks_locked()
            text = self._api.get_unicode_text(2**31 - 3) if CF_UNICODETEXT in set(self._api.enum_formats()) else None
            return text, marks
        finally:
            self._api.close_clipboard()

    def clear(self) -> bool:
        if not self.open():
            return False
        try:
            return self._api.empty_clipboard()
        finally:
            self._api.close_clipboard()

    def has_text(self) -> bool:
        """本文を読まずに、テキスト形式があるかだけを見る(プレビューの警告用)。"""
        return self._api.is_format_available(CF_UNICODETEXT)
