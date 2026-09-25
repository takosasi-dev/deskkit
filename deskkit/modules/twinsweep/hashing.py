# 画像1枚ごとの特徴(dHash 64bit・SHA-256・幅高さ・撮影日時・鮮明さ)を計算する(G-1・G-2・G-4)。
# ファイルは読み取り専用で1回だけ読み、JPEG は draft() で縮小しながらデコードする。回転(Orientation)は適用してから計算する。
# Pillow・numpy・pi-heif は最初に使うときに import する(NFR-5)。読めない画像は None を返すだけで、パスはどこにも書かない。
from __future__ import annotations

import hashlib
import io
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

WORK_SIDE = 512            # 鮮明さを測る白黒画像の長辺(G-4)
_STREAM_LIMIT = 256 * 1024 * 1024  # これより大きいファイルは読み込み済みのバイト列ではなくファイルから直接デコードする
_heif_lock = threading.Lock()
_heif_ready = False

# Orientation の値 → Pillow の Transpose(ImageOps.exif_transpose と同じ対応)
_ORIENT_OPS = {2: "FLIP_LEFT_RIGHT", 3: "ROTATE_180", 4: "FLIP_TOP_BOTTOM", 5: "TRANSPOSE", 6: "ROTATE_270",
               7: "TRANSVERSE", 8: "ROTATE_90"}


@dataclass(frozen=True)
class Features:
    sha256: str
    dhash: int | None      # None = 読めない画像(キャッシュにもこの形で残し、次回は開かない)
    width: int
    height: int
    taken_at: str | None   # EXIF の撮影日時(ISO 8601、秒まで)。無い・壊れているときは None
    sharpness: float

    @property
    def readable(self) -> bool:
        return self.dhash is not None


class FileChangedError(Exception):
    """読んでいる間にファイルが変わった・消えた(§10: 飛ばす。キャッシュにも書かない)。"""


def numpy() -> Any:
    """numpy を使う直前に読む(NFR-5)。PyInstaller が依存を見つけられるよう、文字列ではなく import 文で読む。"""
    import numpy as np

    return np


def pil_image() -> Any:
    """PIL.Image を使う直前に読む(NFR-5)。"""
    from PIL import Image

    return Image


def ensure_heif() -> None:
    global _heif_ready
    if _heif_ready:
        return
    with _heif_lock:
        if _heif_ready:
            return
        try:
            import pi_heif

            pi_heif.register_heif_opener()
        except Exception:  # noqa: BLE001 - HEIC が読めないだけで他の形式は続ける
            pass
        _heif_ready = True


def parse_exif_datetime(v: Any) -> str | None:
    if isinstance(v, bytes):
        v = v.decode("ascii", "ignore")
    if not isinstance(v, str):
        return None
    s = v.strip().strip("\x00")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M"):
        try:
            dt = datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
        if dt.year < 1971:
            return None
        return dt.isoformat(timespec="seconds")
    return None


def to_signed64(v: int) -> int:
    return v - (1 << 64) if v >= (1 << 63) else v


def to_unsigned64(v: int) -> int:
    return v + (1 << 64) if v < 0 else v


def dhash_of(gray: Any) -> int:
    """白黒の PIL 画像から 8×9 の輝度差の 64bit(左の画素より右が明るければ 1)。"""
    np = numpy()
    pil = pil_image()

    small = gray.resize((9, 8), pil.Resampling.BOX)
    a = np.asarray(small, dtype=np.int16)
    bits = (a[:, 1:] > a[:, :-1]).flatten()
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


def sharpness_of(gray: Any) -> float:
    """長辺 512px の白黒画像のラプラシアンの分散(G-4)。"""
    np = numpy()

    a = np.asarray(gray, dtype=np.float32)
    if a.shape[0] < 3 or a.shape[1] < 3:
        return 0.0
    lap = a[1:-1, :-2] + a[1:-1, 2:] + a[:-2, 1:-1] + a[2:, 1:-1] - 4.0 * a[1:-1, 1:-1]
    return float(lap.var())


def orientation_of(img: Any) -> int:
    try:
        return int(img.getexif().get(0x0112, 1) or 1)
    except Exception:  # noqa: BLE001 - 壊れた EXIF は回転なし扱い
        return 1


def taken_at_of(img: Any) -> str | None:
    try:
        exif = img.getexif()
        sub = exif.get_ifd(0x8769)
        for tag in (0x9003, 0x9004):
            t = parse_exif_datetime(sub.get(tag))
            if t:
                return t
    except Exception:  # noqa: BLE001 - 壊れた EXIF は「撮影日時なし」
        return None
    return None


def apply_orientation(img: Any, orientation: int) -> Any:
    pil = pil_image()

    op = _ORIENT_OPS.get(orientation)
    if op is None:
        return img
    return img.transpose(getattr(pil.Transpose, op))


def open_image(path: str, data: bytes | None = None) -> Any:
    """読み取り専用で開く(INV-3)。data があればメモリから開く。"""
    ensure_heif()
    pil = pil_image()

    src: Any = io.BytesIO(data) if data is not None else path
    return pil.open(src)


def work_gray(img: Any, side: int = WORK_SIDE) -> tuple[Any, int, int, int]:
    """(長辺 side 以下の白黒画像, 元の幅, 元の高さ, Orientation)。幅高さは回転を適用した後の値。"""
    pil = pil_image()

    w, h = img.size
    orient = orientation_of(img)
    if getattr(img, "format", None) == "JPEG" and w > 0 and h > 0:
        scale = side / max(w, h)
        if scale < 1:
            img.draft("L", (max(1, int(w * scale)), max(1, int(h * scale))))
    g = img.convert("L")
    if max(g.size) > side:
        g.thumbnail((side, side), pil.Resampling.BOX)
    g = apply_orientation(g, orient)
    if orient in (5, 6, 7, 8):
        w, h = h, w
    return g, int(w), int(h), orient


def compute_features(path: str, size: int, mtime_ns: int) -> Features:
    """1枚の特徴。読めない中身は dhash=None の Features。読んでいる間に変わった・消えたら FileChangedError。"""
    h = hashlib.sha256()
    data: bytes | None = None
    try:
        if size <= _STREAM_LIMIT:
            with open(path, "rb") as f:
                data = f.read()
            h.update(data)
        else:
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
        st = os.stat(path)
    except OSError:
        raise FileChangedError() from None
    if st.st_size != size or st.st_mtime_ns != mtime_ns:
        raise FileChangedError()
    digest = h.hexdigest()
    try:
        with open_image(path, data) as img:
            taken = taken_at_of(img)
            gray, w, hh, _o = work_gray(img)
        return Features(digest, dhash_of(gray), w, hh, taken, sharpness_of(gray))
    except Exception:  # noqa: BLE001 - 壊れている・対応していない中身(FR-7)。種類を問わず「読めない」にする
        return Features(digest, None, 0, 0, None, 0.0)
