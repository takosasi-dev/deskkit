# 軽くする(P-10)。対象は 8 ビット・64×64 画素以上・色が DeviceRGB/DeviceGray/ICCBased(1・3)/Indexed・圧縮が DCT/Flate/LZW/無し、
# マスクの無い画像。長辺の上限 = その画像を使うページのうち最大の長辺(インチ)× dpi。超えれば縮めて JPEG にし、
# 元の 90% 未満になったときだけ入れ替える。最後に同じ中身のオブジェクトをまとめ、ページの中身を圧縮する。
from __future__ import annotations

import io
import math
from dataclasses import dataclass
from typing import Any

from deskkit.modules.pagepress.build import PHASE_BUILD, Control

LEVELS: dict[str, tuple[int, int]] = {"light": (200, 85), "normal": (150, 75), "strong": (100, 60)}
MIN_SIDE = 64
REPLACE_RATIO = 0.9
_FILTERS = frozenset({"/DCTDecode", "/FlateDecode", "/LZWDecode"})


@dataclass
class Stats:
    candidates: int = 0
    replaced: int = 0


def _obj(v: Any) -> Any:
    try:
        return v.get_object()
    except AttributeError:
        return v


def _page_long_inches(page: Any) -> float:
    try:
        mb = page.mediabox
        uu = float(_obj(page.get("/UserUnit")) or 1.0)
        return max(float(mb.width), float(mb.height)) * uu / 72.0
    except Exception:  # noqa: BLE001
        return 11.69


def _collect(res: Any, long_in: float, out: dict[int, float], seen: set[int]) -> None:
    from pypdf.generic import DictionaryObject, IndirectObject

    res = _obj(res)
    if not isinstance(res, DictionaryObject):
        return
    xo = _obj(res.get("/XObject"))
    if not isinstance(xo, DictionaryObject):
        return
    for name in list(xo.keys()):
        ref = xo.raw_get(name)
        if not isinstance(ref, IndirectObject):
            continue
        o = _obj(ref)
        if not isinstance(o, DictionaryObject):
            continue
        st = o.get("/Subtype")
        if st == "/Image":
            out[ref.idnum] = max(out.get(ref.idnum, 0.0), long_in)
        elif st == "/Form" and ref.idnum not in seen:
            seen.add(ref.idnum)
            _collect(o.get("/Resources"), long_in, out, seen)
            seen.discard(ref.idnum)


def image_targets(writer: Any) -> dict[int, float]:
    """画像の idnum → それを使うページのうち最大の長辺(インチ)。"""
    out: dict[int, float] = {}
    for page in writer.pages:
        _collect(page.get("/Resources"), _page_long_inches(page), out, set())
    return out


def _filters(o: Any) -> list[str] | None:
    from pypdf.generic import ArrayObject

    f = _obj(o.get("/Filter"))
    if f is None:
        return []
    names = [str(_obj(x)) for x in f] if isinstance(f, ArrayObject) else [str(f)]
    if len(names) > 1 or any(n not in _FILTERS for n in names):
        return None
    return names


def _colorspace(cs: Any) -> tuple[str, int, Any] | None:
    """(種類, 成分の数, 入れ替え後の色空間)。種類は "gray" / "rgb" / "indexed"。対象外なら None。"""
    from pypdf.generic import ArrayObject, NameObject

    cs = _obj(cs)
    if isinstance(cs, NameObject):
        if cs == "/DeviceRGB":
            return "rgb", 3, NameObject("/DeviceRGB")
        if cs == "/DeviceGray":
            return "gray", 1, NameObject("/DeviceGray")
        return None
    if isinstance(cs, ArrayObject) and cs:
        kind = _obj(cs[0])
        if kind == "/ICCBased" and len(cs) >= 2:
            n = int(_obj(_obj(cs[1]).get("/N")) or 0)
            if n == 3:
                return "rgb", 3, cs
            if n == 1:
                return "gray", 1, cs
            return None
        if kind == "/Indexed" and len(cs) >= 4:
            base = _colorspace(cs[1])
            if base is None or base[0] == "indexed":
                return None
            return "indexed", base[1], base[2]
    return None


def _encoded_len(o: Any) -> int:
    try:
        n = int(_obj(o.get("/Length")))
        if n > 0:
            return n
    except (TypeError, ValueError):
        pass
    return len(getattr(o, "_data", b"") or b"")


