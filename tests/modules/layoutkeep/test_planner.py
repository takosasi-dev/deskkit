# 計画(planner)の単体テスト: 画面外・既に同じ位置・ログ用辞書にタイトルが無いこと。
from __future__ import annotations

import json

from deskkit.modules.layoutkeep import matcher, planner
from deskkit.modules.layoutkeep.fake_win32 import fake_monitor, fake_window
from deskkit.modules.layoutkeep.model import SavedEntry, WindowInfo
from deskkit.modules.layoutkeep.selftest import Scenario, make_plan

MONS = [fake_monitor("\\\\.\\DISPLAY1", (0, 0, 1920, 1080), primary=True)]


def _one(rect: tuple[int, int, int, int], cur: tuple[int, int, int, int], show: str = "normal") -> planner.PlanItem:
    e = SavedEntry(exe="C:\\a.exe", cls="A", title_regex=None, title_at_save="秘密のタイトル", hwnd=5, pid=6, proc_start=1000,
                   show=show, normal_rect=rect, screen_rect=rect)
    w = WindowInfo(raw=fake_window(5, 6, "C:\\a.exe", "A", "秘密のタイトル", rect=cur), target_index=0)
    return planner.make_plan("0123456789ab", matcher.match([e], [w]), MONS).items[0]


def test_offscreen_is_skipped() -> None:
    it = _one((5000, 5000, 5500, 5500), (0, 0, 100, 100))
    assert it.action == "skip" and it.key == "excluded:offscreen"


def test_unchanged_is_not_a_move() -> None:
    it = _one((10, 10, 500, 500), (10, 10, 500, 500))
    assert it.action == "skip" and it.key == "unchanged"


def test_show_change_is_a_move() -> None:
    it = _one((10, 10, 500, 500), (10, 10, 500, 500), show="maximized")
    assert it.action == "move" and it.after_show == "maximized"


def test_log_dict_has_no_title(scenario: Scenario) -> None:
    plan = make_plan(scenario)
    text = json.dumps([i.log_dict() for i in plan.items], ensure_ascii=False)
    assert "title" not in text
    assert "alpha doc" not in text and "page A" not in text


def test_skipped_counts(scenario: Scenario) -> None:
    sk = make_plan(scenario).skipped()
    assert sk == {"ambiguous": 2, "excluded:cloaked": 1, "excluded:minimized": 1, "not_running": 1, "unchanged": 1}
