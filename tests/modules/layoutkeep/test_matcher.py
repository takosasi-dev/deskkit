# 対応付け(M-1〜M-6)と計画の単体テスト。偽 Win32 の固定データを使う。
from __future__ import annotations

import pytest

from deskkit.modules.layoutkeep import matcher, planner
from deskkit.modules.layoutkeep.fake_win32 import fake_window
from deskkit.modules.layoutkeep.model import SavedEntry, WindowInfo
from deskkit.modules.layoutkeep.selftest import EXPECTED, Scenario, make_plan

CH = "C:\\chrome.exe"
CLS = "Chrome_WidgetWin_1"


def entry(hwnd: int, pid: int, *, regex: str | None = None, proc_start: int | None = 1000,
          rect: tuple[int, int, int, int] = (0, 0, 500, 500)) -> SavedEntry:
    return SavedEntry(exe=CH, cls=CLS, title_regex=regex, title_at_save="", hwnd=hwnd, pid=pid, proc_start=proc_start,
                      show="normal", normal_rect=rect, screen_rect=rect)


def win(hwnd: int, pid: int, title: str, *, excluded: str | None = None, proc_start: int | None = 1000) -> WindowInfo:
    return WindowInfo(raw=fake_window(hwnd, pid, CH, CLS, title, proc_start=proc_start), excluded=excluded, target_index=0)


@pytest.mark.parametrize(("idx", "rule", "key", "_desc"), EXPECTED)
def test_decision_table(scenario: Scenario, idx: int, rule: str, key: str, _desc: str) -> None:
    items = {i.index: i for i in make_plan(scenario).items}
    assert items[idx].rule == rule
    assert items[idx].key == key


def test_every_rule_exercised(scenario: Scenario) -> None:
    rules = {i.rule for i in make_plan(scenario).items}
    assert {"M-1", "M-2", "M-3", "M-4", "M-5", "M-6"} <= rules


def test_strong_match_even_with_changed_title_and_sibling() -> None:
    ms = matcher.match([entry(1, 10)], [win(1, 10, "new title"), win(2, 10, "other")])
    assert ms[0].rule == "M-1" and ms[0].status == "matched"
    assert ms[0].window is not None and ms[0].window.hwnd == 1


def test_strong_requires_same_process_start() -> None:
    # hwnd と pid が同じでも起動時刻が違えば別物(再利用された hwnd)→ 強い一致にしない
    ms = matcher.match([entry(1, 10, proc_start=1)], [win(1, 10, "t", proc_start=2)])
    assert ms[0].rule == "M-3"


def test_one_entry_two_windows_is_ambiguous() -> None:
    ms = matcher.match([entry(99, 99)], [win(1, 10, "a"), win(2, 10, "b")])
    assert ms[0].status == "ambiguous" and ms[0].rule == "M-5"


def test_m3_regex_mismatch_goes_to_m5() -> None:
    ms = matcher.match([entry(99, 99, regex="^zzz")], [win(1, 10, "a")])
    assert ms[0].status == "ambiguous"


def test_m4_partial_pairs_rest_ambiguous() -> None:
    es = [entry(91, 9, regex="^A"), entry(92, 9, regex="^B"), entry(93, 9)]
    ws = [win(1, 10, "A doc"), win(2, 10, "B doc"), win(3, 10, "C doc")]
    ms = matcher.match(es, ws)
    assert [m.rule for m in ms] == ["M-4", "M-4", "M-5"]
    assert ms[0].window is not None and ms[0].window.hwnd == 1
    assert ms[1].window is not None and ms[1].window.hwnd == 2


def test_m4_regex_hitting_two_windows_is_ambiguous() -> None:
    es = [entry(91, 9, regex="doc"), entry(92, 9, regex="^B")]
    ws = [win(1, 10, "A doc"), win(2, 10, "B doc")]
    ms = matcher.match(es, ws)
    # "doc" は2枚に当たるので曖昧。"^B" の相手は "doc" からも当たるので1対1にならない
    assert [m.status for m in ms] == ["ambiguous", "ambiguous"]


def test_invalid_regex_never_matches() -> None:
    ms = matcher.match([entry(91, 9, regex="(")], [win(1, 10, "(")])
    assert ms[0].status == "ambiguous"


def test_excluded_candidates_removed_before_matching() -> None:
    # M-6: 最小化中の窓は候補から外す。残り1枚なら M-3 で対応(除外窓は動かさない)
    ms = matcher.match([entry(99, 99)], [win(1, 10, "a", excluded="minimized"), win(2, 10, "b")])
    assert ms[0].rule == "M-3" and ms[0].window is not None and ms[0].window.hwnd == 2


def test_only_excluded_in_group_is_m6() -> None:
    ms = matcher.match([entry(99, 99)], [win(1, 10, "a", excluded="cloaked")])
    assert (ms[0].rule, ms[0].status) == ("M-6", "excluded:cloaked")


def test_duplicate_strong_claims_are_ambiguous() -> None:
    ms = matcher.match([entry(1, 10), entry(1, 10)], [win(1, 10, "a")])
    assert [m.status for m in ms] == ["ambiguous", "ambiguous"]


def test_no_window_is_not_running() -> None:
    ms = matcher.match([entry(1, 10)], [])
    assert (ms[0].rule, ms[0].status) == ("M-2", "not_running")


def test_ambiguous_notification_text(scenario: Scenario) -> None:
    text = planner.summary_text(make_plan(scenario), dry_run=False, moved=4)
    assert "chrome のウィンドウ 2 枚は区別できず動かしていません" in text
