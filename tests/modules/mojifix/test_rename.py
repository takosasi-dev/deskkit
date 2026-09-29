# 名前の濁点(AC-15・AC-16・FR-25〜FR-30・M-13)。名前の変更は一時フォルダの中だけで試す。
from __future__ import annotations

import ctypes
import os
import unicodedata
from pathlib import Path

import pytest

from deskkit.modules.mojifix import renamer as R
from deskkit.modules.mojifix.compose import compose, separated_positions, singletons


def nfd(s: str) -> str:
    return unicodedata.normalize("NFD", s)


def test_compose_rules() -> None:
    assert len(singletons()) == 1035
    assert compose(nfd("がぱ")) == "がぱ"
    assert compose("\ufa19" + nfd("が")) == "\ufa19が"          # AC-16: 互換漢字はそのまま
    assert compose("か\u309b") == "か\u309b"                       # 分かれていない濁点の記号はくっつけない
    assert compose(nfd("café")) == "café"
    assert compose("abc") == "abc"
    assert separated_positions(nfd("がa")) == [1]


def _tree(root: Path) -> None:
    d = root / nfd("ぶどう")
    (d / nfd("ざる")).mkdir(parents=True)
    (d / (nfd("が") + ".txt")).write_text("1", encoding="utf-8")
    (d / nfd("ざる") / (nfd("ぴ") + ".txt")).write_text("2", encoding="utf-8")
    (root / nfd("げ.txt")).write_text("3", encoding="utf-8")
    (root / "げ.txt").write_text("nfc", encoding="utf-8")  # NFC の同名がある(P-11)


