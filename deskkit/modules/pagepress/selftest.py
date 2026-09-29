# PagePress の自己検査(AC-22)。一時フォルダに検体を作り、まとめる・分ける・整理・軽くする・掃除と検査・画像のページ化・
# 暗号化の拒否・PDFium での描画(描画スレッドの中)・操作記録にファイル名が出ないことを確かめる。利用者のファイルには触れない。
# run() は 0=合格 / 1=不合格。
from __future__ import annotations

import io
import struct
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any


class _Result:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, name: str, ok: bool) -> None:
        print(f"  [{'OK' if ok else 'NG'}] {name}")
        if not ok:
            self.failed += 1


def _jpeg(w: int, h: int, orientation: int | None = None, noise: bool = False) -> bytes:
    from PIL import Image

    im = Image.effect_noise((w, h), 60).convert("RGB") if noise else Image.new("RGB", (w, h), (200, 200, 200))
    kw: dict[str, Any] = {"quality": 90}
    if orientation is not None:
        tiff = b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", 2)
        tiff += struct.pack("<HHI", 0x0112, 3, 1) + struct.pack("<HH", orientation, 0)
        tiff += struct.pack("<HHI", 0x8825, 4, 1) + struct.pack("<I", 38) + struct.pack("<I", 0)
        tiff += struct.pack("<H", 1) + struct.pack("<HHI", 1, 2, 2) + b"N\x00\x00\x00" + struct.pack("<I", 0)
        kw["exif"] = b"Exif\x00\x00" + tiff
    buf = io.BytesIO()
    im.save(buf, "JPEG", **kw)
    return buf.getvalue()


def _pdf(path: Path, n: int, *, js: bool = False, encrypt: bool = False) -> Path:
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, TextStringObject

    w = PdfWriter()
    for _ in range(n):
        w.add_blank_page(200, 300)
    if js:
        w.add_metadata({"/Author": "selftest-author"})
        w.root_object[NameObject("/OpenAction")] = DictionaryObject(
            {NameObject("/S"): NameObject("/JavaScript"), NameObject("/JS"): TextStringObject("1")})
        w.add_outline_item("bm", 0)
    if encrypt:
        w.encrypt(user_password="", owner_password="o", algorithm="RC4-128")
    with open(path, "wb") as f:
        w.write(f)
    return path


def _write(writer: Any, path: Path) -> None:
    from deskkit.modules.pagepress import sanitize

    sanitize.clean(writer)
    with open(path, "wb") as f:
        writer.write(f)


