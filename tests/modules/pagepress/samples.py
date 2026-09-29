# PagePress のテスト用の検体をその場で作る(利用者のファイルは使わない)。pypdf の空ページと Pillow の画像だけで作る。
# しおり・JavaScript・添付ファイル・文書情報・入力欄・注釈・名前付きの行き先・暗号化を、必要に応じて足す。
from __future__ import annotations

import io
import struct
from pathlib import Path
from typing import Any

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    StreamObject,
    TextStringObject,
)

MARK = "ZZMARK"  # 目印の文字列(AC-19)


def _write(w: PdfWriter, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        w.write(f)
    return path


def text_page(w: PdfWriter, label: str, width: float = 200, height: float = 300) -> Any:
    """番号の文字を描いたページ(中身でどのページかを見分ける)。"""
    page = w.add_blank_page(width, height)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): w._add_object(font)})})
    cs = DecodedStreamObject()
    cs.set_data(f"BT /F1 24 Tf 20 {height / 2:.0f} Td ({label}) Tj ET".encode("latin-1"))
    page[NameObject("/Contents")] = w._add_object(cs)
    return page


def blank_pdf(path: Path, n: int, *, width: float = 200, height: float = 300, labels: bool = True) -> Path:
    w = PdfWriter()
    for i in range(n):
        if labels:
            text_page(w, f"P{i + 1}", width, height)
        else:
            w.add_blank_page(width, height)
    return _write(w, path)


def _js(code: str) -> DictionaryObject:
    return DictionaryObject({NameObject("/S"): NameObject("/JavaScript"), NameObject("/JS"): TextStringObject(code)})


def rich_pdf(path: Path, n: int = 3, *, mark: str = MARK) -> Path:
    """しおり2つ・/Author・JavaScript の /OpenAction・添付ファイル・XMP・ページラベル・名前付きの行き先を持つ PDF(AC-1)。"""
    w = PdfWriter()
    for i in range(n):
        text_page(w, f"R{i + 1}")
    w.add_metadata({"/Author": f"{mark}-author", "/Title": f"{mark}-title"})
    w.add_outline_item(f"{mark}-bm1", 0)
    w.add_outline_item(f"{mark}-bm2", n - 1)
    w.add_attachment(f"{mark}.txt", f"{mark} attached".encode())
    root = w.root_object
    root[NameObject("/OpenAction")] = _js("app.alert(1)")
    xmp = StreamObject()
    xmp[NameObject("/Type")] = NameObject("/Metadata")
    xmp[NameObject("/Subtype")] = NameObject("/XML")
    xmp.set_data(f"<x:xmpmeta>{mark}</x:xmpmeta>".encode())
    root[NameObject("/Metadata")] = w._add_object(xmp)
    root[NameObject("/PageLabels")] = DictionaryObject({NameObject("/Nums"): ArrayObject(
        [NumberObject(0), DictionaryObject({NameObject("/S"): NameObject("/r")})])})
    root[NameObject("/AA")] = DictionaryObject({NameObject("/WC"): _js("1")})
    w.add_named_destination("last", n - 1)
    page0 = w.pages[0]
    page0[NameObject("/AA")] = DictionaryObject({NameObject("/O"): _js("2")})
    page0[NameObject("/PieceInfo")] = DictionaryObject({NameObject("/X"): DictionaryObject()})
    link = DictionaryObject({NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Link"),
                             NameObject("/Rect"): _rect(10, 10, 60, 40), NameObject("/Dest"): TextStringObject("last")})
    w.add_annotation(0, link)
    return _write(w, path)


def _rect(a: float, b: float, c: float, d: float) -> ArrayObject:
    return ArrayObject([FloatObject(a), FloatObject(b), FloatObject(c), FloatObject(d)])


def annots_pdf(path: Path, n: int = 4) -> Path:
    """/JavaScript のリンク・/Launch のリンク・ページの /AA・/FileAttachment 注釈・/URI のリンク・2 ページ目へのリンクを持つ(AC-2)。"""
    w = PdfWriter()
    for i in range(n):
        text_page(w, f"A{i + 1}")
    page = w.pages[0]
    page[NameObject("/AA")] = DictionaryObject({NameObject("/O"): _js("3")})

    def link(action: DictionaryObject | None, dest: Any = None) -> DictionaryObject:
        d = DictionaryObject({NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Link"),
                              NameObject("/Rect"): _rect(10, 10, 60, 40)})
        if action is not None:
            d[NameObject("/A")] = action
        if dest is not None:
            d[NameObject("/Dest")] = dest
        return d

    w.add_annotation(0, link(_js("4")))
    w.add_annotation(0, link(DictionaryObject({NameObject("/S"): NameObject("/Launch"),
                                               NameObject("/F"): TextStringObject("calc.exe")})))
    w.add_annotation(0, link(DictionaryObject({NameObject("/S"): NameObject("/URI"),
                                               NameObject("/URI"): TextStringObject("https://example.com/")})))
    fs = DictionaryObject({NameObject("/Type"): NameObject("/Filespec"), NameObject("/F"): TextStringObject("a.txt")})
    w.add_annotation(0, DictionaryObject({NameObject("/Type"): NameObject("/Annot"),
                                          NameObject("/Subtype"): NameObject("/FileAttachment"),
                                          NameObject("/Rect"): _rect(70, 10, 90, 30), NameObject("/FS"): fs}))
    # 2 ページ目を指すリンク(抜いたときに外れるか)
    w.add_annotation(0, link(None, ArrayObject([w.pages[1].indirect_reference, NameObject("/Fit")])))
    return _write(w, path)


