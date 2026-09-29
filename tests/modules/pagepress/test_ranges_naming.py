# 範囲の書き方(FR-6・AC-5)・出力名(P-12)・pending.json(FR-21)・設定の正規化(§9)・操作記録の形(§9)。
from __future__ import annotations

from pathlib import Path

import pytest

from deskkit.modules.pagepress import config as cfgmod
from deskkit.modules.pagepress import naming, ranges
from deskkit.modules.pagepress.oplog import OpsLog, compressed, made_pdfs
from deskkit.modules.pagepress.ranges import RangeError, Span


def test_ranges_basic_ac5() -> None:
    spans = ranges.parse("1-3, 5, 8-", 10)
    assert spans == [Span(1, 3), Span(5, 5), Span(8, None)]
    assert [s.resolve(10) for s in spans] == [(1, 3), (5, 5), (8, 10)]
    assert ranges.parse("１－３", 10) == ranges.parse("1-3", 10)
    assert ranges.parse("-3、5〜6,7ー8, 9~", 10) == [Span(1, 3), Span(5, 6), Span(7, 8), Span(9, None)]


@pytest.mark.parametrize(("text", "msg"), [
    ("5-3", "5-3 の順が逆です"),
    ("0", ranges.MSG_ZERO),
    ("11", "10 ページまでしかありません"),
    ("3-11", "10 ページまでしかありません"),
    ("あ", ranges.MSG_NOT_NUMBER),
    ("1-2-3", ranges.MSG_NOT_NUMBER),
    ("-", ranges.MSG_NOT_NUMBER),
    ("", ranges.MSG_EMPTY),
    (" , ", ranges.MSG_EMPTY),
])
def test_ranges_errors(text: str, msg: str) -> None:
    with pytest.raises(RangeError) as ei:
        ranges.parse(text, 10)
    assert ei.value.message == msg


def test_ranges_overlap_and_resolve() -> None:
    spans = ranges.parse("1-3,2-4")  # 同じページが2つの範囲に入ってよい
    assert [s.resolve(5) for s in spans] == [(1, 3), (2, 4)]
    with pytest.raises(RangeError) as ei:
        Span(8, None).resolve(5)
    assert ei.value.message == ranges.MSG_OVER_ALL


def test_every_single_ac6() -> None:
    assert ranges.single(3) == [(1, 1), (2, 2), (3, 3)]
    assert ranges.every(10, 3) == [(1, 3), (4, 6), (7, 9), (10, 10)]
    assert ranges.count_every(10, 3) == 4


def test_names_unique_and_exhausted(tmp_path: Path) -> None:
    for i in range(3):
        t = tmp_path / naming.tmp_name()
        t.write_bytes(b"x")
        p = naming.rename_unique(t, tmp_path, "a_まとめ", ".pdf")
        assert p.name == ("a_まとめ.pdf" if i == 0 else f"a_まとめ ({i + 1}).pdf")
    (tmp_path / "b.pdf").write_bytes(b"x")
    for n in range(2, 1001):
        (tmp_path / f"b ({n}).pdf").write_bytes(b"x")
    t = tmp_path / naming.tmp_name()
    t.write_bytes(b"x")
    with pytest.raises(naming.NamesExhaustedError):
        naming.rename_unique(t, tmp_path, "b", ".pdf")


def test_tmp_name_shape() -> None:
    n = naming.tmp_name()
    assert n.startswith("~pagepress-") and n.endswith(".tmp") and len(n) == len("~pagepress-") + 16 + 4
    assert naming.is_own_tmp(Path(n)) and not naming.is_own_tmp(Path("user.tmp"))


def test_fit_stem() -> None:
    folder = Path("C:/" + "d" * 100)
    stem = naming.fit_stem(folder, "x" * 300, "_まとめ.pdf")
    assert stem is not None and len(str(folder / (stem + " (1000)_まとめ.pdf"))) <= 259
    assert naming.fit_stem(Path("C:/" + "d" * 260), "x", ".pdf") is None


def test_pending_sweep_only_own_tmp_ac12(tmp_path: Path) -> None:
    own = tmp_path / naming.tmp_name()
    own.write_bytes(b"x")
    user = tmp_path / "利用者の書類.pdf"
    user.write_bytes(b"keep")
    user_tmp = tmp_path / "other.tmp"
    user_tmp.write_bytes(b"keep")
    p = naming.Pending(tmp_path / "pending.json")
    p.add(own)
    p.add(user)
    p.add(user_tmp)
    assert p.count() == 3
    assert p.sweep() == 1
    assert not own.exists() and user.exists() and user_tmp.exists()
    assert p.count() == 0


def test_config_normalize() -> None:
    s, changed = cfgmod.normalize({})
    assert changed and s == cfgmod.DEFAULTS
    s, changed = cfgmod.normalize({**cfgmod.DEFAULTS, "margin_mm": 5, "split_every": 0, "paper": "a3", "x": 1,
                                   "sendto_enabled": "yes"})
    assert changed and s["margin_mm"] == 10 and s["split_every"] == 10 and s["paper"] == "a4" and "x" not in s
    assert s["sendto_enabled"] is False
    s, changed = cfgmod.normalize({**cfgmod.DEFAULTS, "split_every": 999, "margin_mm": 0, "enabled": True})
    assert not changed and s["split_every"] == 999


def test_oplog_shape(tmp_path: Path) -> None:
    ops = OpsLog(tmp_path / "ops.jsonl")
    ops.write(op="merge", result="ok", inputs=2, images=1, pages=5, outputs=1, in_bytes=10, out_bytes=8, level=None, ms=3,
              code=None)
    ops.write(op="compress", result="ok", inputs=2, images=0, pages=5, outputs=2, in_bytes=10, out_bytes=8,
              level="normal", ms=3, code=None)
    ops.write(op="split", result="error", inputs=1, images=0, pages=0, outputs=0, in_bytes=10, out_bytes=None, level=None,
              ms=3, code="range_out")
    rows = ops.read()
    assert [r["op"] for r in rows] == ["merge", "compress", "split"]
    assert set(rows[0]) == {"ts", "op", "result", "inputs", "images", "pages", "outputs", "in_bytes", "out_bytes", "level",
                            "ms", "code"}
    assert ops.per_day(3, made_pdfs)[-1] == 3
    assert ops.per_day(3, compressed)[-1] == 2
    with pytest.raises(ValueError):
        ops.write(op="merge", result="ok", inputs=1, images=0, pages=1, outputs=1, in_bytes=1, out_bytes=1, level=None,
                  ms=1, code="C:\\secret")
