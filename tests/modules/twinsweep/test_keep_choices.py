# v0.4.1: 似ている度合いを変えても、利用者が選び直した「残す」「ごみ箱へ」を写真ごとに引き継ぐ(T-9 を改めた)。
# 「残す」が無くなるグループは初期の選び方に戻す(G-5・INV-2)。新しいスキャンで覚えた選択を捨てる。
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from deskkit.modules.twinsweep.grouping import exact_groups
from deskkit.modules.twinsweep.results import ResultModel
from tests.modules.twinsweep.conftest import FakeCtx, wait_idle
from tests.modules.twinsweep.test_grouping_scoring import flip, ph
from tests.modules.twinsweep.test_module_flow import make_module, make_set, scan


def _three() -> ResultModel:
    # 1 と 2 は距離 3、1 と 3 は距離 6: 「厳しめ」(4)では {1,2}、「ふつう」(8)では {1,2,3}。1 がいちばん画素数が多い
    photos = [ph(1, 0, w=800, h=600), ph(2, flip(0, 3)), ph(3, flip(0, 6))]
    return ResultModel.build(photos, [], "normal", False)


def _members(m: ResultModel) -> list[list[int]]:
    return [sorted(p.pid for p in g.photos) for g in m.groups]


def test_initial_state_has_no_choices() -> None:
    m = _three()
    assert _members(m) == [[1, 2, 3]]
    assert m.choices == {}
    assert [m.is_keep(i) for i in (1, 2, 3)] == [True, False, False]


def test_choices_survive_regroup_and_come_back() -> None:
    m = _three()
    assert m.set_keep(2, True) and m.set_keep(3, True) and m.set_keep(1, False)
    assert m.choices == {1: False, 2: True, 3: True}
    strict = m.rebuild("strict", False)
    assert _members(strict) == [[1, 2]]
    assert (strict.is_keep(1), strict.is_keep(2)) == (False, True)
    assert strict.invariant_ok()
    # 3 は「厳しめ」ではグループに入らないが、覚えた選択は捨てない。戻すとまた当たる
    back = strict.rebuild("normal", False)
    assert [back.is_keep(i) for i in (1, 2, 3)] == [False, True, True]
    assert back.choices == {1: False, 2: True, 3: True}
    # 選び直した結果が初期と同じでも覚えておく(同じ値を当てるだけ)
    assert back.set_keep(3, False)
    assert back.rebuild("loose", False).is_keep(3) is False


def test_group_without_keep_falls_back_to_initial() -> None:
    m = _three()
    assert m.set_keep(3, True) and m.set_keep(1, False)   # 1: ごみ箱へ、2: ごみ箱へ(初期)、3: 残す
    strict = m.rebuild("strict", False)
    # {1,2} に当てると「残す」が無くなるので、このグループは初期の選び方(1 を残す)に戻す
    assert _members(strict) == [[1, 2]]
    assert (strict.is_keep(1), strict.is_keep(2)) == (True, False)
    assert strict.invariant_ok()
    # 覚えた選択は残るので、「ふつう」に戻せば 3 を残して 1 をごみ箱へ、に戻る
    back = strict.rebuild("normal", False)
    assert [back.is_keep(i) for i in (1, 2, 3)] == [False, False, True]
    assert back.invariant_ok()


def test_all_trash_is_never_produced_with_exact_and_similar() -> None:
    # T-5: 完全一致の「残す」は似ているグループにも入る。どの組み合わせで作り直しても全部ごみ箱にはならない
    photos = [ph(1, 0, sha="S", size=500), ph(2, 0, sha="S", size=500), ph(3, flip(0, 3), w=200, h=150, size=30)]
    exact, _ = exact_groups(photos, lambda p: (1, p.pid))
    m = ResultModel.build(photos, exact, "normal", False)
    ex = m.exact_groups()[0]
    keep_pid = ex.recommended
    other = next(p.pid for p in ex.photos if p.pid != keep_pid)
    assert m.set_keep(other, True) and m.set_keep(3, True) and m.set_keep(keep_pid, False)
    for level in ("strict", "normal", "loose"):
        for exact_only in (False, True):
            r = m.rebuild(level, exact_only)
            assert r.invariant_ok(), (level, exact_only)
            assert r.is_keep(other) and not r.is_keep(keep_pid)


def test_invalid_state_is_repaired_even_for_hand_made_choices() -> None:
    # 画面を通さずに作った「全部ごみ箱へ」の選択でも、作り直すと初期に戻る
    m = _three()
    m.choices = {1: False, 2: False, 3: False}
    r = m.rebuild("normal", False)
    assert r.invariant_ok()
    assert [r.is_keep(i) for i in (1, 2, 3)] == [True, False, False]


def test_removed_photos_are_forgotten() -> None:
    m = _three()
    assert m.set_keep(2, True) and m.set_keep(3, True)
    m.remove_photos([3])
    assert 3 not in m.choices and m.choices == {2: True}
    assert m.rebuild("loose", False).is_keep(2)


def test_reset_choices_keeps_current_view() -> None:
    m = _three()
    assert m.set_keep(2, True)
    m.reset_choices()
    assert m.choices == {} and m.is_keep(2)  # 今の表示はそのまま
    assert m.rebuild("strict", False).is_keep(2) is False  # 作り直すと初期に戻る


def test_module_keeps_choices_on_level_change_and_forgets_on_new_scan(
        make_ctx: Callable[..., FakeCtx], tmp_path: Path) -> None:
    photos = tmp_path / "pics"
    files = make_set(photos)
    ctx = make_ctx({"folders": [str(photos)]})
    m = make_module(ctx, tmp_path)
    scan(ctx, m)
    assert m.model is not None
    small = next(p for p in m.model.photos.values() if Path(p.path).name == files["small"].name)
    assert not m.model.is_keep(small.pid)
    assert m.set_keep(small.pid, True)
    for level in ("loose", "strict", "normal"):
        assert m.set_level(level) is None
        wait_idle(ctx, m)
        assert m.model is not None and m.model.level == level
        if small.pid in m.model.keep:
            assert m.model.is_keep(small.pid), level
        assert m.model.invariant_ok()
    assert m.model.choices == {small.pid: True}
    assert m.model.is_keep(small.pid)
    # 探す写真を変えても同じ
    assert m.set_exact_only(True) is None
    wait_idle(ctx, m)
    assert m.set_exact_only(False) is None
    wait_idle(ctx, m)
    assert m.model is not None and m.model.is_keep(small.pid)
    # 新しいスキャンを始めたら捨てる
    assert m.start_scan() is None
    assert m.model is not None and m.model.choices == {}
    wait_idle(ctx, m)
    assert m.model is not None and m.model.choices == {}
    small2 = next(p for p in m.model.photos.values() if Path(p.path).name == files["small"].name)
    assert not m.model.is_keep(small2.pid)
    m.stop()
