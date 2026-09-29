# 画像 → PDF のページ(P-8・P-9)。JPEG(RGB・グレー)は作り直さずに入れ、APP1(Exif・XMP)・APP13(IPTC)・COM だけを外す。
# 向き(Orientation)3・6・8 は配置の行列で回し、2・4・5・7 と CMYK・HEIC は Pillow で向きを直して品質 92 の JPEG にする。
# PNG は透明を白で埋め、16 ビットは 8 ビットにして Flate(可逆)で入れる。SendPrep の metadata.py は import しない(C-1)。
from __future__ import annotations

import io
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deskkit.modules.pagepress.reader import MSG_BAD_IMAGE, MSG_TOO_BIG_IMAGE, ProbeError, open_image

A4 = (595.28, 841.89)
LETTER = (612.0, 792.0)
PAPERS = {"a4": A4, "letter": LETTER}
MAX_SIDE_PT = 14400.0
DEFAULT_DPI = 96.0
REENCODE_QUALITY = 92
_DROP_MARKERS = frozenset({0xE1, 0xED, 0xFE})  # APP1・APP13・COM


class JpegStructureError(ValueError):
    pass


def strip_jpeg(data: bytes) -> bytes:
    """SOS より前の APP1・APP13・COM を外した JPEG。圧縮されたデータ(SOS 以降)は1バイトも変えない。"""
    if data[:2] != b"\xff\xd8":
        raise JpegStructureError("not jpeg")
    out = bytearray(b"\xff\xd8")
    i = 2
    n = len(data)
    while i < n:
        if data[i] != 0xFF:
            raise JpegStructureError("marker expected")
        j = i
        while j < n and data[j] == 0xFF:  # 詰め物の FF は飛ばす
            j += 1
        if j >= n:
            raise JpegStructureError("truncated")
        marker = data[j]
        if marker == 0xD9:  # EOI
            out += b"\xff\xd9"
            return bytes(out)
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            out += bytes((0xFF, marker))
            i = j + 1
            continue
        if j + 3 > n:
            raise JpegStructureError("truncated")
        (length,) = struct.unpack(">H", data[j + 1:j + 3])
        end = j + 1 + length
        if length < 2 or end > n:
            raise JpegStructureError("bad length")
        if marker == 0xDA:  # SOS: ここから後ろはそのまま
            out += data[j - 1:]
            return bytes(out)
        if marker not in _DROP_MARKERS:
            out += b"\xff" + data[j:end]
        i = end
    raise JpegStructureError("no SOS")


def jpeg_segments(data: bytes) -> list[int]:
    """SOS までのマーカーの一覧(テスト・検査用)。"""
    seen: list[int] = []
    i = 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        m = data[i + 1]
        if m == 0xDA:
            seen.append(m)
            break
        (length,) = struct.unpack(">H", data[i + 2:i + 4])
        seen.append(m)
        i += 2 + length
    return seen


@dataclass(frozen=True)
class Payload:
    """PDF の画像オブジェクトにする中身。width・height は記録された画素(回す前)。"""

    data: bytes
    filter: str            # "/DCTDecode" | "/FlateDecode"
    colorspace: str        # "/DeviceRGB" | "/DeviceGray"
    width: int
    height: int
    orientation: int       # 1・3・6・8(配置の行列で回す)
    dpi: float | None

    @property
    def display_size(self) -> tuple[int, int]:
        return (self.height, self.width) if self.orientation in (6, 8) else (self.width, self.height)


def _dpi_of(im: Any) -> float | None:
    d = im.info.get("dpi")
    try:
        if isinstance(d, tuple) and d:
            return float(d[0])
        if d is not None:
            return float(d)
    except (TypeError, ValueError):
        return None
    return None


def _orientation(im: Any) -> int:
    try:
        o = int(im.getexif().get(0x0112) or 1)
    except Exception:  # noqa: BLE001 - Exif が壊れていても向きなしとして続ける
        return 1
    return o if 1 <= o <= 8 else 1


def _encode_jpeg(im: Any) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=REENCODE_QUALITY, optimize=True)  # info を渡さないので Exif・XMP・コメントは入らない
    return buf.getvalue()


def _white(im: Any) -> Any:
    """透明を白で埋めた RGB。"""
    from PIL import Image

    rgba = im.convert("RGBA")
    bg = Image.new("RGB", rgba.size, (255, 255, 255))
    bg.paste(rgba, mask=rgba.getchannel("A"))
    return bg


def _to_8bit(im: Any) -> Any:
    """PNG の画像を 8 ビットの L か RGB にする(透明は白で埋める)。"""
    import numpy as np
    from PIL import Image

    mode = im.mode
    if mode in ("I;16", "I;16B", "I;16L", "I", "I;16N"):
        arr = np.asarray(im, dtype=np.uint32 if mode == "I" else np.uint16)
        top = 65535 if mode != "I" or arr.max(initial=0) > 255 else 255
        return Image.fromarray((arr.astype(np.float64) * (255.0 / top)).clip(0, 255).astype(np.uint8), "L")
    has_alpha = mode in ("RGBA", "LA", "PA", "La", "RGBa") or "transparency" in im.info
    if has_alpha:
        return _white(im)
    if mode == "L":
        return im
    if mode == "1":
        return im.convert("L")
    return im.convert("RGB")


