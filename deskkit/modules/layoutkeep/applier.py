# 計画の「動かす」を SetWindowPlacement だけで適用する(INV-4)。適用前に対象全部の現在配置を undo.json に書き、
# 書けなければ1件も動かさない(INV-6)。dry_run では SetWindowPlacement を呼ばない(INV-7)。
# 通常表示は SW_SHOWNOACTIVATE で戻してフォーカスを奪わない(FR-16)。最大化は SW_SHOWMAXIMIZED しか無いので結果を記録する。
from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ._win32 import Win32Api
from .model import SW_SHOWMAXIMIZED, SW_SHOWNOACTIVATE, Placement, Rect, exe_basename, rect_of
from .planner import Plan, PlanItem
from .store import Store, StoreWriteError, now_iso


@dataclass
class ApplyResult:
    status: str  # "applied" | "dry_run" | "nothing" | "undo_failed"
    moved: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    focus_kept: bool | None = None
    message: str = ""
    moved_indexes: list[int] = field(default_factory=list)


def placement_for(show: str, rect: Rect, current: Placement | None) -> Placement:
    """保存した show / normal_rect から、適用に使う WINDOWPLACEMENT を作る。"""
    cmd = SW_SHOWMAXIMIZED if show == "maximized" else SW_SHOWNOACTIVATE
    min_pos = current.min_pos if current else (-1, -1)
    max_pos = current.max_pos if current else (-1, -1)
    return Placement(show_cmd=cmd, normal_rect=rect, min_pos=min_pos, max_pos=max_pos, flags=0)


def _same_window(api: Win32Api, hwnd: int, pid: int | None, proc_start: int | None) -> bool:
    if not api.is_window(hwnd):
        return False
    raw = api.describe_window(hwnd)
    if raw is None or raw.pid != pid:
        return False
    return proc_start is None or raw.proc_start == proc_start


def _undo_row(item: PlanItem, cur: Placement) -> dict[str, Any]:
    return {"exe": item.exe, "class": item.cls, "hwnd": item.hwnd, "pid": item.pid, "proc_start": item.proc_start,
            "show": cur.show if cur.show != "minimized" else "normal",
            "normal_rect": list(item.before_rect if item.before_rect is not None else cur.normal_rect)}


class Applier:
    def __init__(self, api: Win32Api, store: Store) -> None:
        self.api = api
        self.store = store

    def apply(self, plan: Plan, *, dry_run: bool, append_undo: bool = False,
              only: Sequence[int] | None = None) -> ApplyResult:
        """only を渡すと、その添字の項目だけ動かす(wait_s の再試行用)。append_undo で既存の undo.json に足す。"""
        moves = [m for m in plan.moves if only is None or m.index in only]
        skipped = dict(plan.skipped())
        if dry_run:
            return ApplyResult("dry_run", 0, skipped, message="試運転")
        if not moves:
            return ApplyResult("nothing", 0, skipped)
        # 1) 対象全部の現在配置を取って undo.json に書く(書けなければ中止: INV-6)
        rows: list[dict[str, Any]] = []
        currents: dict[int, Placement] = {}
        extra: Counter[str] = Counter()
        ready: list[PlanItem] = []
        for m in moves:
            assert m.hwnd is not None
            cur = self.api.get_placement(m.hwnd)
            if cur is None:  # 今の配置が取れない物は undo に残せないので動かさない
                extra["excluded:no_placement"] += 1
                continue
            currents[m.index] = cur
            rows.append(_undo_row(m, cur))
            ready.append(m)
        if not ready:
            for k, v in extra.items():
                skipped[k] = skipped.get(k, 0) + v
            return ApplyResult("nothing", 0, skipped)
        data: dict[str, Any] = {"schema": 1, "signature": plan.signature, "created_at": now_iso(), "windows": rows}
        if append_undo:
            prev = self.store.read_undo()
            if prev and prev.get("signature") == plan.signature:
                data["windows"] = list(prev.get("windows", [])) + rows
        try:
            self.store.write_undo(data)
        except StoreWriteError as e:
            return ApplyResult("undo_failed", 0, skipped, message=str(e))
        # 2) SetWindowPlacement で適用
        fg_before = self.api.foreground_window()
        moved = 0
        moved_idx: list[int] = []
        errors: list[dict[str, Any]] = []
        for m in ready:
            assert m.hwnd is not None
            if not _same_window(self.api, m.hwnd, m.pid, m.proc_start):
                extra["set_failed"] += 1
                errors.append({"exe": m.exe_name, "class": m.cls, "hwnd": m.hwnd, "win32_error": 1400})  # 閉じられた
                continue
            err = self.api.set_placement(m.hwnd, placement_for(m.after_show, m.after_rect, currents.get(m.index)))
            if err == 0:
                moved += 1
                moved_idx.append(m.index)
            else:
                extra["set_failed"] += 1
                errors.append({"exe": m.exe_name, "class": m.cls, "hwnd": m.hwnd, "win32_error": err})
        fg_after = self.api.foreground_window()
        for k, v in extra.items():
            skipped[k] = skipped.get(k, 0) + v
        return ApplyResult("applied", moved, skipped, errors, focus_kept=(fg_before == fg_after), moved_indexes=moved_idx)

    def undo(self, current_sig: str | None, *, dry_run: bool) -> ApplyResult:
        """undo.json の配置に戻し、空にする。構成が違えば戻さない。dry_run では何も動かさない。"""
        data = self.store.read_undo()
        if not data:
            return ApplyResult("nothing", message="元に戻せる適用がありません")
        if data.get("signature") != current_sig:
            return ApplyResult("sig_mismatch", message="構成が変わったため元に戻せません")
        rows = [r for r in data.get("windows", []) if isinstance(r, dict)]
        if dry_run:
            return ApplyResult("dry_run", 0, {}, message=f"試運転: {len(rows)} 件を元に戻す予定")
        fg_before = self.api.foreground_window()
        moved = 0
        skipped: Counter[str] = Counter()
        errors: list[dict[str, Any]] = []
        for r in rows:
            hwnd = int(r.get("hwnd") or 0)
            rect = rect_of(r.get("normal_rect"))
            ps = r.get("proc_start")
            exe_name = exe_basename(str(r.get("exe") or ""))
            if not hwnd or rect is None or not _same_window(self.api, hwnd, int(r.get("pid") or 0),
                                                              ps if isinstance(ps, int) else None):
                skipped["gone"] += 1
                continue
            cur = self.api.get_placement(hwnd)
            if cur is not None and cur.show == "minimized":
                skipped["excluded:minimized"] += 1
                continue
            err = self.api.set_placement(hwnd, placement_for(str(r.get("show") or "normal"), rect, cur))
            if err == 0:
                moved += 1
            else:
                skipped["set_failed"] += 1
                errors.append({"exe": exe_name, "class": str(r.get("class") or ""), "hwnd": hwnd, "win32_error": err})
        fg_after = self.api.foreground_window()
        try:
            self.store.clear_undo()
        except StoreWriteError as e:
            return ApplyResult("applied", moved, dict(skipped), errors, fg_before == fg_after, message=str(e))
        return ApplyResult("applied", moved, dict(skipped), errors, fg_before == fg_after)
