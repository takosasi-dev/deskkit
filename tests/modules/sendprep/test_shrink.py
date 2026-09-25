# 目標サイズへの縮小(S-4・S-5・FR-7)。AC-4(25MB 相当 → 10MB 以下)と、640px の下限・PNG の扱い。
from __future__ import annotations

import io

from PIL import Image

from deskkit.modules.sendprep import metadata as M
from deskkit.modules.sendprep import shrink


def _photo(size: tuple[int, int]) -> Image.Image:
    """写真らしく圧縮しにくい合成画像(ノイズとグラデーション)。"""
    noise = Image.effect_noise(size, 70).convert("RGB")
    grad = Image.linear_gradient("L").resize(size).convert("RGB")
    return Image.blend(noise, grad, 0.35)


def test_ac4_25mb_class_to_10mb() -> None:
    im = _photo((6000, 4500))
    big = shrink.encode_jpeg(im, 98)
    assert len(big) > 20_000_000  # 前提: 25MB 相当
    enc = shrink.fit_jpeg(im, 10_000_000, None, 6)
    assert enc is not None and len(enc.data) <= 10_000_000
    assert M.verify_jpeg(enc.data) and M.jpeg_orientation(enc.data) == 6
    assert 60 <= (enc.quality or 0) <= 92


def test_quality_first_then_resize() -> None:
    im = _photo((1600, 1200))
    q60 = len(shrink.encode_jpeg(im, 60))
    q92 = len(shrink.encode_jpeg(im, 92))
    enc = shrink.fit_jpeg(im, (q60 + q92) // 2, None)
    assert enc is not None and not enc.resized and enc.size == (1600, 1200) and 60 < (enc.quality or 0) < 92
    enc2 = shrink.fit_jpeg(im, q60 // 2, None)
    assert enc2 is not None and enc2.resized and max(enc2.size) < 1600 and len(enc2.data) <= q60 // 2


def test_gives_up_below_640() -> None:
    im = _photo((640, 480))
    assert shrink.fit_jpeg(im, 10_000, None) is None  # AC-5 の条件
    sizes = shrink._sizes(_photo((4000, 100)))
    assert sizes[0] == 4000 and len(sizes) == 9 and all(s >= 640 for s in sizes)


def test_png_s5_opaque_to_jpeg_and_alpha_stays_png() -> None:
    opaque = _photo((1400, 900))
    png = shrink.encode_png(opaque)
    enc = shrink.fit_png(opaque, len(png) // 3, None)
    assert enc is not None and enc.fmt == "jpeg" and len(enc.data) <= len(png) // 3
    alpha = opaque.convert("RGBA")
    alpha.putalpha(Image.linear_gradient("L").resize(alpha.size))
    apng = shrink.encode_png(alpha)
    enc2 = shrink.fit_png(alpha, len(apng) // 2, None)
    assert enc2 is not None and enc2.fmt == "png" and enc2.resized and len(enc2.data) <= len(apng) // 2
    assert Image.open(io.BytesIO(enc2.data)).mode == "RGBA"
    small = Image.new("RGB", (100, 100), (1, 2, 3))
    enc3 = shrink.fit_png(small, 10_000_000, None)
    assert enc3 is not None and enc3.fmt == "png" and not enc3.resized


def test_saved_output_has_no_metadata() -> None:
    im = Image.new("RGB", (50, 50))
    im.info["comment"] = b"secret"
    im.info["exif"] = b"Exif\x00\x00II*\x00"
    for data, fmt in ((shrink.encode_jpeg(im, 80), "jpeg"), (shrink.encode_png(im), "png"), (shrink.encode_webp(im, 80), "webp")):
        assert M.verify(fmt, data) and b"secret" not in data
    assert shrink.has_alpha(Image.new("RGBA", (4, 4), (0, 0, 0, 255))) is False
    assert shrink.has_alpha(Image.new("RGBA", (4, 4), (0, 0, 0, 10))) is True