def _decode(o: Any, filt: list[str], cs: tuple[str, int, Any]) -> Any | None:
    """PIL の画像(L か RGB)。読めなければ None。"""
    from PIL import Image

    w, h = int(_obj(o["/Width"])), int(_obj(o["/Height"]))
    raw = o.get_data()
    if filt == ["/DCTDecode"]:
        if cs[0] == "indexed":
            return None
        im = Image.open(io.BytesIO(raw))
        im.load()
        if im.mode not in ("L", "RGB") or im.size != (w, h):
            return None
        return im
    kind, comps, _ = cs
    if kind == "indexed":
        need = w * h
        if len(raw) < need:
            return None
        csa = _obj(o["/ColorSpace"])
        hival = int(_obj(csa[2]))
        lut = _obj(csa[3])
        table: bytes
        if hasattr(lut, "get_data"):
            table = lut.get_data()
        elif getattr(lut, "original_bytes", None) is not None:
            table = bytes(lut.original_bytes)
        elif isinstance(lut, str):
            table = lut.encode("latin-1")
        else:
            table = bytes(lut)
        want = (hival + 1) * comps
        if len(table) < want:
            return None
        table = table[:want]
        if comps == 1:
            table = bytes(b for v in table for b in (v, v, v))
        pim = Image.frombytes("P", (w, h), raw[:need])
        pim.putpalette(table)
        return pim.convert("L" if comps == 1 else "RGB")
    need = w * h * comps
    if len(raw) < need:
        return None
    return Image.frombytes("L" if comps == 1 else "RGB", (w, h), raw[:need])


def _eligible(o: Any) -> tuple[list[str], tuple[str, int, Any]] | None:
    if _obj(o.get("/BitsPerComponent")) != 8:
        return None
    if any(k in o for k in ("/SMask", "/Mask", "/Decode", "/SMaskInData")) or _obj(o.get("/ImageMask")):
        return None
    try:
        w, h = int(_obj(o["/Width"])), int(_obj(o["/Height"]))
    except (KeyError, TypeError, ValueError):
        return None
    if w < MIN_SIDE or h < MIN_SIDE:
        return None
    filt = _filters(o)
    if filt is None:
        return None
    cs = _colorspace(o.get("/ColorSpace"))
    if cs is None:
        return None
    return filt, cs


def _replace(o: Any, jpeg: bytes, size: tuple[int, int], colorspace: Any) -> None:
    from pypdf.generic import NameObject, NumberObject, StreamObject

    for k in ("/Filter", "/DecodeParms", "/Decode", "/Length"):
        if k in o:
            del o[k]
    StreamObject.set_data(o, jpeg)  # 符号化済みのバイト列として持たせる(EncodedStreamObject.set_data は Flate しか受けない)
    if hasattr(o, "decoded_self"):
        o.decoded_self = None
    o[NameObject("/Filter")] = NameObject("/DCTDecode")
    o[NameObject("/Width")] = NumberObject(size[0])
    o[NameObject("/Height")] = NumberObject(size[1])
    o[NameObject("/BitsPerComponent")] = NumberObject(8)
    o[NameObject("/ColorSpace")] = colorspace


def shrink_images(writer: Any, level: str, ctl: Control) -> Stats:
    from PIL import Image

    dpi, quality = LEVELS.get(level, LEVELS["normal"])
    targets = image_targets(writer)
    stats = Stats()
    total = len(targets)
    ctl.progress(PHASE_BUILD, 0, total)
    for k, (idnum, long_in) in enumerate(targets.items()):
        ctl.check()
        try:
            o = writer.get_object(idnum)
            el = _eligible(o)
            if el is None:
                continue
            stats.candidates += 1
            filt, cs = el
            w, h = int(_obj(o["/Width"])), int(_obj(o["/Height"]))
            limit = max(1, math.ceil(long_in * dpi))
            if max(w, h) <= limit:
                continue
            im = _decode(o, filt, cs)
            if im is None:
                continue
            k_ = limit / max(w, h)
            size = (max(1, round(w * k_)), max(1, round(h * k_)))
            small = im.resize(size, Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            small.save(buf, "JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            if len(data) < _encoded_len(o) * REPLACE_RATIO:
                _replace(o, data, size, cs[2])
                stats.replaced += 1
        except (MemoryError, KeyboardInterrupt):
            raise
        except Exception:  # noqa: BLE001 - 読めない画像は触らない(そのまま残す)
            continue
        finally:
            ctl.progress(PHASE_BUILD, k + 1, total)
    return stats


def finish(writer: Any) -> None:
    """全段階で行う可逆の処理(P-10)。"""
    writer.compress_identical_objects()
    for page in writer.pages:
        try:
            page.compress_content_streams()
        except Exception:  # noqa: BLE001 - 中身の圧縮に失敗したページはそのまま
            continue
