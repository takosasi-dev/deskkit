# zip の名前の読み直しと展開(AC-9〜AC-14・FR-17〜FR-24・M-7・M-10)。zip は生のバイト列から組み立てる。
from __future__ import annotations

import os
import stat
import unicodedata
import zipfile
from pathlib import Path

import pytest

from deskkit.modules.mojifix import zipextract as ZX
from deskkit.modules.mojifix import zipnames as Z
from deskkit.modules.mojifix.owned import Owned

from .conftest import raw_zip, unicode_extra


def _zip(tmp: Path, name: str, data: bytes) -> Path:
    p = tmp / name
    p.write_bytes(data)
    return p


def _extract(p: Path, key: str, tmp: Path, **kw: object) -> ZX.ExtractResult:
    return ZX.unpack(p, Z.load(p), key, Owned(), lambda: tmp / "Documents" / "MojiFix", **kw)  # type: ignore[arg-type]


def test_ac9_cp932_names(tmp_path: Path) -> None:
    names = ["表示/ソフト.txt", "能力.txt", "普通.txt"]  # 「表」「ソ」「能」は 2 バイト目が 0x5C
    p = _zip(tmp_path, "win.zip", raw_zip([(n.encode("cp932"), n.encode()) for n in names]))
    scan = Z.load(p)
    assert scan.needs_candidates
    assert [e.raw for e in scan.entries] == [n.encode("cp932") for n in names]  # M-7: 元のバイト列に戻る
    cands = Z.candidates(scan)
    sj = next(c for c in cands if c.key == "sjis")
    assert sj.sample == names and sj.bad_names == 0
    assert cands[0].key == "sjis"
    res = _extract(p, "sjis", tmp_path)
    assert res.dest == tmp_path / "win"
    assert (tmp_path / "win" / "表示" / "ソフト.txt").read_bytes() == "表示/ソフト.txt".encode()
    assert (tmp_path / "win" / "能力.txt").is_file() and res.extracted == 3
    assert not list((tmp_path / "win").rglob("*.part"))


def test_ac10_mac_nfd_utf8_without_flag(tmp_path: Path) -> None:
    nfd = unicodedata.normalize("NFD", "がぎ写真/ぱぴぷ.jpg")
    p = _zip(tmp_path, "mac.zip", raw_zip([(nfd.encode("utf-8"), b"x"), ("普通.txt".encode(), b"y")]))
    scan = Z.load(p)
    assert all(e.fixed is None for e in scan.entries)
    cands = Z.candidates(scan)
    assert cands[0].key == "utf8"
    res = _extract(p, "utf8", tmp_path)
    got = {q.relative_to(res.dest).as_posix() for q in res.dest.rglob("*") if q.is_file()}
    assert got == {"がぎ写真/ぱぴぷ.jpg", "普通.txt"}
    assert res.renamed == 1 and res.composed == 1


def test_compose_off_keeps_nfd(tmp_path: Path) -> None:
    nfd = unicodedata.normalize("NFD", "が.txt")
    p = _zip(tmp_path, "m.zip", raw_zip([(nfd.encode(), b"x")]))
    res = _extract(p, "utf8", tmp_path, compose_names=False)
    assert [q.name for q in res.dest.iterdir()] == [nfd]


@pytest.mark.parametrize("raw", [b"../x.txt", b"..\\x.txt", b"/abs.txt", b"\\abs.txt", b"C:\\x.txt", b"C:x.txt",
                                 b"a/../../x.txt", b"CON.txt", b"con", b"a/LPT1.log/b.txt", b"nul\x00.txt", b"aux .txt"])
def test_ac11_unsafe_names_are_skipped(tmp_path: Path, raw: bytes) -> None:
    p = _zip(tmp_path, "evil.zip", raw_zip([(raw, b"bad"), (b"ok.txt", b"ok")]))
    res = _extract(p, "utf8", tmp_path)
    assert res.extracted == 1 and res.skipped_total == 1
    files = [q for q in tmp_path.rglob("*") if q.is_file()]
    assert sorted(q.name for q in files) == ["evil.zip", "ok.txt"]


def test_ac11_symlink_entry(tmp_path: Path) -> None:
    mode = (stat.S_IFLNK | 0o777) << 16
    p = _zip(tmp_path, "link.zip", raw_zip([(b"link", b"/etc/passwd"), (b"ok.txt", b"ok")], create_system=3,
                                          external={b"link": mode}))
    items = Z.plan(Z.load(p), "utf8", tmp_path / "link")
    assert [(i.status, i.code) for i in items] == [("skip", "link"), ("extract", "")]
    res = _extract(p, "utf8", tmp_path)
    assert not (res.dest / "link").exists()


