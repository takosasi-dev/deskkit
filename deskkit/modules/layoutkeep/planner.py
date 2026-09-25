# 対応付けの結果から適用計画(動かす / 動かさない + 理由 + 移動前後の矩形)を作る。純関数。
# 曖昧(M-5)・起動していない(M-2)・除外(M-6)・既に同じ位置・画面外は「動かさない」。
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .matcher import Match
from .model import RawMonitor, Rect, rects_intersect
from .monitors import workspace_to_screen
from .windows import EXCLUDE_LABELS, effective_normal_rect


@dataclass
class PlanItem:
    index: int
    exe: str
    exe_name: str
    cls: str
    rule: str
    action: str  # "move" | "skip"
    key: str  # "move" | "unchanged" | "not_running" | "ambiguous" | "excluded:<種類>"
    reason: str
    hwnd: int | None = None
    pid: int | None = None
    proc_start: int | None = None
    before_show: str | None = None
    before_rect: Rect | None = None
    before_screen: Rect | None = None
    after_show: str = "normal"
    after_rect: Rect = (0, 0, 0, 0)
    after_screen: Rect | None = None

    def log_dict(self) -> dict[str, Any]:
        """ログ・oplog 用(タイトルを含めない。exe はファイル名だけ)。"""
        return {"exe": self.exe_name, "class": self.cls, "hwnd": self.hwnd, "rule": self.rule, "action": self.action,
                "key": self.key, "before": [self.before_show, list(self.before_rect) if self.before_rect else None],
                "after": [self.after_show, list(self.after_rect)]}


@dataclass
class Plan:
    signature: str
    items: list[PlanItem] = field(default_factory=list)

    @property
    def moves(self) -> list[PlanItem]:
        return [i for i in self.items if i.action == "move"]

    def skipped(self) -> dict[str, int]:
        c = Counter(i.key for i in self.items if i.action == "skip")
        return dict(sorted(c.items()))

    def ambiguous_groups(self) -> list[tuple[str, str, int]]:
        """(exe ファイル名, クラス, 枚数)。通知文「Chrome のウィンドウ 2 枚は区別できず…」用。"""
        g: dict[tuple[str, str], int] = defaultdict(int)
        for i in self.items:
            if i.key == "ambiguous":
                g[(i.exe_name, i.cls)] += 1
        return [(e, c, n) for (e, c), n in g.items()]


REASONS = {
    "M-1": "強い一致(hwnd・pid・起動時刻が同じ)",
    "M-3": "同じアプリのウィンドウが1枚だけ",
    "M-4": "タイトル正規表現で1対1に対応",
    "M-2": "起動していない(起動はしません)",
    "M-5": "同じアプリのウィンドウを区別できない",
}


def _on_screen(r: Rect, monitors: Sequence[RawMonitor]) -> bool:
    scr = workspace_to_screen(r, monitors)
    return any(rects_intersect(scr, m.rect) for m in monitors)


def make_plan(signature: str, matches: Sequence[Match], monitors: Sequence[RawMonitor]) -> Plan:
    plan = Plan(signature)
    for m in matches:
        e = m.entry
        item = PlanItem(index=m.index, exe=e.exe, exe_name=e.exe_name, cls=e.cls, rule=m.rule, action="skip", key=m.status,
                        reason="", after_show=e.show, after_rect=e.normal_rect, after_screen=e.screen_rect)
        w = m.window
        if w is not None:
            item.hwnd, item.pid, item.proc_start = w.hwnd, w.pid, w.raw.proc_start
            item.before_screen = w.raw.screen_rect
            if w.raw.placement is not None:
                item.before_show = w.raw.placement.show
                # スナップ中の窓は今の見た目の矩形で比べる(保存側と同じ規則)
                item.before_rect = effective_normal_rect(w.raw, monitors)[0]
        if m.status == "matched":
            if not _on_screen(e.normal_rect, monitors):
                item.key = "excluded:offscreen"
                item.reason = "保存した位置がどのモニタとも重ならない"
            elif item.before_show == e.show and item.before_rect == e.normal_rect:
                item.key = "unchanged"
                item.reason = f"既に保存した位置にある({m.rule})"
            else:
                item.action = "move"
                item.key = "move"
                item.reason = REASONS.get(m.rule, m.rule)
        elif m.status.startswith("excluded:"):
            kind = m.status.split(":", 1)[1]
            item.reason = f"除外: {EXCLUDE_LABELS.get(kind, kind)}"
        else:
            item.reason = REASONS.get(m.rule, m.status)
        plan.items.append(item)
    return plan


def summary_text(plan: Plan, *, dry_run: bool, moved: int | None = None) -> str:
    """通知用の要約(タイトルを含めない)。"""
    skipped = sum(plan.skipped().values()) - plan.skipped().get("unchanged", 0)
    n = len(plan.moves) if moved is None else moved
    head = f"試運転: {n} 件を動かす予定" if dry_run else f"{n} 件戻しました"
    parts = [head]
    if skipped:
        parts.append(f"{skipped} 件は動かしていません")
    unchanged = plan.skipped().get("unchanged", 0)
    if unchanged:
        parts.append(f"{unchanged} 件は既に元の位置")
    text = " / ".join(parts)
    for exe, _cls, k in plan.ambiguous_groups():
        name = exe.rsplit(".", 1)[0] if exe else "?"
        text += f"\n{name} のウィンドウ {k} 枚は区別できず動かしていません"
    return text