def form_pdf(path: Path, field: str = "name", value: str = "A", *, xfa: bool = False, signed: bool = False) -> Path:
    w = PdfWriter()
    p = text_page(w, "F1")
    fld = DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Widget"),
        NameObject("/FT"): NameObject("/Tx"), NameObject("/T"): TextStringObject(field),
        NameObject("/V"): TextStringObject(value), NameObject("/Rect"): _rect(10, 10, 150, 40),
        NameObject("/AA"): DictionaryObject({NameObject("/K"): _js("5")}),
    })
    ref = w._add_object(fld)
    fld[NameObject("/P")] = p.indirect_reference
    fields = ArrayObject([ref])
    annots = ArrayObject([ref])
    if signed:
        sig = DictionaryObject({
            NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/FT"): NameObject("/Sig"), NameObject("/T"): TextStringObject("sig"),
            NameObject("/Rect"): _rect(0, 0, 0, 0),
            NameObject("/V"): DictionaryObject({NameObject("/Type"): NameObject("/Sig"),
                                                NameObject("/Filter"): NameObject("/Adobe.PPKLite")}),
        })
        sref = w._add_object(sig)
        fields.append(sref)
        annots.append(sref)
    p[NameObject("/Annots")] = annots
    af = DictionaryObject({NameObject("/Fields"): fields})
    if xfa:
        x = StreamObject()
        x.set_data(b"<xdp/>")
        af[NameObject("/XFA")] = w._add_object(x)
    w.root_object[NameObject("/AcroForm")] = af
    return _write(w, path)


def encrypted_pdf(path: Path, *, user: str = "", owner: str = "owner") -> Path:
    w = PdfWriter()
    text_page(w, "E1")
    w.encrypt(user_password=user, owner_password=owner, algorithm="RC4-128")
    return _write(w, path)


# ------------------------------------------------------------------ 画像
def _exif(orientation: int | None, gps: bool) -> bytes:
    entries: list[bytes] = []
    extra = b""
    count = 0
    if orientation is not None:
        entries.append(struct.pack("<HHI", 0x0112, 3, 1) + struct.pack("<HH", orientation, 0))
        count += 1
    if gps:
        count += 1
    ifd0_size = 2 + 12 * count + 4
    gps_off = 8 + ifd0_size
    if gps:
        entries.append(struct.pack("<HHI", 0x8825, 4, 1) + struct.pack("<I", gps_off))
        extra = struct.pack("<H", 1) + struct.pack("<HHI", 1, 2, 2) + b"N\x00\x00\x00" + struct.pack("<I", 0)
    tiff = b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", count) + b"".join(entries) + struct.pack("<I", 0) + extra
    return b"Exif\x00\x00" + tiff


def jpeg_bytes(w: int, h: int, *, orientation: int | None = None, gps: bool = False, quality: int = 90,
               mode: str = "RGB", noise: bool = False, marker: bool = True, comment: bytes | None = None) -> bytes:
    """左上に赤い印のある JPEG。orientation と GPS の Exif を付けられる。"""
    from PIL import Image

    im = Image.effect_noise((w, h), 60).convert(mode) if noise else Image.new(mode, (w, h), (200, 200, 200)
                                                                                   if mode == "RGB" else 200)
    if marker and mode == "RGB":
        im.paste((255, 0, 0), (0, 0, max(1, w // 4), max(1, h // 4)))
    buf = io.BytesIO()
    kw: dict[str, Any] = {"quality": quality}
    if orientation is not None or gps:
        kw["exif"] = _exif(orientation, gps)
    if comment is not None:
        kw["comment"] = comment
    im.save(buf, "JPEG", **kw)
    return buf.getvalue()


def png_bytes(w: int, h: int, *, mode: str = "RGBA") -> bytes:
    from PIL import Image

    if mode == "I;16":
        im = Image.new("I;16", (w, h), 40000)
    elif mode == "RGBA":
        im = Image.new("RGBA", (w, h), (0, 0, 255, 0))
        im.paste((0, 0, 255, 255), (0, 0, w // 2, h))
    else:
        im = Image.new(mode, (w, h))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def image_pdf(path: Path, pages: int, *, w: int = 2480, h: int = 3508, quality: int = 90, noise: bool = True) -> Path:
    """1 ページに w×h の JPEG を A4 いっぱいに置いた PDF(検体 B・AC-10)。"""
    from deskkit.modules.pagepress import images

    wr = PdfWriter()
    for i in range(pages):
        data = jpeg_bytes(w, h, quality=quality, noise=noise, marker=False)
        pl = images.place((w, h), None, "paper", "a4", 0)
        images.add_page(wr, images.Payload(data, "/DCTDecode", "/DeviceRGB", w, h, 1, None), pl)
        del i
    return _write(wr, path)