def test_sanitize_and_duplicates(tmp_path: Path) -> None:
    entries = [(b"a<b>.txt", b"1"), (b"dir./x.txt", b"2"), (b"Same.txt", b"3"), (b"same.txt", b"4"), (b"./c.txt", b"5"),
               (b"d/", b"")]
    p = _zip(tmp_path, "s.zip", raw_zip(entries))
    items = Z.plan(Z.load(p), "utf8", tmp_path / "s")
    rels = [i.rel for i in items]
    assert rels == ["a_b_.txt", "dir_\\x.txt", "Same.txt", "same (2).txt", "c.txt", "d"]
    assert items[3].status == "rename" and "番号" in items[3].reason
    res = _extract(p, "utf8", tmp_path)
    assert (res.dest / "same (2).txt").read_bytes() == b"4" and (res.dest / "d").is_dir()


def test_fr20_mac_files(tmp_path: Path) -> None:
    entries = [(b"__MACOSX/._a.txt", b"x"), (b"sub/.DS_Store", b"x"), (b"a.txt", b"a")]
    p = _zip(tmp_path, "m.zip", raw_zip(entries))
    items = Z.plan(Z.load(p), "utf8", tmp_path / "m")
    assert [i.status for i in items] == ["skip", "skip", "extract"]
    items2 = Z.plan(Z.load(p), "utf8", tmp_path / "m", skip_mac=False)
    assert [i.status for i in items2] == ["extract", "extract", "extract"]


def test_fr19_long_path(tmp_path: Path) -> None:
    name = ("d" * 100 + "/") * 2 + "f" * 80 + ".txt"
    p = _zip(tmp_path, "l.zip", raw_zip([(name.encode(), b"x")]))
    items = Z.plan(Z.load(p), "utf8", tmp_path / "l")
    assert items[0].status == "skip" and items[0].code == "long_path"


def test_unicode_path_extra(tmp_path: Path) -> None:
    raw = "表示.txt".encode("cp932")
    good = raw_zip([(raw, b"x")], extra={raw: unicode_extra(raw, "表示.txt")})
    scan = Z.load(_zip(tmp_path, "g.zip", good))
    assert scan.entries[0].fixed == "表示.txt" and not scan.needs_candidates
    bad = raw_zip([(raw, b"x")], extra={raw: unicode_extra(raw, "別名.txt", crc=0x12345678)})
    scan2 = Z.load(_zip(tmp_path, "b.zip", bad))
    assert scan2.entries[0].fixed is None and scan2.needs_candidates


def test_flagged_utf8_is_fixed(tmp_path: Path) -> None:
    p = _zip(tmp_path, "f.zip", raw_zip([("表示.txt".encode(), b"x")], flag=0x800))
    scan = Z.load(p)
    assert scan.entries[0].fixed == "表示.txt" and not scan.needs_candidates


def test_fr18_no_mojibake(tmp_path: Path) -> None:
    p = _zip(tmp_path, "a.zip", raw_zip([(b"a.txt", b"x"), (b"b/c.txt", b"y")]))
    assert not Z.load(p).needs_candidates


def test_fr17_errors(tmp_path: Path) -> None:
    cases = {
        "broken_zip": b"PK\x03\x04 not a zip",
        "bad_names": raw_zip([("表示.txt".encode("cp932"), b"x")], flag=0x800),
        "encrypted": raw_zip([(b"a.txt", b"xxxxxxxxxxxxxxxxxxxx")], flag=0x1),
        "unsupported_method": raw_zip([(b"a.txt", b"xxxx")], method=9),
        "zip_empty": raw_zip([]),
    }
    msgs = {"broken_zip": Z.MSG_BROKEN, "bad_names": Z.MSG_BAD_NAMES, "encrypted": Z.MSG_ENCRYPTED,
            "unsupported_method": Z.MSG_METHOD, "zip_empty": Z.MSG_EMPTY}
    for reason, data in cases.items():
        with pytest.raises(Z.ZipLoadError) as ei:
            Z.load(_zip(tmp_path, f"{reason}.zip", data))
        assert ei.value.reason == reason and ei.value.message == msgs[reason]


def test_fr17_too_many(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Z, "MAX_ENTRIES", 3)
    p = _zip(tmp_path, "many.zip", raw_zip([(f"{i}.txt".encode(), b"") for i in range(4)]))
    with pytest.raises(Z.ZipLoadError) as ei:
        Z.load(p)
    assert ei.value.reason == "too_many"


def test_ac12_bomb_declared_total(tmp_path: Path) -> None:
    p = _zip(tmp_path, "big.zip", raw_zip([(b"a.txt", b"x")], sizes={b"a.txt": (1, 3 * Z.GB)}))
    with pytest.raises(ZX.ExtractError) as ei:
        _extract(p, "utf8", tmp_path, max_total_gb=2)
    assert ei.value.reason == "bomb" and ei.value.message == Z.MSG_BOMB
    assert not (tmp_path / "big").exists()


def test_ac12_bomb_ratio(tmp_path: Path) -> None:
    data = b"\x00" * (2 * Z.MB)
    p = _zip(tmp_path, "r.zip", raw_zip([(b"z.bin", data)], method=8))
    assert Z.load(p).entries[0].file_size / Z.load(p).entries[0].compress_size > 1000
    with pytest.raises(ZX.ExtractError) as ei:
        _extract(p, "utf8", tmp_path)
    assert ei.value.reason == "bomb"
    assert not (tmp_path / "r").exists()


