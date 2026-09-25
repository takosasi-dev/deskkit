# Aero スナップ中のウィンドウの保存(偽 Win32): GetWindowRect と rcNormalPosition(スクリーン換算)が違えば
# 今の矩形をワークスペース座標に直して normal_rect にし、原値を normal_rect_raw、snapped=true で残す。
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from deskkit.modules.layoutkeep import monitors
from deskkit.modules.layoutkeep.fake_win32 import FakeWin32, fake_monitor, fake_window
from deskkit.modules.layoutkeep.model import SW_SHOWMAXIMIZED, Placement
from deskkit.modules.layoutkeep.windows import effective_normal_rect

from .fakes import make_module

# タスクバーが上にある主モニタ(ワークスペース原点がスクリーンの (0, 40))
PRIMARY = fake_monitor("\\\\.\\DISPLAY1", (0, 0, 1920, 1080), primary=True, work=(0, 40, 1920, 1080))
OFFSET = monitors.workspace_offset([PRIMARY])


def _win(hwnd: int, normal: tuple[int, int, int, int], screen: tuple[int, int, int, int], *, maximized: bool = False):  # type: ignore[no-untyped-def]
    w = fake_window(hwnd, 10, "C:\\Apps\\ed.exe", "EdWnd", "doc", rect=normal, maximized=maximized)
    return replace(w, screen_rect=screen)


def test_offset_is_work_origin_relative_to_monitor() -> None:
    assert OFFSET == (0, 40)


def test_not_snapped_when_rects_agree_after_conversion() -> None:
    rect, snapped = effective_normal_rect(_win(1, (100, 100, 500, 500), (100, 140, 500, 540)), OFFSET)
    assert (rect, snapped) == ((100, 100, 500, 500), False)


def test_snapped_uses_current_rect_in_workspace_coords() -> None:
    rect, snapped = effective_normal_rect(_win(1, (100, 100, 500, 500), (0, 40, 960, 1080)), OFFSET)
    assert (rect, snapped) == ((0, 0, 960, 1040), True)


def test_maximized_is_never_snapped() -> None:
    rect, snapped = effective_normal_rect(_win(1, (100, 100, 500, 500), (-8, 32, 1928, 1088), maximized=True), OFFSET)
    assert (rect, snapped) == ((100, 100, 500, 500), False)


def test_save_stores_snapped_and_plan_sees_it_in_place(tmp_path: Path) -> None:
    snapped_win = _win(11, (300, 200, 900, 700), (0, 40, 960, 1080))
    plain_win = replace(_win(12, (100, 100, 500, 500), (100, 140, 500, 540)), cls="Other")
    api = FakeWin32(monitors=[PRIMARY], windows=[snapped_win, plain_win], pid=4242)
    ctx, mod = make_module(tmp_path, api, {"targets": [{"exe": "ed.exe"}]})
    mod.start()
    assert mod.save("tray")
    sig = mod.last_sig.signature
    data = json.loads(mod.store.layout_path(sig).read_text(encoding="utf-8"))
    by_hwnd = {w["hwnd"]: w for w in data["windows"]}
    assert by_hwnd[11]["snapped"] is True
    assert by_hwnd[11]["normal_rect"] == [0, 0, 960, 1040]
    assert by_hwnd[11]["normal_rect_raw"] == [300, 200, 900, 700]
    assert by_hwnd[12]["snapped"] is False and by_hwnd[12]["normal_rect_raw"] is None
    assert by_hwnd[12]["normal_rect"] == [100, 100, 500, 500]
    # 読み戻せる
    lay = mod.store.load_layout(sig)
    assert lay is not None and lay.windows[0].snapped and lay.windows[0].normal_rect_raw == (300, 200, 900, 700)
    # 保存直後の計画: スナップ中の窓も「既に同じ位置」で動かさない
    plan, _ = mod.plan_for(sig)
    assert plan is not None and not plan.moves
    assert {i.key for i in plan.items} == {"unchanged"}


def test_apply_uses_snapped_rect_via_set_window_placement(tmp_path: Path) -> None:
    snapped_win = _win(11, (300, 200, 900, 700), (0, 40, 960, 1080))
    api = FakeWin32(monitors=[PRIMARY], windows=[snapped_win], pid=4242)
    ctx, mod = make_module(tmp_path, api, {"targets": [{"exe": "ed.exe"}], "mode": "live"})
    mod.start()
    assert mod.save("tray")
    # ウィンドウが崩れた(スナップが外れて別の場所へ)
    api.windows[0] = replace(api.windows[0], placement=Placement(1, (500, 500, 900, 900)), screen_rect=(500, 540, 900, 940))
    assert mod.apply_current("tray") == "applied"
    (hwnd, pl), = api.set_calls
    assert hwnd == 11 and pl.normal_rect == (0, 0, 960, 1040) and pl.show_cmd != SW_SHOWMAXIMIZED
    # undo には崩れた時点の見た目どおりの矩形が残る
    undo = mod.store.read_undo()
    assert undo is not None and undo["windows"][0]["normal_rect"] == [500, 500, 900, 900]
