# 画像のメタデータ除去と、除去できたかの検証(S-1・S-2・FR-9)。バイト列だけを扱い、画素は再エンコードしない。
# JPEG はセグメント、PNG はチャンク、WebP は RIFF チャンクを書き換える。形式の判定(先頭のバイト)もここに置く。
# Pillow を import しない(NFR-5: モジュールの読み込みを軽く保つ)。
from __future__ import annotations

import struct
from dataclasses import dataclass


class MetadataError(Exception):
    """構造を読めない(壊れている・途中で切れている)。"""


# ------------------------------------------------------------------ 形式の判定
HEIF_BRANDS = frozenset({b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs"})


def detect(data: bytes) -> str | None:
    """先頭のバイトから画像の形式を返す: jpeg / png / webp / gif / bmp / heif。分からなければ None。"""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:2] == b"BM" and len(data) > 26:
        return "bmp"
    if data[4:8] == b"ftyp" and len(data) >= 16:
        size = struct.unpack(">I", data[:4])[0]
        box = data[8:min(max(size, 16), 256)]
        brands = {box[i:i + 4] for i in range(0, len(box) - 3, 4) if i != 4}  # major + compatible(minor_version は除く)
        if brands & HEIF_BRANDS:
            return "heif"
    return None


# ------------------------------------------------------------------ TIFF(Exif)の最小限の読み書き
ORIENTATION_TAG = 0x0112


def _tiff_ifd0(tiff: bytes) -> tuple[dict[int, tuple[int, int, bytes]], int]:
    """IFD0 の {tag: (type, count, value_or_offset 4 バイト)} と次の IFD の位置。"""
    if len(tiff) < 8 or tiff[:2] not in (b"II", b"MM"):
        raise MetadataError("TIFF ヘッダが読めません")
    e = "<" if tiff[:2] == b"II" else ">"
    if struct.unpack(e + "H", tiff[2:4])[0] != 42:
        raise MetadataError("TIFF ヘッダが読めません")
    off = struct.unpack(e + "I", tiff[4:8])[0]
    if off + 2 > len(tiff):
        raise MetadataError("IFD0 が範囲外")
    n = struct.unpack(e + "H", tiff[off:off + 2])[0]
    end = off + 2 + n * 12
    if end + 4 > len(tiff):
        raise MetadataError("IFD0 が範囲外")
    entries: dict[int, tuple[int, int, bytes]] = {}
    for i in range(n):
        p = off + 2 + i * 12
        tag, typ, cnt = struct.unpack(e + "HHI", tiff[p:p + 8])
        entries[tag] = (typ, cnt, tiff[p + 8:p + 12])
    nxt = struct.unpack(e + "I", tiff[end:end + 4])[0]
    return entries, nxt


def exif_orientation(exif_payload: bytes) -> int:
    """APP1 の中身("Exif\\0\\0" + TIFF)から Orientation(1〜8)。無い・読めないときは 1。"""
    if not exif_payload.startswith(b"Exif\x00\x00"):
        return 1
    tiff = exif_payload[6:]
    try:
        entries, _ = _tiff_ifd0(tiff)
    except (MetadataError, struct.error):
        return 1
    ent = entries.get(ORIENTATION_TAG)
    if ent is None or ent[0] != 3 or ent[1] != 1:
        return 1
    e = "<" if tiff[:2] == b"II" else ">"
    v = int(struct.unpack(e + "H", ent[2][:2])[0])
    return v if 1 <= v <= 8 else 1


def minimal_exif(orientation: int) -> bytes:
    """Orientation だけを持つ Exif(APP1 の中身)。"""
    tiff = b"MM\x00\x2a" + struct.pack(">I", 8) + struct.pack(">H", 1)
    tiff += struct.pack(">HHI", ORIENTATION_TAG, 3, 1) + struct.pack(">HH", orientation, 0) + struct.pack(">I", 0)
    return b"Exif\x00\x00" + tiff


def is_orientation_only_exif(exif_payload: bytes) -> bool:
    if not exif_payload.startswith(b"Exif\x00\x00"):
        return False
    try:
        entries, nxt = _tiff_ifd0(exif_payload[6:])
    except (MetadataError, struct.error):
        return False
    return set(entries) == {ORIENTATION_TAG} and entries[ORIENTATION_TAG][:2] == (3, 1) and nxt == 0


# ------------------------------------------------------------------ JPEG(S-1)
SOI, EOI, SOS = 0xD8, 0xD9, 0xDA
APP0, APP1, APP2, APP13, APP14, COM = 0xE0, 0xE1, 0xE2, 0xED, 0xEE, 0xFE


@dataclass(frozen=True)
class Segment:
    marker: int | None  # None はスキャンの符号化データ
    raw: bytes          # マーカー(FF xx)から末尾までそのまま

    @property
    def payload(self) -> bytes:
        return self.raw[4:] if self.marker is not None and len(self.raw) >= 4 else b""


def jpeg_segments(data: bytes) -> tuple[list[Segment], bytes]:
    """SOI〜EOI をセグメントに分ける。戻り値は (セグメント, EOI の後ろに付いていたデータ)。"""
    if data[:2] != b"\xff\xd8":
        raise MetadataError("JPEG ではありません")
    segs: list[Segment] = [Segment(SOI, data[:2])]
    pos, n = 2, len(data)
    while pos < n:
        if data[pos] != 0xFF:
            raise MetadataError("マーカーが見つかりません")
        while pos < n and data[pos] == 0xFF:
            pos += 1
        if pos >= n:
            break
        marker = data[pos]
        pos += 1
        if marker == EOI:
            segs.append(Segment(EOI, b"\xff\xd9"))
            return segs, data[pos:]
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            segs.append(Segment(marker, b"\xff" + bytes([marker])))
            continue
        if pos + 2 > n:
            raise MetadataError("セグメントが途中で切れています")
        length = struct.unpack(">H", data[pos:pos + 2])[0]
        if length < 2 or pos + length > n:
            raise MetadataError("セグメントの長さが不正です")
        end = pos + length
        segs.append(Segment(marker, b"\xff" + bytes([marker]) + data[pos:end]))
        pos = end
        if marker == SOS:
            # 符号化データ: 次のマーカー(FF 00 と RSTn 以外)まで
            scan = pos
            while True:
                i = data.find(b"\xff", scan)
                if i < 0 or i + 1 >= n:
                    segs.append(Segment(None, data[pos:]))  # EOI の無い(途中で切れた)ファイルは末尾までを符号化データとみなす
                    segs.append(Segment(EOI, b"\xff\xd9"))
                    return segs, b""
                nb = data[i + 1]
                if nb == 0x00 or 0xD0 <= nb <= 0xD7:
                    scan = i + 2
                    continue
                if nb == 0xFF:
                    scan = i + 1
                    continue
                segs.append(Segment(None, data[pos:i]))
                pos = i
                break
    raise MetadataError("EOI がありません")


def _keep_app(seg: Segment) -> bool:
    """残す APP セグメント: APP0(JFIF/JFXX)・APP2(ICC)・APP14(Adobe)だけ。"""
    p = seg.payload
    if seg.marker == APP0:
        return p.startswith((b"JFIF\x00", b"JFXX\x00"))
    if seg.marker == APP2:
        return p.startswith(b"ICC_PROFILE\x00")
    if seg.marker == APP14:
        return p.startswith(b"Adobe")
    return False


def jpeg_orientation(data: bytes) -> int:
    segs, _ = jpeg_segments(data)
    for s in segs:
        if s.marker == SOS:
            break
        if s.marker == APP1 and s.payload.startswith(b"Exif\x00\x00"):
            return exif_orientation(s.payload)
    return 1


def strip_jpeg(data: bytes) -> bytes:
    """APP1(Exif・XMP)・APP13(IPTC)・COM・その他の APPn(MPF など)と EOI の後ろのデータ(付属画像)を消す。
    Orientation が 1 以外なら Orientation だけの Exif を入れ直す。画素のデータはそのまま(再エンコードしない)。"""
    segs, _trailer = jpeg_segments(data)
    orientation = 1
    for s in segs:
        if s.marker == SOS:
            break
        if s.marker == APP1 and s.payload.startswith(b"Exif\x00\x00"):
            orientation = exif_orientation(s.payload)
            break
    kept: list[bytes] = []
    for s in segs:
        m = s.marker
        if m is not None and (0xE0 <= m <= 0xEF) and not _keep_app(s):
            continue
        if m == COM:
            continue
        kept.append(s.raw)
    if orientation != 1:
        payload = minimal_exif(orientation)
        app1 = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
        # JFIF の APP0 は先頭に置く決まりなので、その直後に入れる
        idx = 1
        while idx < len(kept) and kept[idx][:2] == b"\xff\xe0":
            idx += 1
        kept.insert(idx, app1)
    return b"".join(kept)


def verify_jpeg(data: bytes) -> bool:
    """FR-9: APP1 は「Orientation だけの Exif」以外に無い・XMP/IPTC/COM が無い(ほかの APPn・付属データも無い)。"""
    try:
        segs, trailer = jpeg_segments(data)
    except (MetadataError, struct.error):
        return False
    if trailer.strip(b"\x00"):
        return False
    for s in segs:
        m = s.marker
        if m in (COM, APP13):
            return False
        if m == APP1:
            if not is_orientation_only_exif(s.payload):
                return False
            continue
        if m is not None and 0xE0 <= m <= 0xEF and not _keep_app(s):
            return False
    return True


# ------------------------------------------------------------------ PNG(S-2)
PNG_SIG = b"\x89PNG\r\n\x1a\n"
PNG_DROP = frozenset({b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME"})


def png_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """(種類, チャンク全体のバイト列) の一覧。IEND で終わる(後ろのデータは捨てる)。"""
    if data[:8] != PNG_SIG:
        raise MetadataError("PNG ではありません")
    pos, n = 8, len(data)
    out: list[tuple[bytes, bytes]] = []
    while pos + 12 <= n:
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        end = pos + 12 + length
        if end > n:
            raise MetadataError("チャンクが途中で切れています")
        out.append((typ, data[pos:end]))
        pos = end
        if typ == b"IEND":
            return out
    raise MetadataError("IEND がありません")


def strip_png(data: bytes) -> bytes:
    return PNG_SIG + b"".join(raw for typ, raw in png_chunks(data) if typ not in PNG_DROP)


def verify_png(data: bytes) -> bool:
    try:
        return not any(typ in PNG_DROP for typ, _ in png_chunks(data))
    except (MetadataError, struct.error):
        return False


# ------------------------------------------------------------------ WebP(S-2)
WEBP_DROP = frozenset({b"EXIF", b"XMP "})
VP8X_ICC, VP8X_ALPHA, VP8X_EXIF, VP8X_XMP, VP8X_ANIM = 0x20, 0x10, 0x08, 0x04, 0x02


def webp_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """(FourCC, チャンク全体(埋め草を含む)) の一覧。"""
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise MetadataError("WebP ではありません")
    riff_end = min(len(data), 8 + struct.unpack("<I", data[4:8])[0])
    pos = 12
    out: list[tuple[bytes, bytes]] = []
    while pos + 8 <= riff_end:
        fourcc = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        end = pos + 8 + size + (size & 1)
        if pos + 8 + size > riff_end:
            raise MetadataError("チャンクが途中で切れています")
        out.append((fourcc, data[pos:min(end, riff_end)]))
        pos = end
    if not out:
        raise MetadataError("チャンクがありません")
    return out


def webp_is_animated(data: bytes) -> bool:
    try:
        chunks = webp_chunks(data)
    except (MetadataError, struct.error):
        return False
    for fourcc, raw in chunks:
        if fourcc == b"VP8X" and len(raw) > 8 and raw[8] & VP8X_ANIM:
            return True
        if fourcc in (b"ANIM", b"ANMF"):
            return True
    return False


def strip_webp(data: bytes) -> bytes:
    body = bytearray()
    for fourcc, raw in webp_chunks(data):
        if fourcc in WEBP_DROP:
            continue
        if fourcc == b"VP8X" and len(raw) > 8:
            b = bytearray(raw)
            b[8] &= ~(VP8X_EXIF | VP8X_XMP) & 0xFF
            raw = bytes(b)
        body += raw
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + bytes(body)


def verify_webp(data: bytes) -> bool:
    try:
        chunks = webp_chunks(data)
    except (MetadataError, struct.error):
        return False
    for fourcc, raw in chunks:
        if fourcc in WEBP_DROP:
            return False
        if fourcc == b"VP8X" and len(raw) > 8 and raw[8] & (VP8X_EXIF | VP8X_XMP):
            return False
    return True


def verify(fmt: str, data: bytes) -> bool:
    """出力の形式ごとの検証(FR-9)。"""
    if fmt == "jpeg":
        return verify_jpeg(data)
    if fmt == "png":
        return verify_png(data)
    if fmt == "webp":
        return verify_webp(data)
    return False
