# 確認の読みと書き出し(AC-3〜AC-7・FR-11〜FR-16・FR-4・M-16)。1MB の区切りをまたぐ検体も作る。
from __future__ import annotations

import hashlib
import unicodedata
from pathlib import Path

import pytest

from deskkit.modules.mojifix import textfix as TF
from deskkit.modules.mojifix.owned import Owned

from .conftest import ADDRESS


def _write(p: Path, opts: TF.Options, tmp: Path, **kw: object) -> TF.WriteResult:
    chk = TF.check(p, opts)
    return TF.write(p, chk, opts, Owned(), lambda: tmp / "Documents" / "MojiFix", **kw)  # type: ignore[arg-type]


def test_ac3_sjis_to_excel(tmp_path: Path) -> None:
    src = tmp_path / "売上.csv"
    src.write_bytes(ADDRESS.encode("cp932"))
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    w = _write(src, TF.Options("sjis", "excel"), tmp_path)
    assert w.path == tmp_path / "売上_文字直し.csv"   # Q-3 の回答の名前
    out = w.path.read_bytes()
    assert out.startswith(b"\xef\xbb\xbf") and out.count(b"\xef\xbb\xbf") == 1
    assert out.decode("utf-8-sig") == src.read_bytes().decode("cp932")
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before
    assert w.lines == 3 and w.chars == len(ADDRESS)


def test_names_numbered(tmp_path: Path) -> None:
    src = tmp_path / "a.csv"
    src.write_bytes(ADDRESS.encode("cp932"))
    (tmp_path / "a_文字直し.csv").write_bytes(b"keep")
    w = _write(src, TF.Options("sjis", "excel"), tmp_path)
    assert w.path.name == "a_文字直し (2).csv"
    assert (tmp_path / "a_文字直し.csv").read_bytes() == b"keep"


def test_ac4_unencodable_stops_before_writing(tmp_path: Path) -> None:
    src = tmp_path / "e.txt"
    src.write_bytes("一行目 — ダッシュ\n二行目\n三行目 😀 絵文字\n".encode())
    chk = TF.check(src, TF.Options("utf8", "sjis"))
    assert chk.unencodable == 2
    assert [x.line_no for x in chk.unencodable_lines] == [1, 3]
    seg = chk.unencodable_lines[0].segments
    assert ("—", True) in seg
    assert not list(tmp_path.glob("*_文字直し*"))  # 確認だけでは何も作らない


def test_unencodable_geta(tmp_path: Path) -> None:
    src = tmp_path / "e.txt"
    src.write_bytes("a—b😀c\n".encode())
    chk = TF.check(src, TF.Options("utf8", "sjis"))
    opts = TF.Options("utf8", "sjis", geta_unencodable=True)
    w = TF.write(src, chk, opts, Owned(), lambda: tmp_path)
    assert w.path.read_bytes().decode("cp932") == "a〓b〓c\n" and w.replaced == 2


def test_ac5_formulas_are_kept(tmp_path: Path) -> None:
    body = '=HYPERLINK("x"),+1,-1,@SUM(A1)\r\n"=1+2",名前\r\n'
    src = tmp_path / "f.csv"
    src.write_bytes(body.encode("cp932"))
    w = _write(src, TF.Options("sjis", "utf8"), tmp_path)
    assert w.path.read_bytes().decode("utf-8") == body


def test_ac6_newlines_keep_and_crlf_across_chunk(tmp_path: Path) -> None:
    head = b"a" * (TF.CHUNK - 1) + b"\r"   # 1MB の区切りの最後に CR
    tail = b"\nx\ny\r\nz\rw"
    src = tmp_path / "n.txt"
    src.write_bytes(head + tail + "あ".encode())
    keep = _write(src, TF.Options("utf8", "utf8", "keep"), tmp_path)
    assert keep.path.read_bytes() == src.read_bytes()  # 印なし→印なしでも、形の選択が同じでなければ書く
    crlf = _write(src, TF.Options("utf8", "utf8", "crlf"), tmp_path)
    data = crlf.path.read_bytes()
    assert b"\r\r\n" not in data
    assert data.endswith(b"\r\nx\r\ny\r\nz\r\nw" + "あ".encode())
    assert data.count(b"\r\n") == 4 and data.replace(b"\r\n", b"").count(b"\r") == 0 and b"\n" not in data.replace(b"\r\n", b"")
    lf = _write(src, TF.Options("utf8", "utf8", "lf"), tmp_path)
    assert b"\r" not in lf.path.read_bytes() and lf.lines == 5


def test_ac7_mixed_stops_with_line(tmp_path: Path) -> None:
    body = "".join(f"{i}行目,UTF-8\n" for i in range(1, 11)).encode("utf-8")
    body += "".join(f"{i}行目,Shift_JIS\n" for i in range(11, 16)).encode("cp932")
    src = tmp_path / "m.txt"
    src.write_bytes(body)
    chk = TF.check(src, TF.Options("utf8", "excel"))
    assert chk.undecodable > 0 and chk.first_undecodable_line == 11
    assert [x.line_no for x in chk.undecodable_lines] == [11, 12, 13, 14, 15]
    assert any(hit and t == "�" for t, hit in chk.undecodable_lines[0].segments)


def test_geta_one_per_run(tmp_path: Path) -> None:
    src = tmp_path / "g.txt"
    src.write_bytes(b"ok" + bytes([0xFF, 0xFE, 0xFD]) + b"mid" + bytes([0xC3]) + b"end")
    chk = TF.check(src, TF.Options("utf8", "utf8"))
    assert chk.undecodable == 2
    w = TF.write(src, chk, TF.Options("utf8", "utf8", geta_undecodable=True), Owned(), lambda: tmp_path)
    assert w.path.read_bytes().decode() == "ok〓mid〓end" and w.replaced == 2