def _pil_payload(im: Any, dpi: float | None, *, jpeg: bool) -> Payload:
    if jpeg:
        if im.mode not in ("RGB", "L"):
            im = _white(im) if "A" in im.getbands() else im.convert("RGB")
        return Payload(_encode_jpeg(im), "/DCTDecode", "/DeviceGray" if im.mode == "L" else "/DeviceRGB",
                       im.width, im.height, 1, dpi)
    im8 = _to_8bit(im)
    cs = "/DeviceGray" if im8.mode == "L" else "/DeviceRGB"
    return Payload(zlib.compress(im8.tobytes(), 6), "/FlateDecode", cs, im8.width, im8.height, 1, dpi)


def prepare(path: Path) -> Payload:
    """画像ファイルを読み、ページに入れる中身にする。読めなければ ProbeError。"""
    from PIL import Image, ImageOps

    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        raise ProbeError("unreadable", MSG_BAD_IMAGE) from None
    im = open_image(path)
    try:
        fmt = (im.format or "").upper()
        dpi = _dpi_of(im)
        orient = _orientation(im)
        if fmt == "JPEG":
            if im.mode in ("RGB", "L") and orient in (1, 3, 6, 8):
                try:
                    data = strip_jpeg(raw)
                except JpegStructureError:
                    raise ProbeError("bad_image", MSG_BAD_IMAGE) from None
                return Payload(data, "/DCTDecode", "/DeviceGray" if im.mode == "L" else "/DeviceRGB",
                               im.width, im.height, orient, dpi)
            im.load()
            return _pil_payload(ImageOps.exif_transpose(im), dpi, jpeg=True)
        im.load()
        fixed = ImageOps.exif_transpose(im)  # PNG の eXIf。HEIC は pi-heif が回して向きを 1 に戻している
        return _pil_payload(fixed, dpi, jpeg=fmt != "PNG")
    except Image.DecompressionBombError:
        raise ProbeError("too_big_image", MSG_TOO_BIG_IMAGE) from None
    except ProbeError:
        raise
    except (OSError, ValueError, SyntaxError, EOFError, IndexError, TypeError, KeyError, MemoryError):
        raise ProbeError("bad_image", MSG_BAD_IMAGE) from None
    finally:
        im.close()


# ------------------------------------------------------------------ 配置(P-9)
@dataclass(frozen=True)
class Placement:
    page_w: float
    page_h: float
    x: float
    y: float
    w: float     # 画像を置く箱(向きを直したあとの見た目)
    h: float


def place(display: tuple[int, int], dpi: float | None, fit: str, paper: str, margin_mm: int) -> Placement:
    iw, ih = max(1, display[0]), max(1, display[1])
    if fit == "original":
        d = dpi if dpi is not None and 72 <= dpi <= 1200 else DEFAULT_DPI
        pw, ph = iw / d * 72.0, ih / d * 72.0
        big = max(pw, ph)
        if big > MAX_SIDE_PT:
            k = MAX_SIDE_PT / big
            pw, ph = pw * k, ph * k
        return Placement(pw, ph, 0.0, 0.0, pw, ph)
    short, long_ = PAPERS.get(paper, A4)
    pw, ph = (long_, short) if iw > ih else (short, long_)
    m = margin_mm * 72.0 / 25.4
    bw, bh = max(1.0, pw - 2 * m), max(1.0, ph - 2 * m)
    k = min(bw / iw, bh / ih)
    w, h = iw * k, ih * k
    return Placement(pw, ph, (pw - w) / 2, (ph - h) / 2, w, h)


def matrix(orientation: int, pl: Placement) -> tuple[float, float, float, float, float, float]:
    """単位正方形の画像を箱へ写す cm 行列。orientation は EXIF の値(3・6・8 は回す)。"""
    x, y, w, h = pl.x, pl.y, pl.w, pl.h
    if orientation == 6:   # 時計回りに 90 度
        return (0.0, -h, w, 0.0, x, y + h)
    if orientation == 8:   # 反時計回りに 90 度
        return (0.0, h, -w, 0.0, x + w, y)
    if orientation == 3:
        return (-w, 0.0, 0.0, -h, x + w, y + h)
    return (w, 0.0, 0.0, h, x, y)


def add_page(writer: Any, payload: Payload, pl: Placement) -> Any:
    """writer に画像1枚のページを足して返す。"""
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject, StreamObject

    img = StreamObject()
    img[NameObject("/Type")] = NameObject("/XObject")
    img[NameObject("/Subtype")] = NameObject("/Image")
    img[NameObject("/Width")] = NumberObject(payload.width)
    img[NameObject("/Height")] = NumberObject(payload.height)
    img[NameObject("/ColorSpace")] = NameObject(payload.colorspace)
    img[NameObject("/BitsPerComponent")] = NumberObject(8)
    img[NameObject("/Filter")] = NameObject(payload.filter)
    img.set_data(payload.data)
    ref = writer._add_object(img)
    page = writer.add_blank_page(pl.page_w, pl.page_h)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): ref}),
        NameObject("/ProcSet"): ArrayObject([NameObject("/PDF"), NameObject("/ImageC" if payload.colorspace == "/DeviceRGB" else "/ImageB")]),
    })
    cm = " ".join(f"{v:.4f}" for v in matrix(payload.orientation, pl))
    content = DecodedStreamObject()
    content.set_data(f"q {cm} cm /Im0 Do Q".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(content)
    return page
