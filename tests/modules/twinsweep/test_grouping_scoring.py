# グループ分け(G-2・G-3)・「残す」候補(G-4)・選択の状態(G-5・INV-2・FR-15)のテスト。特徴を直接与える。
from __future__ import annotations

from deskkit.modules.twinsweep.grouping import Photo, aspect_close, exact_groups, similar_groups
from deskkit.modules.twinsweep.results import KIND_EXACT, KIND_SIMILAR, ResultModel
from deskkit.modules.twinsweep.scoring import REASON_OLDEST, REASON_PIXELS, REASON_SHARP, REASON_SIZE, pick_keep


def ph(pid: int, dhash: int, *, w: int = 400, h: int = 300, size: int = 50_000, mtime: int = 0, sha: str | None = None,
       sharp: float = 10.0, taken: str | None = None) -> Photo:
    return Photo(pid, f"C:\\p\\{pid}.jpg", "C:\\p", size, mtime or (1_700_000_000_000_000_000 + pid * 1_000_000_000),
                 sha or f"sha{pid}", dhash, w, h, taken, sharp)


def flip(v: int, n: int, start: int = 0) -> int:
    for i in range(start, start + n):
        v ^= 1 << i
    return v


def test_ac2_chain_does_not_join_a_and_c() -> None:
    a = 0
    b = flip(a, 6)            # A-B = 6
    c = flip(b, 6, start=6)   # B-C = 6、A-C = 12
    assert bin(a ^ c).count("1") == 12
    groups = similar_groups([ph(1, a), ph(2, b), ph(3, c)], 8)
    together = [sorted(p.pid for p in g) for g in groups]
    assert [1, 3] not in together
    assert not any(1 in g and 3 in g for g in together)
    assert [1, 2] in together


def test_threshold_levels() -> None:
    a = 0
    b = flip(a, 5)
    assert len(similar_groups([ph(1, a), ph(2, b)], 4)) == 0
    assert len(similar_groups([ph(1, a), ph(2, b)], 8)) == 1


def test_aspect_ratio_rule() -> None:
    assert aspect_close(4 / 3, 4 / 3)
    assert not aspect_close(4 / 3, 3 / 4)
    assert not aspect_close(1.0, 1.12)
    groups = similar_groups([ph(1, 0, w=400, h=300), ph(2, 0, w=300, h=400)], 8)
    assert groups == []  # 横長と縦長の同じ構図は別(§10)


def test_older_first_and_joins_nearest_rep() -> None:
    far = (1 << 64) - 1
    photos = [ph(1, 0, mtime=10), ph(2, far, mtime=20), ph(3, flip(0, 2), mtime=30), ph(4, flip(far, 1), mtime=40)]
    groups = similar_groups(photos, 8)
    assert sorted(sorted(p.pid for p in g) for g in groups) == [[1, 3], [2, 4]]


def test_exact_groups_and_hardlinks() -> None:
    a = ph(1, 0, sha="X", size=100)
    b = ph(2, 0, sha="X", size=100)
    c = ph(3, 0, sha="X", size=100)
    d = ph(4, 0, sha="Y", size=100)
    keys = {1: (1, 11), 2: (1, 11), 3: (1, 33), 4: (1, 44)}  # 1 と 2 は同じ実体
    groups, dropped = exact_groups([a, b, c, d], lambda p: keys[p.pid])
    assert dropped == {2}
    assert [sorted(p.pid for p in g) for g in groups] == [[1, 3]]
    only_links, dropped2 = exact_groups([ph(5, 0, sha="Z"), ph(6, 0, sha="Z")], lambda p: (9, 9))
    assert only_links == [] and dropped2 == {6}


def test_ac4_most_pixels_is_recommended() -> None:
    g = [ph(1, 0, w=800, h=600), ph(2, 0, w=1600, h=1200, size=10), ph(3, 0, w=400, h=300, sharp=999)]
    best, reason = pick_keep(g)
    assert best.pid == 2 and reason == REASON_PIXELS


SAME = 1_700_000_000_000_000_000  # 同じ更新日時(ナノ秒)