def test_issue_lines_are_limited_and_cut(tmp_path: Path) -> None:
    long_line = "x" * 300 + "—" + "y" * 300
    src = tmp_path / "l.txt"
    src.write_bytes(("\n".join([long_line] * 30) + "\n").encode())
    chk = TF.check(src, TF.Options("utf8", "sjis"))
    assert chk.unencodable == 30 and len(chk.unencodable_lines) == TF.MAX_ISSUE_LINES
    text = "".join(t for t, _h in chk.unencodable_lines[0].segments)
    assert text == "…" + "x" * 60 + "—" + "y" * 60 + "…"


def test_fr14_unchanged(tmp_path: Path) -> None:
    src = tmp_path / "u.csv"
    src.write_bytes(b"\xef\xbb\xbf" + ADDRESS.encode())
    chk = TF.check(src, TF.Options("utf8", "excel"))
    assert chk.unchanged()
    assert not chk.unchanged(TF.Options("utf8", "utf8"))
    chk2 = TF.check(src, TF.Options("utf8", "excel", "lf"))
    assert chk2.newline_changes == 3 and not chk2.unchanged()
    sj = tmp_path / "s.csv"
    sj.write_bytes(ADDRESS.encode("cp932"))
    assert TF.check(sj, TF.Options("sjis", "sjis")).unchanged()


def test_compose_in_text(tmp_path: Path) -> None:
    nfd = unicodedata.normalize("NFD", "がぎぐ,ぱ\n")
    src = tmp_path / "c.txt"
    src.write_bytes(("神" + nfd).encode())
    chk = TF.check(src, TF.Options("utf8", "utf8", compose=True))
    assert chk.composed == 4
    w = TF.write(src, chk, TF.Options("utf8", "utf8", compose=True), Owned(), lambda: tmp_path)
    assert w.path.read_bytes().decode() == "神がぎぐ,ぱ\n"


def test_fr15_changed_after_load(tmp_path: Path) -> None:
    src = tmp_path / "c.csv"
    src.write_bytes(ADDRESS.encode("cp932"))
    opts = TF.Options("sjis", "excel")
    chk = TF.check(src, opts)
    src.write_bytes(ADDRESS.encode("cp932") + b"more")
    with pytest.raises(TF.TextError) as ei:
        TF.write(src, chk, opts, Owned(), lambda: tmp_path)
    assert ei.value.reason == "changed" and ei.value.message == TF.MSG_CHANGED
    assert not list(tmp_path.glob("*_文字直し*"))


def test_fr16_verify_failure_removes_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "v.csv"
    src.write_bytes(ADDRESS.encode("cp932"))
    opts = TF.Options("sjis", "excel")
    chk = TF.check(src, opts)
    chk.out_chars += 1  # 数えた文字数と合わない
    with pytest.raises(TF.TextError) as ei:
        TF.write(src, chk, opts, Owned(), lambda: tmp_path)
    assert ei.value.reason == "verify_failed"
    assert not list(tmp_path.glob("*_文字直し*"))


def test_fr4_cancel_removes_output(tmp_path: Path) -> None:
    src = tmp_path / "big.txt"
    src.write_bytes(("あいうえお\n" * 400_000).encode("cp932"))
    opts = TF.Options("sjis", "excel")
    chk = TF.check(src, opts)
    calls = {"n": 0}

    def cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    with pytest.raises(TF.CancelledError):
        TF.write(src, chk, opts, Owned(), lambda: tmp_path, cancel=cancel)
    assert not list(tmp_path.glob("*_文字直し*"))


def test_disk_space_check(tmp_path: Path) -> None:
    src = tmp_path / "d.csv"
    src.write_bytes(ADDRESS.encode("cp932"))
    opts = TF.Options("sjis", "excel")
    chk = TF.check(src, opts)
    with pytest.raises(TF.TextError) as ei:
        TF.write(src, chk, opts, Owned(), lambda: tmp_path, free_bytes=lambda _p: 1000)
    assert ei.value.reason == "disk_full"
    assert not list(tmp_path.glob("*_文字直し*"))


def test_fallback_when_folder_is_not_writable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src_dir = tmp_path / "ro"
    src_dir.mkdir()
    src = src_dir / "r.csv"
    src.write_bytes(ADDRESS.encode("cp932"))
    real_open = open

    def fake_open(file: object, mode: str = "r", *a: object, **k: object) -> object:
        if mode == "xb" and Path(str(file)).parent == src_dir:
            raise PermissionError(13, "denied")
        return real_open(file, mode, *a, **k)  # type: ignore[call-overload]

    monkeypatch.setattr("builtins.open", fake_open)
    opts = TF.Options("sjis", "excel")
    chk = TF.check(src, opts)
    fb = tmp_path / "Documents" / "MojiFix"
    w = TF.write(src, chk, opts, Owned(), lambda: fb)
    assert w.fallback and w.path.parent == fb


def test_utf16_odd_length_is_undecodable(tmp_path: Path) -> None:
    src = tmp_path / "o.txt"
    src.write_bytes("あい".encode("utf-16-le") + b"\x41")
    chk = TF.check(src, TF.Options("u16le", "excel"))
    assert chk.undecodable == 1


def test_bom_removed_for_non_utf8(tmp_path: Path) -> None:
    src = tmp_path / "b.txt"
    src.write_bytes(b"\xff\xfe" + "表,裏\r\n".encode("utf-16-le"))
    w = _write(src, TF.Options("u16le", "utf8"), tmp_path)
    assert w.path.read_bytes() == "表,裏\r\n".encode()