def test_ac12_small_ratio_entry_is_fine(tmp_path: Path) -> None:
    data = b"\x00" * (Z.MB - 1)  # 1MB 以下は倍率を問わない
    p = _zip(tmp_path, "s.zip", raw_zip([(b"z.bin", data)], method=8))
    res = _extract(p, "utf8", tmp_path)
    assert (res.dest / "z.bin").stat().st_size == Z.MB - 1


def test_ac12_encrypted_message(tmp_path: Path) -> None:
    with zipfile.ZipFile(tmp_path / "plain.zip", "w") as zf:
        zf.writestr("a.txt", "a")
    data = bytearray((tmp_path / "plain.zip").read_bytes())
    for sig in (b"PK\x03\x04", b"PK\x01\x02"):
        i = data.find(sig)
        off = 6 if sig == b"PK\x03\x04" else 8
        data[i + off] |= 1
    with pytest.raises(Z.ZipLoadError) as ei:
        Z.load(_zip(tmp_path, "enc.zip", bytes(data)))
    assert ei.value.message == "パスワード付きの zip は扱えません"


def test_disk_space(tmp_path: Path) -> None:
    p = _zip(tmp_path, "a.zip", raw_zip([(b"a.txt", b"x" * 1000)]))
    with pytest.raises(ZX.ExtractError) as ei:
        _extract(p, "utf8", tmp_path, free_bytes=lambda _p: 100)
    assert ei.value.reason == "disk_full" and "あと" in ei.value.message and "GB 要ります" in ei.value.message
    assert not (tmp_path / "a").exists()


def test_fr23_broken_entry_is_skipped(tmp_path: Path) -> None:
    p = _zip(tmp_path, "c.zip", raw_zip([(b"bad.txt", b"hello"), (b"ok.txt", b"fine")], crc_override={b"bad.txt": 1}))
    res = _extract(p, "utf8", tmp_path)
    assert res.extracted == 1 and res.skipped == {"壊れていた": 1}
    assert sorted(q.name for q in res.dest.iterdir()) == ["ok.txt"]


def test_ac14_cancel_removes_everything(tmp_path: Path) -> None:
    entries = [(f"d{i}/f{i}.bin".encode(), os.urandom(300_000)) for i in range(6)]
    p = _zip(tmp_path, "c.zip", raw_zip(entries))
    n = {"i": 0}

    def cancel() -> bool:
        n["i"] += 1
        return n["i"] > 5

    with pytest.raises(ZX.CancelledError):
        _extract(p, "utf8", tmp_path, cancel=cancel)
    assert sorted(q.name for q in tmp_path.iterdir()) == ["c.zip"]


def test_dest_numbered(tmp_path: Path) -> None:
    p = _zip(tmp_path, "n.zip", raw_zip([(b"a.txt", b"x")]))
    (tmp_path / "n").mkdir()
    (tmp_path / "n" / "keep.txt").write_bytes(b"k")
    res = _extract(p, "utf8", tmp_path)
    assert res.dest == tmp_path / "n (2)" and (tmp_path / "n" / "keep.txt").read_bytes() == b"k"


def test_mtime_from_zip(tmp_path: Path) -> None:
    p = _zip(tmp_path, "t.zip", raw_zip([(b"a.txt", b"x")]))
    res = _extract(p, "utf8", tmp_path)
    import time

    lt = time.localtime((res.dest / "a.txt").stat().st_mtime)
    assert (lt.tm_year, lt.tm_mon, lt.tm_mday) == (1980, 1, 1)


def test_changed_zip_is_refused(tmp_path: Path) -> None:
    p = _zip(tmp_path, "c.zip", raw_zip([(b"a.txt", b"x")]))
    scan = Z.load(p)
    p.write_bytes(raw_zip([(b"a.txt", b"xy")]))
    with pytest.raises(ZX.ExtractError) as ei:
        ZX.unpack(p, scan, "utf8", Owned(), lambda: tmp_path)
    assert ei.value.reason == "changed"


@pytest.mark.win32_real
def test_ac13_zone_identifier(tmp_path: Path) -> None:
    p = _zip(tmp_path, "dl.zip", raw_zip([(b"a.txt", b"x"), (b"d/b.txt", b"y")]))
    zone = b"[ZoneTransfer]\r\nZoneId=3\r\nHostUrl=about:internet\r\n"
    with open(str(p) + ":Zone.Identifier", "wb") as f:
        f.write(zone)
    res = _extract(p, "utf8", tmp_path)
    for q in (res.dest / "a.txt", res.dest / "d" / "b.txt"):
        with open(str(q) + ":Zone.Identifier", "rb") as f:
            assert f.read() == zone
    assert not res.zone_failed


def test_zone_read_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "z.zip"
    p.write_bytes(b"x")
    try:
        with open(str(p) + ":Zone.Identifier", "wb") as f:
            f.write(b"x" * (ZX.ZONE_MAX + 1))
    except OSError:
        pytest.skip("この場所は代替データストリームを持てない")
    assert ZX.read_zone(p) is None
