# MojiFix の自己検査(AC-20)。一時フォルダの中だけで、読み方の候補の順・書き出しと確かめ・zip の名前の読み直しと展開・
# 安全の規則・濁点のくっつけと名前の変更と取り消し・操作記録に名前が出ないことを確かめる。利用者のファイルには触れない。
# run() は 0=合格 / 1=不合格。
from __future__ import annotations

import hashlib
import io
import struct
import tempfile
import unicodedata
import zlib
from pathlib import Path


class _Result:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, name: str, ok: bool) -> None:
        print(f"  [{'OK' if ok else 'NG'}] {name}")
        if not ok:
            self.failed += 1


def raw_zip(entries: list[tuple[bytes, bytes]], flag: int = 0) -> bytes:
    """名前を生のバイト列のまま入れた zip(無圧縮)を作る。zipfile で作ると UTF-8 の印が付くので自前で組む。"""
    buf = io.BytesIO()
    cd = io.BytesIO()
    for raw, data in entries:
        crc = zlib.crc32(data) & 0xFFFFFFFF
        off = buf.tell()
        buf.write(struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, flag, 0, 0, 0x21, crc, len(data), len(data), len(raw), 0))
        buf.write(raw)
        buf.write(data)
        cd.write(struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, flag, 0, 0, 0x21, crc, len(data), len(data),
                             len(raw), 0, 0, 0, 0, 0, off))
        cd.write(raw)
    cdb = cd.getvalue()
    start = buf.tell()
    buf.write(cdb)
    buf.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(entries), len(entries), len(cdb), start, 0))
    return buf.getvalue()


def _checks(r: _Result, tmp: Path) -> None:
    from deskkit.modules.mojifix import renamer as R
    from deskkit.modules.mojifix import sniff as S
    from deskkit.modules.mojifix import textfix as TF
    from deskkit.modules.mojifix import zipextract as ZX
    from deskkit.modules.mojifix import zipnames as Z
    from deskkit.modules.mojifix.compose import compose
    from deskkit.modules.mojifix.oplog import OpsLog
    from deskkit.modules.mojifix.owned import Owned

    text = "氏名,住所,電話番号\r\n山田太郎,東京都千代田区一丁目,03-1234-5678\r\n佐藤花子,大阪府大阪市北区,06-9876-5432\r\n"
    tops = {}
    for codec, key in (("utf-8", "utf8"), ("cp932", "sjis"), ("euc_jp", "eucjp"), ("iso2022_jp", "jis"),
                       ("utf-16-le", "u16le"), ("utf-16-be", "u16be")):
        b = text.encode(codec)
        tops[key] = S.sniff_bytes(b, len(b), 0).candidates[0].key
    r.check("候補の1番目が、その形になる(6 通り)", all(k == v for k, v in tops.items()))
    png = b"\x89PNG\r\n\x1a\n" + bytes(64)
    r.check("PNG は「文字のファイルではない」", S.sniff_bytes(png, len(png), 0).kind == "binary")

    secret = "SELFTEST-住所録-7f3"
    src = tmp / f"{secret}.csv"
    src.write_bytes(text.encode("cp932"))
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    opts = TF.Options("sjis", "excel")
    chk = TF.check(src, opts)
    w = TF.write(src, chk, opts, Owned(), lambda: tmp / "fallback")
    out = w.path.read_bytes()
    r.check("Excel の形: EF BB BF で始まり、元を cp932 で読んだ文字列と同じ",
            out.startswith(b"\xef\xbb\xbf") and out.decode("utf-8-sig") == text and w.path.name == f"{secret}_文字直し.csv")
    r.check("元のファイルは1バイトも変わらない", hashlib.sha256(src.read_bytes()).hexdigest() == before)
    bad = tmp / "bad.txt"
    bad.write_bytes("a—b\n😀\n".encode())
    chk2 = TF.check(bad, TF.Options("utf8", "sjis"))
    r.check("古いソフト用で書けない文字の行が出る", [x.line_no for x in chk2.unencodable_lines] == [1, 2])

    names = ["表示/ソフト.txt", "がぎ写真.jpg"]
    zp = tmp / "cp932.zip"
    zp.write_bytes(raw_zip([(n.encode("cp932"), b"hello") for n in names]))
    scan = Z.load(zp)
    cands = Z.candidates(scan)
    sj = next(c for c in cands if c.key == "sjis")
    r.check("zip: Shift_JIS の候補が元の名前と一致する", sj.sample == names and sj.bad_names == 0)
    res = ZX.unpack(zp, scan, "sjis", Owned(), lambda: tmp / "fallback")
    r.check("zip: 表示\\ソフト.txt ができる", (res.dest / "表示" / "ソフト.txt").read_bytes() == b"hello" and res.extracted == 2)
    evil = tmp / "evil.zip"
    evil.write_bytes(raw_zip([(b"../x.txt", b"1"), (b"..\\x.txt", b"2"), (b"/abs.txt", b"3"), (b"C:\\x.txt", b"4"),
                              (b"a/../../x.txt", b"5"), (b"CON.txt", b"6"), (b"nul\x00.txt", b"7")]))
    items = Z.plan(Z.load(evil), "utf8", tmp / "evil")
    r.check("zip: 外へ出る名前・予約名・NUL は全部「飛ばす」", all(it.status == "skip" for it in items))

    nfd = unicodedata.normalize("NFD", "がぱ")
    r.check("くっつける: 濁点はくっつき、「神」U+FA19 は変わらない", compose("\ufa19" + nfd) == "\ufa19がぱ")
    d = tmp / "names"
    (d / unicodedata.normalize("NFD", "ぶどう")).mkdir(parents=True)
    (d / unicodedata.normalize("NFD", "ぶどう") / (nfd + ".txt")).write_text("x", encoding="utf-8")
    sc = R.scan(d)
    rr = R.rename_items(sc.items)
    ok_after = {p.name for p in (d / "ぶどう").iterdir()} == {"がぱ.txt"}
    R.undo(rr.done)
    back = {p.name for p in d.iterdir()} == {unicodedata.normalize("NFD", "ぶどう")}
    r.check("名前の濁点: 入れ子を直して、元に戻せる", rr.renamed == 2 and ok_after and back)

    ops = OpsLog(tmp / "ops.jsonl")
    ops.write(op="text", result="ok", codec_in="cp932", codec_out="utf-8-sig", in_bytes=10, out_bytes=12, lines=3, files=1)
    texts = (tmp / "ops.jsonl").read_text(encoding="utf-8")
    r.check("操作記録にファイル名・中身が出ない", secret not in texts and "山田" not in texts and '"result": "ok"' in texts)


def run() -> int:
    r = _Result()
    print("MojiFix 自己検査")
    try:
        with tempfile.TemporaryDirectory(prefix="mojifix-selftest-") as td:
            _checks(r, Path(td))
    except Exception as e:  # noqa: BLE001 - 自己検査は例外も不合格として返す
        print(f"  [NG] 例外: {type(e).__name__}")
        r.failed += 1
    print("合格" if r.failed == 0 else f"不合格({r.failed} 件)")
    return 0 if r.failed == 0 else 1