def test_scoring_order() -> None:
    best, reason = pick_keep([ph(1, 0, sharp=5, mtime=SAME), ph(2, 0, sharp=9, mtime=SAME)])
    assert (best.pid, reason) == (2, REASON_SHARP)
    best, reason = pick_keep([ph(1, 0, size=10, mtime=SAME), ph(2, 0, size=20, mtime=SAME)])
    assert (best.pid, reason) == (2, REASON_SIZE)
    best, reason = pick_keep([ph(1, 0, mtime=SAME + 50 * 10**9), ph(2, 0, mtime=SAME + 40 * 10**9)])
    assert (best.pid, reason) == (2, REASON_OLDEST)


def test_older_original_beats_sharper_edit() -> None:
    # 明るさを上げた編集版は鮮明さが高く出るが、先に撮った元の写真を残す(2026-09-25 の利用者の判断)
    original = ph(1, 0, sharp=5, taken="2026-05-01T10:00:00")
    edited = ph(2, 0, sharp=50, mtime=SAME + 10**17)
    best, reason = pick_keep([edited, original])
    assert (best.pid, reason) == (1, REASON_OLDEST)


def test_same_second_falls_through_to_sharpness() -> None:
    a = ph(1, 0, sharp=5, taken="2026-05-01T10:00:00")
    b = ph(2, 0, sharp=9, taken="2026-05-01T10:00:00")
    best, reason = pick_keep([a, b])
    assert (best.pid, reason) == (2, REASON_SHARP)


def _model() -> ResultModel:
    photos = [ph(1, 0, sha="S", size=500), ph(2, 0, sha="S", size=500), ph(3, flip(0, 3), w=200, h=150, size=30)]
    exact, _ = exact_groups(photos, lambda p: (1, p.pid))
    return ResultModel.build(photos, exact, "normal", False)


def test_model_sections_and_initial_state() -> None:
    m = _model()
    kinds = [g.kind for g in m.groups]
    assert kinds == [KIND_EXACT, KIND_SIMILAR]  # 完全一致が先(G-2・FR-8)
    ex, sim = m.groups
    assert m.is_keep(ex.recommended)
    assert sum(1 for p in ex.photos if m.is_keep(p.pid)) == 1
    # 完全一致のコピーは似ているグループに入らない。完全一致の「残す」は似ているグループにも入る
    sim_ids = {p.pid for p in sim.photos}
    assert ex.recommended in sim_ids and len(sim_ids & {1, 2}) == 1
    assert m.invariant_ok()
    assert {p.pid for p in m.selected()} == ({1, 2} - {ex.recommended}) | {3}


def test_g5_last_keep_cannot_be_trashed() -> None:
    m = _model()
    ex = m.groups[0]
    keep_pid = ex.recommended
    assert not m.can_trash(keep_pid)
    assert m.set_keep(keep_pid, False) is False
    assert m.is_keep(keep_pid)
    other = next(p.pid for p in ex.photos if p.pid != keep_pid)
    assert m.set_keep(other, True)
    assert m.can_trash(keep_pid) is False  # 似ているグループでは最後の「残す」のまま
    assert m.set_keep(3, True)
    assert m.set_keep(keep_pid, False)
    assert m.invariant_ok()


def test_invariant_detects_broken_state() -> None:
    m = _model()
    for p in m.groups[0].photos:
        m.keep[p.pid] = False  # テストで直接壊す
    assert not m.invariant_ok()


def test_reducible_order_and_remove() -> None:
    photos = [ph(1, 0, sha="A", size=100), ph(2, 0, sha="A", size=100), ph(3, 0, sha="B", size=900), ph(4, 0, sha="B", size=900)]
    exact, _ = exact_groups(photos, lambda p: (1, p.pid))
    m = ResultModel.build(photos, exact, "normal", True)
    assert [m.reducible(g) for g in m.groups] == [900, 100]  # 減らせる大きさの大きい順(FR-8)
    trash = [p.pid for p in m.selected()]
    m.remove_photos(trash)
    assert m.groups == []  # 残りが1枚のグループは消える(FR-15)
    assert m.selected() == []


def test_rebuild_changes_grouping_without_features() -> None:
    photos = [ph(1, 0), ph(2, flip(0, 10))]
    m = ResultModel.build(photos, [], "normal", False)
    assert m.groups == []
    loose = m.rebuild("loose", False)
    assert len(loose.groups) == 1
    assert m.rebuild("loose", True).groups == []  # まったく同じ写真だけ(FR-17)