def _names(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def test_ac15_rename_nested_and_undo(tmp_path: Path) -> None:
    _tree(tmp_path)
    before = _names(tmp_path)
    sc = R.scan(tmp_path)
    by = {x.old: x for x in sc.items}
    assert by[nfd("げ.txt")].status == R.ST_EXISTS
    assert sum(1 for x in sc.items if x.fixable) == 4
    r = R.rename_items(sc.items)
    assert r.renamed == 4 and r.failed == 0
    after = _names(tmp_path)
    assert {"ぶどう", "ぶどう\\ざる", "ぶどう\\が.txt", "ぶどう\\ざる\\ぴ.txt", "げ.txt", nfd("げ.txt")} == after
    assert (tmp_path / nfd("げ.txt")).read_text(encoding="utf-8") == "3"  # 同名があるものは変わらない
    u = R.undo(r.done)
    assert u.renamed == 4
    assert _names(tmp_path) == before  # コードポイント列まで元に戻る


def test_ac16_compat_kanji_name(tmp_path: Path) -> None:
    name = "\ufa19" + nfd("社がぱ.txt")
    (tmp_path / name).write_text("x", encoding="utf-8")
    sc = R.scan(tmp_path)
    assert [x.new for x in sc.items] == ["\ufa19社がぱ.txt"]
    R.rename_items(sc.items)
    assert [p.name for p in tmp_path.iterdir()] == ["\ufa19社がぱ.txt"]


def test_clash_between_fixes(tmp_path: Path) -> None:
    # 並びの違う2つの分かれた形は、どちらも同じ「ậ」になる(NTFS では別の名前として並ぶ)
    one = "ậ.txt"
    two = "ậ.txt"
    (tmp_path / one).write_text("1", encoding="utf-8")
    (tmp_path / two).write_text("2", encoding="utf-8")
    (tmp_path / nfd("が.txt")).write_text("3", encoding="utf-8")
    sc = R.scan(tmp_path)
    st = {x.old: x.status for x in sc.items}
    assert st == {one: R.ST_CLASH, two: R.ST_CLASH, nfd("が.txt"): R.ST_OK}
    r = R.rename_items(sc.items)
    assert r.renamed == 1
    assert {p.name for p in tmp_path.iterdir()} == {one, two, "が.txt"}


def test_not_recursive(tmp_path: Path) -> None:
    _tree(tmp_path)
    sc = R.scan(tmp_path, recursive=False)
    assert {x.old for x in sc.items} == {nfd("ぶどう"), nfd("げ.txt")}


def test_too_many_items(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R, "MAX_ITEMS", 3)
    for i in range(4):
        (tmp_path / f"{i}.txt").write_text("x")
    with pytest.raises(R.ScanError) as ei:
        R.scan(tmp_path)
    assert ei.value.reason == "too_many_items" and ei.value.message == R.MSG_TOO_MANY


def test_system_and_reparse_are_skipped(tmp_path: Path) -> None:
    sysdir = tmp_path / nfd("しすてむ")
    sysdir.mkdir()
    (sysdir / nfd("が.txt")).write_text("x", encoding="utf-8")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    k32.SetFileAttributesW.restype = ctypes.c_int
    assert k32.SetFileAttributesW(str(sysdir), 0x10 | 0x4)  # DIRECTORY | SYSTEM
    try:
        link = tmp_path / nfd("りんく")
        try:
            os.symlink(tmp_path, link, target_is_directory=True)
        except OSError:
            link = None  # type: ignore[assignment]
        sc = R.scan(tmp_path)
        assert sc.items == []
    finally:
        k32.SetFileAttributesW(str(sysdir), 0x10)


def test_forbidden_and_root() -> None:
    env = {"WINDIR": r"C:\Windows", "ProgramFiles": r"C:\Program Files", "APPDATA": r"C:\Users\u\AppData\Roaming",
           "LOCALAPPDATA": r"C:\Users\u\AppData\Local", "ProgramData": r"C:\ProgramData", "ProgramFiles(x86)": r"C:\Program Files (x86)"}
    assert R.is_forbidden(Path(r"C:\Windows"), env)
    assert R.is_forbidden(Path(r"C:\Windows\System32"), env)
    assert R.is_forbidden(Path(r"c:\program files\x"), env)
    assert R.is_forbidden(Path(r"C:\Users\u\AppData\Local\Temp"), env)
    assert not R.is_forbidden(Path(r"C:\Windowsold"), env)
    assert not R.is_forbidden(Path(r"C:\Users\u\Documents"), env)
    assert R.is_drive_root(Path("C:\\")) and not R.is_drive_root(Path(r"C:\Users"))


def test_fr29_reasons(tmp_path: Path) -> None:
    (tmp_path / nfd("が.txt")).write_text("x", encoding="utf-8")
    sc = R.scan(tmp_path)

    def busy(_a: Path, _b: Path) -> None:
        e = OSError("x")
        e.winerror = 32  # type: ignore[attr-defined]
        raise e

    r = R.rename_items(sc.items, rename=busy)
    assert r.failed == 1 and sc.items[0].result == "ほかのソフトが使っています"
    for code, text in ((5, "変える権限がありません"), (2, "見つかりません"), (3, "見つかりません"),
                       (183, "同じ名前がすでにあります"), (999, "変えられませんでした")):
        e = OSError("x")
        e.winerror = code  # type: ignore[attr-defined]
        assert R.winerror_text(e) == text


def test_rename_checks_before_each(tmp_path: Path) -> None:
    (tmp_path / nfd("が.txt")).write_text("x", encoding="utf-8")
    sc = R.scan(tmp_path)
    (tmp_path / "が.txt").write_text("late", encoding="utf-8")  # 調べたあとに同名ができた
    r = R.rename_items(sc.items)
    assert r.renamed == 0 and sc.items[0].result == R.ST_EXISTS
    assert (tmp_path / "が.txt").read_text(encoding="utf-8") == "late"


def test_undo_skips_when_old_name_came_back(tmp_path: Path) -> None:
    (tmp_path / nfd("が.txt")).write_text("x", encoding="utf-8")
    sc = R.scan(tmp_path)
    r = R.rename_items(sc.items)
    (tmp_path / nfd("が.txt")).write_text("new", encoding="utf-8")
    u = R.undo(r.done)
    assert u.renamed == 0 and u.skipped == 1


def test_cancel_stops_after_current(tmp_path: Path) -> None:
    for s in ("が", "ぎ", "ぐ"):
        (tmp_path / (nfd(s) + ".txt")).write_text("x", encoding="utf-8")
    sc = R.scan(tmp_path)
    n = {"i": 0}

    def cancel() -> bool:
        n["i"] += 1
        return n["i"] > 1

    r = R.rename_items(sc.items, cancel)
    assert r.renamed == 1


def test_exists_is_case_insensitive(tmp_path: Path) -> None:
    (tmp_path / nfd("éa.txt")).write_text("1", encoding="utf-8")
    (tmp_path / "ÉA.txt").write_text("2", encoding="utf-8")
    sc = R.scan(tmp_path)
    assert [x.status for x in sc.items] == [R.ST_EXISTS]
