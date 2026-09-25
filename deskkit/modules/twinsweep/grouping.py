# 写真のグループ分け。完全一致(大きさ → SHA-256、同じ実体のハードリンクは1つにまとめる。G-2・§10)と、
# 似ているもの(撮影日時の古い順に、各グループの代表とのハミング距離で振り分ける。縦横比が 10% 以上違えば別。G-3)。
# ハミング距離は numpy で「1枚対全代表」をまとめて計算する。パスは比較にだけ使い、どこにも書かない。
from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from deskkit.modules.twinsweep.hashing import numpy

LEVELS: dict[str, int] = {"strict": 4, "normal": 8, "loose": 12}  # G-1
ASPECT_TOLERANCE = 0.10


@dataclass(eq=False)
class Photo:
    pid: int
    path: str
    root: str
    size: int
    mtime_ns: int
    sha256: str
    dhash: int
    width: int
    height: int
    taken_at: str | None
    sharpness: float
    file_key: tuple[int, int] | None = field(default=None)

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height > 0 else 0.0

    @property
    def when(self) -> float:
        """撮影日時(無ければ更新日時)の UNIX 秒。"""
        if self.taken_at:
            try:
                return datetime.fromisoformat(self.taken_at).timestamp()
            except (ValueError, OSError, OverflowError):
                pass
        return self.mtime_ns / 1e9


def aspect_close(a: float, b: float, tol: float = ASPECT_TOLERANCE) -> bool:
    if a <= 0 or b <= 0:
        return False
    hi, lo = max(a, b), min(a, b)
    return hi / lo - 1.0 < tol


def exact_groups(photos: Sequence[Photo], file_key: Callable[[Photo], tuple[int, int] | None]) -> tuple[list[list[Photo]], set[int]]:
    """(完全一致のグループ, 同じ実体の2つ目以降として外した写真の pid)。file_key は候補にだけ呼ぶ(開かずに stat だけ)。"""
    by_size: dict[int, list[Photo]] = defaultdict(list)
    for p in photos:
        by_size[p.size].append(p)
    groups: list[list[Photo]] = []
    dropped: set[int] = set()
    for same_size in by_size.values():
        if len(same_size) < 2:
            continue
        by_sha: dict[str, list[Photo]] = defaultdict(list)
        for p in same_size:
            by_sha[p.sha256].append(p)
        for members in by_sha.values():
            if len(members) < 2:
                continue
            uniq: list[Photo] = []
            keys: set[tuple[int, int]] = set()
            for p in members:
                k = file_key(p)
                p.file_key = k
                if k is not None and k in keys:
                    dropped.add(p.pid)  # 同じ実体(ハードリンク): 消しても空きが増えないので並べない
                    continue
                if k is not None:
                    keys.add(k)
                uniq.append(p)
            if len(uniq) >= 2:
                groups.append(uniq)
    return groups, dropped


def similar_groups(photos: Sequence[Photo], threshold: int, cancel: threading.Event | None = None) -> list[list[Photo]]:
    """G-3。古い順に1枚ずつ、代表との距離がしきい値以下かつ縦横比が近い既存グループのうち、いちばん近いものに入れる。
    どこにも入らなければ新しいグループ(自分が代表)。2枚以上のグループだけを返す。"""
    np = numpy()

    order = sorted(photos, key=lambda p: (p.when, p.path))
    n = len(order)
    if n < 2:
        return []
    rep_hash = np.zeros(n, dtype=np.uint64)
    rep_aspect = np.zeros(n, dtype=np.float64)
    members: list[list[Photo]] = []
    count = 0
    for i, p in enumerate(order):
        if cancel is not None and i % 1024 == 0 and cancel.is_set():
            return []
        placed = False
        if count:
            d = np.bitwise_count(rep_hash[:count] ^ np.uint64(p.dhash))
            ok = d <= threshold
            if p.aspect > 0:
                ra = rep_aspect[:count]
                hi = np.maximum(ra, p.aspect)
                lo = np.minimum(ra, p.aspect)
                with np.errstate(divide="ignore", invalid="ignore"):
                    ok &= (lo > 0) & (hi / np.where(lo > 0, lo, 1.0) - 1.0 < ASPECT_TOLERANCE)
            else:
                ok &= False
            if ok.any():
                cand = np.where(ok, d, 255)
                j = int(np.argmin(cand))  # 同じ距離なら古いグループ(先に作られた方)
                members[j].append(p)
                placed = True
        if not placed:
            rep_hash[count] = np.uint64(p.dhash)
            rep_aspect[count] = p.aspect
            members.append([p])
            count += 1
    return [g for g in members if len(g) >= 2]
