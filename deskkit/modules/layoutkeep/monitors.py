# 現在のモニタ構成を Win32Api から取り、構成シグネチャ(§9.3)を計算する。
# id が取れないモニタが1台でもあればシグネチャは None(デバイス名で代用しない)。
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from ._win32 import Win32Api
from .model import DEFAULT_FIELDS, RawMonitor, Rect, SigResult


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


def _overlap(a: Rect, b: Rect) -> int:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0


def monitor_for_rect(r: Rect, monitors: Sequence[RawMonitor]) -> RawMonitor | None:
    """MonitorFromRect(MONITOR_DEFAULTTOPRIMARY) 相当: 重なりが最大のモニタ。どれとも重ならなければ主モニタ。"""
    best: RawMonitor | None = None
    best_area = 0
    for m in monitors:
        a = _overlap(r, m.rect)
        if a > best_area:
            best, best_area = m, a
    if best is not None:
        return best
    return next((m for m in monitors if m.primary), None)


def work_offset(m: RawMonitor | None) -> tuple[int, int]:
    """そのモニタのワークスペース座標 → スクリーン座標のずれ(rcWork 左上 − rcMonitor 左上)。"""
    return (m.work[0] - m.rect[0], m.work[1] - m.rect[1]) if m is not None else (0, 0)


def _shift(r: Rect, d: tuple[int, int], sign: int) -> Rect:
    return (r[0] + sign * d[0], r[1] + sign * d[1], r[2] + sign * d[0], r[3] + sign * d[1])


# WINDOWPLACEMENT のワークスペース座標は、主モニタではなく「ウィンドウが載っているモニタ」の作業領域のずれで
# スクリーン座標とずれる(タスクバーを左・上に置いた副モニタでは副モニタ自身のずれ)。
# 取得(GetWindowPlacement)はスクリーン矩形の載るモニタのずれを引き、設定(SetWindowPlacement)は
# 渡した矩形そのもので MonitorFromRect したモニタのずれを足す(ReactOS の実装と PowerToys FancyZones の
# ScreenToWorkAreaCoords が同じ扱い)。境界付近で両者がずれる場合に備え、設定用の変換は2段で決める。
def screen_to_workspace(r: Rect, monitors: Sequence[RawMonitor]) -> Rect:
    """スクリーン矩形 → SetWindowPlacement に渡すワークスペース矩形。"""
    first = monitor_for_rect(r, monitors)
    ref = _shift(r, work_offset(first), -1)
    return _shift(r, work_offset(monitor_for_rect(ref, monitors)), -1)


def workspace_to_screen(r: Rect, monitors: Sequence[RawMonitor]) -> Rect:
    """ワークスペース矩形(rcNormalPosition)→ スクリーン矩形(SetWindowPlacement と同じ規則)。"""
    return _shift(r, work_offset(monitor_for_rect(r, monitors)), +1)


def placement_to_screen_candidates(r: Rect, monitors: Sequence[RawMonitor]) -> set[Rect]:
    """GetWindowPlacement の rcNormalPosition が指し得るスクリーン矩形(取得側・設定側の両方の規則)。"""
    out = {workspace_to_screen(r, monitors)}
    for m in monitors:
        s = _shift(r, work_offset(m), +1)
        if monitor_for_rect(s, monitors) is m:
            out.add(s)
    return out

