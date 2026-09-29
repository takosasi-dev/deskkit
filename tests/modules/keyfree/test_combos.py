# 組み合わせの一覧(K-4)・配列に無い記号(K-10)・Windows 用の印(K-5)・おすすめ(FR-11)・表示名の純粋な関数のテスト。
from __future__ import annotations

from deskkit.modules.keyfree import combos, keynames
from deskkit.modules.keyfree.fakes import FakeKeyboard, full_symbols

C, A, S, WIN = combos.MOD_CONTROL, combos.MOD_ALT, combos.MOD_SHIFT, combos.MOD_WIN


def test_key_count_is_88() -> None:
    assert len(keynames.ALL_KEYS) == 88
    assert len(set(keynames.ALL_KEYS)) == 88
    assert [len(r.vks) for r in keynames.ROWS] == [26, 10, 24, 12, 16]


def test_default_and_include_win_counts() -> None:
    """AC-2: 既定は 347、include_win で 956。F12・Ctrl+Alt+Delete・Win+L は渡さない。"""
    full = full_symbols()
    p = combos.build(False, full)
    assert len(p.probe) == 347
    assert len(p.reserved) == 5
    assert len(p.groups) == 4
    pw = combos.build(True, full)
    assert len(pw.probe) == 956
    assert len(pw.reserved) == 12
    for plan in (p, pw):
        assert all(vk != combos.VK_F12 for _m, vk in plan.probe)
        assert (C | A, combos.VK_DELETE) not in plan.probe
        assert (WIN, combos.VK_L) not in plan.probe
        assert all(combos.modifier_count(m) >= 2 for m, _vk in plan.probe)
    assert all(not m & WIN for m, _vk in p.probe)


def test_missing_symbol_removes_column() -> None:
    """AC-3: VK_OEM_102 が 0 なら、その列が表から消え、渡る組が M 個減る。"""
    chars = full_symbols()
    del chars[0xE2]
    p = combos.build(False, chars)
    assert len(p.probe) == 347 - 4
    assert 0xE2 not in p.keys
    pw = combos.build(True, chars)
    assert len(pw.probe) == 956 - 11


def test_layout_chars_masks_dead_keys_and_skips_zero() -> None:
    kb = FakeKeyboard({0xBA: ";"})
    kb.vk_to_char = lambda vk: (0x80000000 | ord("^")) if vk == 0xDE else (ord(";") if vk == 0xBA else 0)  # type: ignore[method-assign]
    chars = keynames.layout_chars(kb)
    assert chars == {0xBA: ";", 0xDE: "^"}


def test_key_labels() -> None:
    chars = {0xBA: ":"}
    assert keynames.key_label(0xBA, chars) == ":"
    assert keynames.key_label(0x41, chars) == "A"
    assert keynames.key_label(0x35, chars) == "5"
    assert keynames.key_label(0x87, chars) == "F24"
    assert keynames.key_label(0x20, chars) == "Space"
    assert keynames.key_label(0x2C, chars) == "PrtSc"


def test_windows_mark_and_reserved() -> None:
    """AC-4 の前半: Windows キーと PrintScreen を含む組には印。"""
    assert combos.windows_mark(WIN | C, 0x4B)
    assert combos.windows_mark(C | A, keynames.VK_SNAPSHOT)
    assert combos.windows_mark(C | A, combos.VK_F12)
    assert not combos.windows_mark(C | A, 0x4B)
    assert combos.is_reserved(WIN, combos.VK_L)
    assert combos.is_reserved(C | A, combos.VK_DELETE)
    assert not combos.is_reserved(C | A | S, combos.VK_DELETE)


def test_group_names_follow_format_order() -> None:
    assert combos.group_name(C | A | S) == "Ctrl+Alt+Shift"
    assert combos.group_name(WIN | C | A | S) == "Win+Ctrl+Alt+Shift"
    assert combos.parse_group("alt + shift") == A | S
    assert combos.parse_group("Ctrl+Nope") is None
    assert combos.parse_group("") is None


def test_normalize_order() -> None:
    assert combos.normalize_order(["Alt+Shift", "alt+shift", "bogus", 3]) == ["Alt+Shift"]
    assert combos.normalize_order("x") == list(combos.DEFAULT_ORDER)
    assert combos.normalize_order([]) == list(combos.DEFAULT_ORDER)


def test_recommend_order_and_filters() -> None:
    """FR-11・AC-4: 空き・印なし・英字か数字だけを、修飾キーの順に count 個まで。"""
    free = {(C | A | S, vk) for vk in (0x41, 0x42)} | {(C | A, 0x43), (C | A, 0x70), (WIN | C, 0x44),
                                                       (C | A, keynames.VK_SNAPSHOT)}

    def state(c: combos.Combo) -> str | None:
        return "free" if c in free else "used"

    recs = combos.recommend(state, ["Ctrl+Alt+Shift", "Ctrl+Alt", "Win+Ctrl"], 10)
    assert recs == [(C | A | S, 0x41), (C | A | S, 0x42), (C | A, 0x43)]
    assert combos.recommend(state, ["Ctrl+Alt+Shift", "Ctrl+Alt"], 2) == [(C | A | S, 0x41), (C | A | S, 0x42)]
