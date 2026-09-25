# 現在のモニタ構成を Win32Api から取り、構成シグネチャ(§9.3)を計算する。
# id が取れないモニタが1台でもあればシグネチャは None(デバイス名で代用しない)。
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from ._win32 import Win32Api
from .model import DEFAULT_FIELDS, RawMonitor, SigResult


def normalize(monitors: Sequence[RawMonitor], id_source: str,
              fields: Sequence[str] = DEFAULT_FIELDS) -> tuple[list[dict[str, Any]] | None, str | None]:
    """各モニタを {id, w, h, x, y, dpi, primary} にし、fields だけ残して id 昇順に並べる。"""
    if not monitors:
        return None, "モニタが1台も列挙できません"
    primary = next((m for m in monitors if m.primary), None)
    if primary is None:
        return None, "主モニタが見つかりません"
    ox, oy = primary.rect[0], primary.rect[1]
    items: list[dict[str, Any]] = []
    for m in monitors:
        mid = m.ids.get(id_source)
        if not mid:
            return None, f"{m.device} の ID({id_source})が取れません"
        if m.dpi is None:
            return None, f"{m.device} の DPI が取れません"
        full = {"id": mid, "w": m.width, "h": m.height, "x": m.rect[0] - ox, "y": m.rect[1] - oy,
                "dpi": int(m.dpi), "primary": bool(m.primary)}
        items.append({k: full[k] for k in DEFAULT_FIELDS if k in fields})

    def key(d: dict[str, Any]) -> tuple[str, str]:
        return (str(d["id"]), json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False))

    items.sort(key=key)
    return items, None


def signature_of(normalized: Sequence[dict[str, Any]]) -> str:
    text = json.dumps(list(normalized), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def compute(monitors: Sequence[RawMonitor], id_source: str, fields: Sequence[str] = DEFAULT_FIELDS) -> SigResult:
    norm, reason = normalize(monitors, id_source, fields)
    if norm is None:
        return SigResult(None, reason, list(monitors), [])
    return SigResult(signature_of(norm), None, list(monitors), norm)


def current(api: Win32Api, id_source: str, fields: Sequence[str] = DEFAULT_FIELDS) -> SigResult:
    return compute(api.enum_monitors(), id_source, fields)


def workspace_offset(monitors: Sequence[RawMonitor]) -> tuple[int, int]:
    """ワークスペース座標 → スクリーン座標のずれ(主モニタの rcWork 左上 − rcMonitor 左上)。"""
    p = next((m for m in monitors if m.primary), None)
    return (p.work[0] - p.rect[0], p.work[1] - p.rect[1]) if p else (0, 0)


def primary_work_origin(monitors: Sequence[RawMonitor]) -> tuple[int, int]:
    """ワークスペース座標 → スクリーン座標のずれ(主モニタの作業領域の左上)。"""
    p = next((m for m in monitors if m.primary), None)
    return (p.work[0], p.work[1]) if p else (0, 0)
