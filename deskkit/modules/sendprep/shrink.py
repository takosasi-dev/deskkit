# 画像を目標のバイト数に収める(S-4・S-5)。品質を二分探索し、それでも超えるなら長辺を 0.85 倍ずつ縮めて探し直す。
# 保存するときはメタデータを渡さない(ICC と Orientation だけの Exif は呼び出し側が明示したときだけ付ける)。
# Pillow はこのファイルを読み込んだときに初めて import する(jobs が必要になってから読む。NFR-5)。
from __future__ import annotations

import io
from collections.abc import Callable
from dataclasses import dataclass

from PIL import Image

from deskkit.modules.sendprep import metadata

Q_HI = 92
Q_HI_FROM_PNG = 90
Q_LO = 60
SCALE_STEP = 0.85
MAX_SHRINKS = 8
MIN_LONG_EDGE = 640


@dataclass(frozen=True)
class Encoded:
    data: bytes
    fmt: str            # "jpeg" / "png" / "webp"
    size: tuple[int, int]
    quality: int | None
    resized: bool


# ------------------------------------------------------------------ 保存(メタデータは付けない)
def has_alpha(im: Image.Image) -> bool:
    """実際に透けている画素があるか(RGBA でも全部不透明なら False)。"""
    if im.mode in ("RGBA", "LA", "PA"):
        ext = im.getchannel("A").getextrema()
        return isinstance(ext[0], (int, float)) and ext[0] < 255
    if im.mode == "P" and "transparency" in im.info:
        return has_alpha(im.convert("RGBA"))
    return False


def to_rgb(im: Image.Image) -> Image.Image:
    if im.mode in ("RGB", "L"):
        return im
    if im.mode == "CMYK":
        return im.convert("RGB")
    if im.mode in ("I;16", "I;16B", "I;16L", "I"):
        return im.point(lambda v: v / 256).convert("L")
    return im.convert("RGB")


def encode_jpeg(im: Image.Image, quality: int, icc: bytes | None = None, orientation: int = 1) -> bytes:
    buf = io.BytesIO()
    kw: dict[str, object] = {"quality": quality, "optimize": True, "comment": b""}
    if icc:
        kw["icc_profile"] = icc
    if orientation != 1:
        kw["exif"] = metadata.minimal_exif(orientation)  # "Exif\0\0" から始まるバイト列をそのまま APP1 に書く
    to_rgb(im).save(buf, "JPEG", **kw)
    return buf.getvalue()


def encode_png(im: Image.Image, icc: bytes | None = None) -> bytes:
    buf = io.BytesIO()
    kw: dict[str, object] = {"optimize": True}
    if icc:
        kw["icc_profile"] = icc
    src = im
    if im.mode not in ("1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16"):
        src = im.convert("RGBA" if has_alpha(im) else "RGB")
    if src.mode == "P" and "transparency" in src.info:
        kw["transparency"] = src.info["transparency"]
    src.save(buf, "PNG", **kw)
    return buf.getvalue()


def encode_webp(im: Image.Image, quality: int, icc: bytes | None = None) -> bytes:
    buf = io.BytesIO()
    kw: dict[str, object] = {"quality": quality, "method": 4}
    if icc:
        kw["icc_profile"] = icc
    src = im if im.mode in ("RGB", "RGBA") else im.convert("RGBA" if has_alpha(im) else "RGB")
    src.save(buf, "WEBP", **kw)
    return buf.getvalue()


# ------------------------------------------------------------------ 探索
def _long_edge(im: Image.Image) -> int:
    return max(im.size)


def _resized(im: Image.Image, long_edge: int) -> Image.Image:
    w, h = im.size
    s = long_edge / max(w, h)
    size = (max(1, round(w * s)), max(1, round(h * s)))
    src = im if im.mode not in ("P", "1") else im.convert("RGBA" if has_alpha(im) else "RGB")
    return src.resize(size, Image.Resampling.LANCZOS, reducing_gap=3.0)


def _sizes(im: Image.Image) -> list[int]:
    """試す長辺: 元の大きさ → 0.85 倍ずつ最大 8 回(640px を下回るものは試さない)。"""
    base = _long_edge(im)
    out = [base]
    for i in range(1, MAX_SHRINKS + 1):
        le = int(base * (SCALE_STEP ** i))
        if le < MIN_LONG_EDGE:
            break
        out.append(le)
    return out


def fit_quality(im: Image.Image, limit: int, encode: Callable[[Image.Image, int], bytes], fmt: str,
                q_hi: int = Q_HI, q_lo: int = Q_LO, cancelled: Callable[[], bool] = lambda: False) -> Encoded | None:
    """S-4: 品質 q_hi→q_lo の二分探索。収まらなければ長辺を縮めて探し直す。どうしても収まらなければ None。"""
    for i, le in enumerate(_sizes(im)):
        if cancelled():
            return None
        cur = im if i == 0 else _resized(im, le)
        low = encode(cur, q_lo)
        if len(low) > limit:
            continue  # いちばん低い品質でも超える → 縮める
        hi_bytes = encode(cur, q_hi)
        if len(hi_bytes) <= limit:
            return Encoded(hi_bytes, fmt, cur.size, q_hi, i > 0)
        lo, hi, best = q_lo, q_hi, low
        while hi - lo > 1:
            mid = (lo + hi) // 2
            b = encode(cur, mid)
            if len(b) <= limit:
                lo, best = mid, b
            else:
                hi = mid
        return Encoded(best, fmt, cur.size, lo, i > 0)
    return None


def fit_jpeg(im: Image.Image, limit: int, icc: bytes | None, orientation: int = 1, q_hi: int = Q_HI,
             cancelled: Callable[[], bool] = lambda: False) -> Encoded | None:
    return fit_quality(im, limit, lambda m, q: encode_jpeg(m, q, icc, orientation), "jpeg", q_hi, Q_LO, cancelled)


def fit_webp(im: Image.Image, limit: int, icc: bytes | None, cancelled: Callable[[], bool] = lambda: False) -> Encoded | None:
    return fit_quality(im, limit, lambda m, q: encode_webp(m, q, icc), "webp", Q_HI, Q_LO, cancelled)


def fit_png(im: Image.Image, limit: int, icc: bytes | None, orientation: int = 1,
            cancelled: Callable[[], bool] = lambda: False) -> Encoded | None:
    """S-5: まず PNG の最適化保存。超えるなら、透過が無ければ JPEG(品質 90 から S-4)、透過があれば PNG のまま長辺を縮める。"""
    png = encode_png(im, icc)
    if len(png) <= limit:
        return Encoded(png, "png", im.size, None, False)
    if not has_alpha(im):
        return fit_jpeg(im, limit, icc, orientation, Q_HI_FROM_PNG, cancelled)
    for i, le in enumerate(_sizes(im)):
        if i == 0:
            continue  # 元の大きさは上で試した
        if cancelled():
            return None
        cur = _resized(im, le)
        b = encode_png(cur, icc)
        if len(b) <= limit:
            return Encoded(b, "png", cur.size, None, True)
    return None
