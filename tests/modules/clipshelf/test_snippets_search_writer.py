# AC-14(定型文の展開)、FR-10(検索: NFKC・全語一致・ピン→last_used 降順)、writer(自己印・除外形式の引き継ぎ・書式なし化)。
from __future__ import annotations

from datetime import timedelta

from deskkit.modules.clipshelf import search, snippets
from deskkit.modules.clipshelf._win32 import FMT_CAN_INCLUDE_HISTORY, FMT_CAN_UPLOAD_CLOUD, FMT_EXCLUDE_MONITOR, FMT_ORIGIN
from deskkit.modules.clipshelf.fakes import FakeClock, FakeWin32
from deskkit.modules.clipshelf.store import Item
from deskkit.modules.clipshelf.writer import ClipWriter, PlainResult, decode_origin, encode_origin


# ------------------------------------------------------------------ AC-14
def test_expand_exact() -> None:
    clock = FakeClock()
    exp = snippets.expand("{date} {time} {clipboard} {{x}} {unknown}", clock(), "コピー済み")
    assert exp.text == "2026-10-01 10:00 コピー済み {x} {unknown}"
    assert exp.unknown == ["unknown"]
    assert any("{unknown}" in w for w in exp.warnings)
    assert exp.used_clipboard


def test_expand_formats_and_escapes() -> None:
    clock = FakeClock()
    exp = snippets.expand("{{date}} は {date}", clock(), None, date_format="%Y年%m月%d日")
    assert exp.text == "{date} は 2026年10月01日"
    assert not exp.used_clipboard and not exp.warnings


def test_expand_empty_clipboard_warns() -> None:
    exp = snippets.expand("[{clipboard}]", FakeClock()(), None)
    assert exp.text == "[]" and exp.warnings


def test_expand_preview_marker_does_not_need_clipboard() -> None:
    exp = snippets.expand("a {clipboard} b", FakeClock()(), None, clipboard_marker="〔C〕")
    assert exp.text == "a 〔C〕 b" and not exp.warnings


# ------------------------------------------------------------------ FR-10
def _item(i: int, text: str, minutes: int, pinned: bool = False) -> Item:
    t = FakeClock()() + timedelta(minutes=minutes)
    return Item(i, "history", t, t, pinned, text, None, None)


def test_search_terms_nfkc_and_order() -> None:
    items = [
        _item(1, "Hello ＷＯＲＬＤ サンプル", 1),
        _item(2, "hello there", 5),
        _item(3, "world hello", 3, pinned=True),
        _item(4, "ﾊﾛｰ ワールド", 2),
    ]
    assert [i.id for i in search.search(items, "hello world")] == [3, 1]
    assert [i.id for i in search.search(items, "HELLO")] == [3, 2, 1]
    assert [i.id for i in search.search(items, "ハロー")] == [4]  # 半角カナも NFKC で一致
    assert [i.id for i in search.search(items, "")] == [3, 2, 4, 1]


def test_search_includes_snippet_name() -> None:
    t = FakeClock()()
    snip = Item(9, "snippet", t, t, False, "本文", None, "署名")
    assert search.search([snip], "署名") == [snip]


# ------------------------------------------------------------------ writer
def _writer(api: FakeWin32) -> ClipWriter:
    return ClipWriter(api, lambda: 0x99, lambda: 3, sleep=lambda _s: None)


def test_origin_roundtrip() -> None:
    assert decode_origin(encode_origin(42)) == 42
    assert decode_origin(encode_origin(0)) is None
    assert decode_origin(b"garbage") is None


def test_write_sets_origin_and_marks() -> None:
    api = FakeWin32()
    w = _writer(api)
    assert w.write("abc", 7, {FMT_EXCLUDE_MONITOR: b"\x01"})
    names = api.format_names_now()
    assert {"CF_UNICODETEXT", FMT_ORIGIN, FMT_EXCLUDE_MONITOR} <= names
    assert decode_origin(api.clip[api.fmt_id(FMT_ORIGIN)]) == 7
    assert api.text_now() == "abc" and not api.is_open


def test_plain_text_drops_rich_formats_and_keeps_exclusion() -> None:
    # AC-12 / AC-13(偽物で)
    api = FakeWin32()
    api.put("本文", formats={"HTML Format": b"<b>x</b>", "Rich Text Format": b"{\\rtf1}", FMT_CAN_INCLUDE_HISTORY: b"",
                           FMT_CAN_UPLOAD_CLOUD: b"\x00\x00\x00\x00"})
    assert _writer(api).plain_text() == PlainResult.OK
    names = api.format_names_now()
    assert "HTML Format" not in names and "Rich Text Format" not in names
    assert FMT_CAN_INCLUDE_HISTORY in names and FMT_CAN_UPLOAD_CLOUD in names
    # 値が読めなかった除外形式は DWORD 0(許可しない)で書く
    assert api.clip[api.fmt_id(FMT_CAN_INCLUDE_HISTORY)] == b"\x00\x00\x00\x00"
    assert api.text_now() == "本文"


def test_plain_text_without_text() -> None:
    api = FakeWin32()
    api.put(None, formats={"PNG": b"x"})
    assert _writer(api).plain_text() == PlainResult.NO_TEXT
    assert "PNG" in api.format_names_now()  # 何もしない


def test_plain_text_busy() -> None:
    api = FakeWin32()
    api.put("x")
    api.busy_opens = 10
    assert _writer(api).plain_text() == PlainResult.BUSY


def test_read_for_expansion_returns_marks() -> None:
    api = FakeWin32()
    api.put("pw", formats={FMT_EXCLUDE_MONITOR: b"\x05\x00\x00\x00"})
    got = _writer(api).read_for_expansion()
    assert got is not None
    text, marks = got
    assert text == "pw" and marks == {FMT_EXCLUDE_MONITOR: b"\x05\x00\x00\x00"}