def _checks(r: _Result, tmp: Path) -> None:
    from pypdf import PdfReader

    from deskkit.modules.pagepress import build, compress, images, ranges, reader, sanitize
    from deskkit.modules.pagepress.oplog import OpsLog

    reader.quiet_pypdf_logging()
    spans = ranges.parse("1-3, 5, 8-", 10)
    r.check("範囲: 1-3, 5, 8- を 3 つに分ける", [s.resolve(10) for s in spans] == [(1, 3), (5, 5), (8, 10)])
    try:
        ranges.parse("5-3", 10)
        r.check("範囲: 逆順を断る", False)
    except ranges.RangeError as e:
        r.check("範囲: 逆順を断る", e.message == "5-3 の順が逆です")

    a = _pdf(tmp / "a.pdf", 3, js=True)
    b = _pdf(tmp / "b.pdf", 2)
    photo = tmp / "p.jpg"
    photo.write_bytes(_jpeg(400, 300, orientation=6))
    enc = _pdf(tmp / "e.pdf", 1, encrypt=True)
    try:
        reader.probe(1, enc)
        r.check("暗号化(保護だけ)を断る", False)
    except reader.ProbeError as e:
        r.check("暗号化(保護だけ)を断る", e.code == "encrypted")

    entries = [reader.probe(i, p) for i, p in enumerate((a, b, photo))]
    ctl = build.Control(threading.Event())
    out = tmp / "merged.pdf"
    with ExitStack() as st:
        w = build.merge(entries, build.ImageOpts(), ctl, st)
        _write(w, out)
    rd = PdfReader(str(out))
    root: Any = rd.trailer["/Root"]
    r.check("まとめる: ページ数が合計と同じ", len(rd.pages) == 6)
    r.check("まとめる: /Info・/OpenAction・/Names が無い",
            "/Info" not in rd.trailer and "/OpenAction" not in root and "/Names" not in root)
    r.check("まとめる: しおりを保つ", len([o for o in rd.outline if not isinstance(o, list)]) == 1)
    r.check("検査: 合格する", sanitize.verify(io.BytesIO(out.read_bytes()), 6) is None)
    res: Any = rd.pages[5]["/Resources"]
    img = res["/XObject"]["/Im0"].get_object()
    data = bytes(img.get_data())
    r.check("写真: APP1(Exif・位置情報)を外し、縦長のページ", 0xE1 not in images.jpeg_segments(data)
            and float(rd.pages[5].mediabox.height) > float(rd.pages[5].mediabox.width))

    with ExitStack() as st:
        raw = build.merge(entries[:1], build.ImageOpts(), ctl, st)
        bad = tmp / "bad.pdf"
        with open(bad, "wb") as f:
            raw.write(f)  # 掃除しない
    r.check("検査: 持ち込まない物が残った出力は落とす", sanitize.verify(io.BytesIO(bad.read_bytes()), 3) is not None)

    with ExitStack() as st:
        rr = build.open_reader(_pdf(tmp / "ten.pdf", 10), st)
        parts = [(x, y, len(wr.pages)) for x, y, wr in build.split_spans(rr, ranges.every(10, 3), ctl)]
    r.check("分ける: 3 ページごとで 3・3・3・1", [p[2] for p in parts] == [3, 3, 3, 1])

    with ExitStack() as st:
        rr = build.open_reader(_pdf(tmp / "four.pdf", 4), st)
        wo = build.organize(rr, [(2, 0), (0, 90), (3, 0)], ctl)
    r.check("整理: 3 ページで 2 ページ目が 90 度", len(wo.pages) == 3 and wo.pages[1].rotation == 90)

    from pypdf import PdfWriter

    wimg = PdfWriter()
    big = _jpeg(1600, 2200, noise=True)
    images.add_page(wimg, images.Payload(big, "/DCTDecode", "/DeviceRGB", 1600, 2200, 1, None),
                    images.place((1600, 2200), None, "paper", "a4", 0))
    src = tmp / "scan.pdf"
    with open(src, "wb") as f:
        wimg.write(f)
    with ExitStack() as st:
        rr = build.open_reader(src, st)
        wc = build.new_writer()
        wc.append(rr)
        stats = compress.shrink_images(wc, "strong", ctl)
        compress.finish(wc)
        small = tmp / "small.pdf"
        _write(wc, small)
    r.check("軽くする: 画像を縮めて小さくなる", stats.replaced == 1 and small.stat().st_size < src.stat().st_size * 0.5)

    ops = OpsLog(tmp / "ops.jsonl")
    ops.write(op="merge", result="ok", inputs=3, images=1, pages=6, outputs=1, in_bytes=1, out_bytes=1, level=None, ms=1,
              code=None)
    text = (tmp / "ops.jsonl").read_text(encoding="utf-8")
    r.check("操作記録: ファイル名・パスを書かない", "a.pdf" not in text and str(tmp) not in text)

    _render_check(r, out)


def _render_check(r: _Result, pdf: Path) -> None:
    from deskkit.modules.pagepress.render import STATE_RUNNING, Renderer

    delivered: list[Any] = []
    got: list[Any] = []
    rd = Renderer(delivered.append)
    rd.open_doc(1, pdf)
    rd.want_pages(1, [0, 5], 160, lambda i, img: got.append((i, img)))
    end = time.monotonic() + 20
    while len(delivered) < 2 and time.monotonic() < end:
        time.sleep(0.02)
    for fn in delivered:
        fn()
    ok = len(got) == 2 and all(img is not None and not img.isNull() for _i, img in got)
    r.check("描画: PDFium が描画スレッドの中で描ける", ok and rd.state == STATE_RUNNING and rd.thread_id != threading.get_ident())
    print(f"  PDFium {rd.pdfium_version}")
    rd.close_doc(1)
    r.check("描画: 描画スレッドが止まる", rd.stop(5))


def run() -> int:
    print("PagePress 自己検査")
    r = _Result()
    try:
        with tempfile.TemporaryDirectory(prefix="pagepress-selftest-") as d:
            _checks(r, Path(d))
    except Exception as e:  # noqa: BLE001 - 想定外の失敗も不合格として返す
        print(f"  [NG] 例外 {type(e).__name__}")
        r.failed += 1
    print("合格" if r.failed == 0 else f"不合格 {r.failed} 件")
    return 0 if r.failed == 0 else 1
