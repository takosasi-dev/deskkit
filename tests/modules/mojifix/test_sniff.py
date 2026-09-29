# 文字のファイルかの判定と、候補の並べ方(AC-1・AC-8・FR-5・FR-6・FR-8・FR-9)。検体はテストの中で作る。
from __future__ import annotations

import pytest

from deskkit.modules.mojifix import sniff as S

from .conftest import ADDRESS


def _top(b: bytes) -> str:
    return S.sniff_bytes(b, len(b), 0).candidates[0].key


@pytest.mark.parametrize(("codec", "key"), [
    ("utf-8", "utf8"), ("cp932", "sjis"), ("euc_jp", "eucjp"), ("iso2022_jp", "jis"),
    ("utf-16-le", "u16le"), ("utf-16-be", "u16be"),
])
def test_ac1_address_book(codec: str, key: str) -> None:
    assert _top(ADDRESS.encode(codec)) == key


def test_ac1_halfwidth_kana_sjis() -> None:
    kana = "ﾌﾘｺﾐ,ﾔﾏﾀﾞ ﾀﾛｳ,10000\r\nﾌﾘｺﾐ,ｻﾄｳ ﾊﾅｺ,20000\r\n"
    assert _top(kana.encode("cp932")) == "sjis"


def test_six_candidates_and_labels() -> None:
    b = ADDRESS.encode("cp932")
    sn = S.sniff_bytes(b, len(b), 0)
    assert sn.kind == "text"
    assert sorted(c.key for c in sn.candidates) == sorted(k for k, _c, _l in S.CANDIDATES)
    top = sn.candidates[0]
    assert top.label == "Shift_JIS(古い Windows の形)" and top.state_text() == "読めました"
    assert top.lines[0] == "氏名,住所,電話番号"
    assert not sn.all_bad


def test_bom_first_and_mismatch() -> None:
    b = b"\xef\xbb\xbf" + ADDRESS.encode("utf-8")
    sn = S.sniff_bytes(b, len(b), 0)
    assert sn.bom == "utf8" and sn.candidates[0].key == "utf8" and sn.candidates[0].bom
    assert sn.candidates[0].lines[0] == "氏名,住所,電話番号"  # BOM は除いて読む
    bad = b"\xef\xbb\xbf" + ADDRESS.encode("cp932")
    sn2 = S.sniff_bytes(bad, len(bad), 0)
    assert sn2.bom_mismatch  # FR-9
    assert sn2.candidates[0].key == "sjis"  # BOM を除いて cp932 で読める
    le = b"\xff\xfe" + "あいう".encode("utf-16-le")
    assert _top(le) == "u16le"


def test_errors_are_counted_per_run() -> None:
    b = ("あいう\n" * 2).encode("utf-8") + ("かきく\n" * 3).encode("cp932")
    text, n = S.decode_all(b, "utf-8", True)
    assert n == 3 and text.count("�") == 3


def test_truncated_sample_does_not_count_partial_char() -> None:
    b = ("あ" * 100).encode("utf-8")
    sn = S.sniff_bytes(b[:-1], len(b), 0)  # 先頭だけを読んだ(最後の字が途中で切れた)
    assert next(c for c in sn.candidates if c.key == "utf8").errors == 0


@pytest.mark.parametrize("head", [b"\x89PNG\r\n\x1a\n" + bytes(40), b"MZ\x90\x00" + bytes(60), b"PK\x03\x04" + bytes(30),
                                  b"%PDF-1.7\n", b"GIF89a" + bytes(10), b"\xff\xd8\xff\xe0" + bytes(10),
                                  b"abc\x00\x01\x02\x03\xfe\xfd" * 50])
def test_ac8_binary(head: bytes) -> None:
    sn = S.sniff_bytes(head, len(head), 0)
    assert sn.kind == "binary"


def test_zip_head_flag() -> None:
    b = b"PK\x03\x04" + bytes(40)
    assert S.sniff_bytes(b, len(b), 0).is_zip


def test_utf16_with_nul_is_not_binary() -> None:
    b = "abc,def\r\n".encode("utf-16-le") * 10
    sn = S.sniff_bytes(b, len(b), 0)
    assert sn.kind == "text" and sn.candidates[0].key == "u16le"


def test_ascii_and_empty() -> None:
    b = b"a,b,c\r\n1,2,3\r\n"
    sn = S.sniff_bytes(b, len(b), 0)
    assert sn.kind == "ascii" and [c.key for c in sn.candidates] == ["utf8"]
    assert S.sniff_bytes(b"", 0, 0).kind == "empty"


def test_all_bad() -> None:
    b = bytes([0x82, 0xA0, 0x8E, 0xFF, 0xFE, 0x80]) * 30 + b"\x1b"
    sn = S.sniff_bytes(b, len(b), 0)
    assert sn.kind == "text" and sn.all_bad


def test_marks() -> None:
    assert S.marks("a\tb\r\n") == 0
    assert S.marks("\x01\x7f\x85") == 4


def test_sniff_file_reads_only_head(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "big.csv"
    p.write_bytes(ADDRESS.encode("cp932") * 5000)
    sn = S.sniff_file(p)
    assert sn.size == p.stat().st_size and sn.candidates[0].key == "sjis"
    assert len(sn.candidates[0].lines) == S.PREVIEW_LINES
