# 個人情報らしい文字列の候補(FR-11・AC-7)。正規表現は文字列だけで全種類を確かめ、実機の文字認識は win32_real。
from __future__ import annotations

import pytest

from deskkit.modules.sendprep import ocr


def kinds(text: str, words: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    t = ocr.normalize(text)
    return [(k, t[s:e]) for k, s, e in ocr.find_spans(text, words)]


@pytest.mark.parametrize(("text", "want"), [
    ("連絡は test.user+x@example.co.jp まで", ("email", "test.user+x@example.co.jp")),
    ("ＴＥＳＴ＠ＥＸＡＭＰＬＥ．ＣＯＭ", ("email", "TEST@EXAMPLE.COM")),
    ("電話 03-1234-5678", ("phone", "03-1234-5678")),
    ("携帯 090-1234-5678 です", ("phone", "090-1234-5678")),
    ("携帯 09012345678", ("phone", "09012345678")),
    ("０９０ー１２３４ー５６７８", ("phone", "090-1234-5678")),
    ("(03)1234-5678", ("phone", "(03)1234-5678")),
    ("0120(12)3456", ("phone", "0120(12)3456")),
    ("+81 90-1234-5678", ("phone", "+81 90-1234-5678")),
    ("十 81 90 1234 5678", ("phone", "十 81 90 1234 5678")),
    ("+819012345678", ("phone", "+819012345678")),
    ("〒100-0001 東京都", ("postal", "〒100-0001")),
    ("〒 100 0001", ("postal", "〒 100 0001")),
    ("住所 150-0002 渋谷", ("postal", "150-0002")),
    ("Twitter @alice_01 です", ("user", "@alice_01")),
    ("by @Bob.Smith", ("user", "@Bob.Smith")),
])
def test_each_kind(text: str, want: tuple[str, str]) -> None:
    assert want in kinds(text)


@pytest.mark.parametrize("text", [
    "価格は 12345678 円", "2026-09-25", "03-1234", "123-4567-8901 は番号ではない", "mail@example", "a@b", "ver 1.2.3",
    "1234-5678",
])
def test_not_candidates(text: str) -> None:
    got = [k for k, _ in kinds(text)]
    assert "phone" not in got and "email" not in got and "postal" not in got


def test_email_not_user_and_my_words() -> None:
    got = kinds("user@example.com 山田太郎 様 YAMADA", ("山田太郎", "yamada"))
    assert ("email", "user@example.com") in got and not [k for k, _ in got if k == "user"]
    assert ("word", "山田太郎") in got and ("word", "YAMADA") in got


def _line(*words: tuple[str, float]) -> ocr.Line:
    return ocr.Line(tuple(ocr.Word(t, (x, 10.0, 30.0 * max(1, len(t) // 3), 20.0)) for t, x in words))


def test_find_candidates_rects_and_joins() -> None:
    lines = [
        _line(("連", 0), ("絡", 30), ("先", 60), ("test.user@example.com", 100)),
        _line(("十", 0), ("81", 30), ("90", 70), ("1234", 110), ("5678", 160)),
        _line(("山", 0), ("田", 30), ("太", 60), ("郎", 90)),  # 日本語は1文字ずつに分かれる → 空白なしでつないで探す
        _line(("〒", 0), ("100-0001", 30)),
    ]
    cands = ocr.find_candidates(lines, ["山田太郎"])
    by = {c.kind: c for c in cands}
    assert set(by) == {"email", "phone", "word", "postal"}
    x, y, w, h = by["word"].rect
    assert x <= 0 + 1 and x + w >= 90 + 30 and y <= 10 and y + h >= 30
    assert by["phone"].rect[0] <= 1 and by["phone"].label == "電話番号"
    assert not hasattr(by["email"], "text")  # 文字は持たない(INV-4)


def test_availability_is_cached() -> None:
    a = ocr.availability()
    assert a == ocr.availability() and a[1] in ("ok", "import_failed", "no_language")


@pytest.mark.win32_real
def test_ac7_real_ocr() -> None:
    from PIL import Image, ImageDraw, ImageFont

    ok, _ = ocr.availability()
    if not ok:
        pytest.skip("この PC では文字認識が使えません")
    img = Image.new("RGB", (1400, 560), "white")
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype("C:/Windows/Fonts/meiryo.ttc", 40)
    for i, s in enumerate(["連絡先 test.user@example.com", "電話 090-1234-5678", "〒100-0001 東京都", "Twitter @alice です"]):
        d.text((60, 40 + i * 120), s, fill="black", font=f)
    cands = ocr.find_candidates(ocr.recognize(img))
    assert {"email", "phone", "postal", "user"} <= {c.kind for c in cands}
