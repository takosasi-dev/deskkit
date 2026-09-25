# メタデータ除去と検証(S-1・S-2・FR-6・FR-9)。AC-1(GPS が消え、画素が同じ)・AC-2(Orientation だけ残る)・AC-3(PNG のチャンク)。
from __future__ import annotations

import io
import struct

import pytest
from PIL import Image

from deskkit.modules.sendprep import metadata as M

from .conftest import jpeg_with_meta, png_with_meta


def _pixels(data: bytes) -> bytes:
    im = Image.open(io.BytesIO(data))
    im.load()
    return im.convert("RGB").tobytes()


def test_detect() -> None:
    assert M.detect(jpeg_with_meta()) == "jpeg"
    assert M.detect(png_with_meta()) == "png"
    assert M.detect(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert M.detect(b"GIF89a....") == "gif"
    assert M.detect(b"BM" + b"\x00" * 40) == "bmp"
    assert M.detect(struct.pack(">I", 24) + b"ftypmif1\x00\x00\x00\x00mif1heic") == "heif"
    assert M.detect(struct.pack(">I", 20) + b"ftypavif\x00\x00\x00\x00avif") is None
    assert M.detect(b"hello") is None


def test_ac1_jpeg_gps_removed_pixels_same() -> None:
    src = jpeg_with_meta(trailer=True)
    assert Image.open(io.BytesIO(src)).getexif().get_ifd(0x8825)  # 前提: GPS がある
    out = M.strip_jpeg(src)
    assert M.verify_jpeg(out)
    ex = Image.open(io.BytesIO(out)).getexif()
    assert not ex.get_ifd(0x8825) and 0x8825 not in ex and 0x010F not in ex
    assert _pixels(out) == _pixels(src)
    for needle in (b"SecretCam", b"GPS secret", b"secret comment", b"8BIM", b"MPF\x00", b"secret-embedded-image"):
        assert needle not in out
    assert b"ICC_PROFILE\x00" in out  # ICC は残る(FR-6)
    assert not M.verify_jpeg(src)


def test_ac2_orientation_only_exif() -> None:
    src = jpeg_with_meta(orientation=6)
    out = M.strip_jpeg(src)
    ex = Image.open(io.BytesIO(out)).getexif()
    assert dict(ex) == {0x0112: 6}
    assert M.jpeg_orientation(out) == 6 and M.verify_jpeg(out)
    segs, _ = M.jpeg_segments(out)
    app1 = [s for s in segs if s.marker == M.APP1]
    assert len(app1) == 1 and M.is_orientation_only_exif(app1[0].payload)
    # JFIF の APP0 が先頭のまま
    assert segs[1].marker == M.APP0


def test_jpeg_without_exif_gets_no_app1() -> None:
    im = Image.new("RGB", (40, 30), (1, 2, 3))
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    out = M.strip_jpeg(buf.getvalue())
    assert b"\xff\xe1" not in out[:200] and M.verify_jpeg(out)


def test_verify_rejects_other_exif_and_broken() -> None:
    payload = b"Exif\x00\x00" + b"MM\x00\x2a" + struct.pack(">I", 8) + struct.pack(">H", 2)
    payload += struct.pack(">HHI", 0x0112, 3, 1) + struct.pack(">HH", 1, 0)
    payload += struct.pack(">HHI", 0x010F, 2, 4) + b"abc\x00" + struct.pack(">I", 0)
    assert not M.is_orientation_only_exif(payload)
    assert M.is_orientation_only_exif(M.minimal_exif(3))
    assert not M.verify_jpeg(b"\xff\xd8\xff\xe0\x00")
    with pytest.raises(M.MetadataError):
        M.strip_jpeg(b"not a jpeg")


def test_progressive_jpeg_keeps_all_scans() -> None:
    im = Image.effect_noise((160, 120), 60).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "JPEG", progressive=True, exif=b"Exif\x00\x00" + b"II*\x00\x08\x00\x00\x00\x00\x00\x00\x00\x00\x00", comment=b"c")
    src = buf.getvalue()
    out = M.strip_jpeg(src)
    assert _pixels(out) == _pixels(src) and M.verify_jpeg(out)


def test_ac3_png_chunks() -> None:
    src = png_with_meta()
    types = [t for t, _ in M.png_chunks(src)]
    assert {b"tEXt", b"iTXt", b"eXIf", b"tIME"} <= set(types)
    out = M.strip_png(src)
    t2 = [t for t, _ in M.png_chunks(out)]
    assert not ({b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME"} & set(t2))
    idat = [raw for t, raw in M.png_chunks(src) if t == b"IDAT"]
    assert idat == [raw for t, raw in M.png_chunks(out) if t == b"IDAT"]
    assert M.verify_png(out) and not M.verify_png(src)
    assert _pixels(out) == _pixels(src)


def _webp_with_meta() -> bytes:
    im = Image.new("RGBA", (64, 48), (10, 200, 30, 200))
    buf = io.BytesIO()
    im.save(buf, "WEBP", exif=b"Exif\x00\x00" + b"II*\x00\x08\x00\x00\x00\x00\x00\x00\x00\x00\x00",
            xmp=b"<x:xmpmeta>secret</x:xmpmeta>")
    return buf.getvalue()


def test_webp_strip() -> None:
    src = _webp_with_meta()
    fcc = [f for f, _ in M.webp_chunks(src)]
    assert b"EXIF" in fcc and b"XMP " in fcc
    out = M.strip_webp(src)
    assert M.verify_webp(out) and not M.verify_webp(src)
    assert b"secret" not in out
    assert struct.unpack("<I", out[4:8])[0] == len(out) - 8
    vp8x = dict(M.webp_chunks(out))[b"VP8X"]
    assert not vp8x[8] & (M.VP8X_EXIF | M.VP8X_XMP) and vp8x[8] & M.VP8X_ALPHA
    a = Image.open(io.BytesIO(out))
    a.load()
    b = Image.open(io.BytesIO(src))
    b.load()
    assert a.tobytes() == b.tobytes()
    assert not M.webp_is_animated(out)


def test_verify_dispatch() -> None:
    assert not M.verify("gif", b"")
