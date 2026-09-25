# スキャン結果の状態(グループの一覧と、写真ごとの「残す」「ごみ箱へ」)。G-5・INV-2 をここで守る:
# どのグループにも「残す」が1枚以上ある状態からしか切り替えを許さず、送る直前にも全グループを確かめる。
# 写真は完全一致のグループと似ているグループの両方に入ることがある(同じ Photo を共有し、状態も1つ)。
from __future__ import annotations

import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from deskkit.modules.twinsweep.grouping import LEVELS, Photo, similar_groups
from deskkit.modules.twinsweep.scoring import pick_keep

KIND_EXACT = "exact"
KIND_SIMILAR = "similar"


@dataclass(eq=False)
class Group:
    gid: int
    kind: str
    photos: list[Photo]
    recommended: int     # pid
    reason: str


@dataclass
class ResultModel:
    photos: dict[int, Photo] = field(default_factory=dict)       # グループ分けの材料(読めた写真すべて)
    exact_sets: list[list[Photo]] = field(default_factory=list)  # 完全一致(しきい値を変えても作り直さない)
    groups: list[Group] = field(default_factory=list)
    keep: dict[int, bool] = field(default_factory=dict)          # グループに入っている写真だけ
    member_of: dict[int, list[Group]] = field(default_factory=dict)
    level: str = "normal"
    exact_only: bool = False

    # ------------------------------------------------------------ 組み立て
    @classmethod
    def build(cls, photos: Iterable[Photo], exact_sets: Sequence[Sequence[Photo]], level: str, exact_only: bool,
              cancel: threading.Event | None = None) -> ResultModel:
        m = cls(photos={p.pid: p for p in photos}, exact_sets=[list(s) for s in exact_sets], level=level,
                exact_only=exact_only)
        m._regroup(cancel)
        return m

    def rebuild(self, level: str, exact_only: bool, cancel: threading.Event | None = None) -> ResultModel:
        """FR-16: 特徴を調べ直さずにグループを作り直した新しいモデル(選択は初期状態に戻る)。"""
        return ResultModel.build(self.photos.values(), self.exact_sets, level, exact_only, cancel)

    def _regroup(self, cancel: threading.Event | None) -> None:
        gid = 0
        exact: list[Group] = []
        forced_keep: set[int] = set()
        in_exact_non_keep: set[int] = set()
        for s in self.exact_sets:
            live = [p for p in s if p.pid in self.photos]
            if len(live) < 2:
                continue
            best, reason = pick_keep(live)
            exact.append(Group(gid, KIND_EXACT, live, best.pid, reason))
            gid += 1
            forced_keep.add(best.pid)
            in_exact_non_keep.update(p.pid for p in live if p is not best)
        similar: list[Group] = []
        if not self.exact_only:
            pool = [p for p in self.photos.values() if p.pid not in in_exact_non_keep]
            for members in similar_groups(pool, LEVELS.get(self.level, LEVELS["normal"]), cancel):
                best, reason = pick_keep(members)
                similar.append(Group(gid, KIND_SIMILAR, members, best.pid, reason))
                gid += 1
        keep: dict[int, bool] = {}
        for g in exact + similar:
            for p in g.photos:
                want = p.pid == g.recommended or p.pid in forced_keep
                keep[p.pid] = keep.get(p.pid, False) or want
        self.keep = keep
        # FR-8: それぞれの見出しの中で「減らせる大きさ」の大きい順
        exact.sort(key=lambda g: (-self.reducible(g), g.gid))
        similar.sort(key=lambda g: (-self.reducible(g), g.gid))
        self.groups = exact + similar
        self.member_of = {}
        for g in self.groups:
            for p in g.photos:
                self.member_of.setdefault(p.pid, []).append(g)

    # ------------------------------------------------------------ 参照
    def reducible(self, g: Group) -> int:
        return sum(p.size for p in g.photos if not self.keep.get(p.pid, True))

    def exact_groups(self) -> list[Group]:
        return [g for g in self.groups if g.kind == KIND_EXACT]

    def similar_groups(self) -> list[Group]:
        return [g for g in self.groups if g.kind == KIND_SIMILAR]

    def is_keep(self, pid: int) -> bool:
        return self.keep.get(pid, True)

    def selected(self) -> list[Photo]:
        """「ごみ箱へ」の写真(重複なし)。"""
        return [self.photos[pid] for pid, k in self.keep.items() if not k and pid in self.photos]

    def selected_bytes(self) -> int:
        return sum(p.size for p in self.selected())

    # ------------------------------------------------------------ G-5
    def can_trash(self, pid: int) -> bool:
        """pid を「ごみ箱へ」にしても、pid が入っているどのグループにも「残す」が1枚以上残るか。"""
        groups = self.member_of.get(pid, [])
        if not groups:
            return False
        return all(any(q.pid != pid and self.keep.get(q.pid, True) for q in g.photos) for g in groups)

    def set_keep(self, pid: int, keep: bool) -> bool:
        """切り替える。グループの最後の「残す」を「ごみ箱へ」にしようとしたら切り替えずに False(G-5)。"""
        if pid not in self.keep:
            return False
        if not keep and self.keep[pid] and not self.can_trash(pid):
            return False
        self.keep[pid] = keep
        return True

    def invariant_ok(self) -> bool:
        """INV-2: どのグループにも「残す」が1枚以上ある。"""
        return all(any(self.keep.get(p.pid, True) for p in g.photos) for g in self.groups)

    # ------------------------------------------------------------ 送ったあと(FR-15)
    def remove_photos(self, pids: Iterable[int]) -> None:
        gone = set(pids)
        if not gone:
            return
        for pid in gone:
            self.photos.pop(pid, None)
            self.keep.pop(pid, None)
            self.member_of.pop(pid, None)
        self.exact_sets = [[p for p in s if p.pid not in gone] for s in self.exact_sets]
        kept: list[Group] = []
        for g in self.groups:
            g.photos = [p for p in g.photos if p.pid not in gone]
            if len(g.photos) >= 2:
                kept.append(g)
        self.groups = kept
        live = {p.pid for g in kept for p in g.photos}
        for pid in list(self.keep):
            if pid not in live:
                self.keep.pop(pid, None)
                self.member_of.pop(pid, None)
        for pid, gs in list(self.member_of.items()):
            self.member_of[pid] = [g for g in gs if g in kept]
