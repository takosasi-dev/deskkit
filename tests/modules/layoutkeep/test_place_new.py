# L3 新しいウィンドウの配置: 既定オフ・試運転から。EnumWindows の差分だけで検出し(フックなし)、
# 既定のプリセットの保存エントリに一意に対応した窓だけを1回だけ動かす。スヌーズ中・ゲーム/全画面が前面・昇格した窓は動かさない。
from __future__ import annotations

from pathlib import Path
from typing import Any

from deskkit.modules.layoutkeep.fake_win32 import FakeWin32, fake_monitor, fake_window
from deskkit.modules.layoutkeep.model import Layout, SavedEntry
from deskkit.modules.layoutkeep.monitors import compute
from deskkit.modules.layoutkeep.placer import NewWindowWatcher
from deskkit.modules.layoutkeep.sysevents import WM_DISPLAYCHANGE

from .fakes import GAME_FG, SAFE_FG, FakeCtx, make_module

ED = "C:\\Apps\\ed.exe"
OTHER = "C:\\Apps\\other.exe"
MON = fake_monitor("\\\\.\\DISPLAY1", (0, 0, 1920, 1080), primary=True, work=(0, 0, 1920, 1040))


def _entry(hwnd: int, rect: tuple[int, int, int, int], regex: str | None = None) -> SavedEntry:
    return SavedEntry(exe=ED, cls="EdWnd", title_regex=regex, title_at_save="", hwnd=hwnd, pid=hwnd, proc_start=1,
                      show="normal", normal_rect=rect, screen_rect=rect)


def _setup(tmp_path: Path, entries: list[SavedEntry], *, place: dict[str, Any] | None = None, mode: str = "dry_run",
           existing: list[Any] | None = None) -> tuple[FakeCtx, Any, FakeWin32]:
    api = FakeWin32(monitors=[MON], windows=list(existing or [fake_window(10, 10, OTHER, "OtherWnd", "x")]), pid=4242)
    sig = compute([MON], "device_interface").signature
    assert sig is not None
    ctx, mod = make_module(tmp_path, api, {"targets": [{"exe": "ed.exe"}], "mode": mode,
                                           "place_new": {"enabled": True, **(place or {})}})
    mod.store.save_layout(Layout(sig, [], "2026-09-25T10:00:00+09:00", entries))
    mod.start()
    return ctx, mod, api


def _tick(mod: Any, n: int = 1) -> None:
    for _ in range(n):
        mod._place_timer.fire()


def _open(api: FakeWin32, hwnd: int, title: str = "doc", rect: tuple[int, int, int, int] = (0, 0, 300, 300)) -> None:
    api.windows.append(fake_window(hwnd, hwnd, ED, "EdWnd", title, rect=rect, proc_start=hwnd))


def test_off_by_default_no_polling(tmp_path: Path) -> None:
    api = FakeWin32(monitors=[MON], windows=[], pid=4242)
    ctx, mod = make_module(tmp_path, api, {"targets": [{"exe": "ed.exe"}]})
    mod.start()
    assert mod._place_timer is None
    calls = api.enum_calls
    mod._place_tick()
    assert api.enum_calls == calls  # 無効なら EnumWindows を呼ばない


def test_dry_run_reports_and_never_moves(tmp_path: Path) -> None:
    ctx, mod, api = _setup(tmp_path, [_entry(900, (100, 100, 700, 600))])
    assert mod._place_timer.interval == 1500
    _tick(mod)  # 1回目: 今ある窓を覚えるだけ
    _open(api, 501)
    _tick(mod, 2)  # 出現 → 位置が落ち着いたら判定
    assert api.set_calls == []
    ev = mod.place_events[0]
    assert (ev.action, ev.key, ev.rule, ev.after) == ("would_move", "move", "M-3", (100, 100, 700, 600))
    text = mod.store.oplog_path.read_text(encoding="utf-8")
    assert '"action":"place"' in text and '"result":"dry_run"' in text and "doc" not in text
    _tick(mod, 3)
    assert len(mod.place_events) == 1  # 1回だけ扱う
    d = mod.diagnostics()
    assert d["place_new"] is True and d["place_new_mode"] == "dry_run" and d["last_place"] == "would_move"


def test_existing_windows_are_not_touched(tmp_path: Path) -> None:
    existing = [fake_window(501, 501, ED, "EdWnd", "doc", rect=(0, 0, 300, 300), proc_start=501)]
    ctx, mod, api = _setup(tmp_path, [_entry(900, (100, 100, 700, 600))], place={"mode": "live"}, mode="live",
                           existing=existing)
    _tick(mod, 5)
    assert api.set_calls == [] and not mod.place_events


def test_live_moves_once_and_writes_undo(tmp_path: Path) -> None:
    ctx, mod, api = _setup(tmp_path, [_entry(900, (100, 100, 700, 600))], place={"mode": "live"}, mode="live")
    _tick(mod)
    _open(api, 501)
    _tick(mod, 2)
    assert [h for h, _ in api.set_calls] == [501]
    assert mod.place_events[0].action == "moved"
    undo = mod.store.read_undo()
    assert undo is not None and undo.get("tag") == "place" and undo["windows"][0]["hwnd"] == 501
    # アプリが自分で位置を変えても追いかけない(1回だけ)
    api.windows[-1] = fake_window(501, 501, ED, "EdWnd", "doc", rect=(0, 0, 50, 50), proc_start=501)
    _tick(mod, 3)
    assert len(api.set_calls) == 1


def test_global_dry_run_blocks_live_placement(tmp_path: Path) -> None:
    # INV-7: 全体が試運転なら、配置だけ本番にしても SetWindowPlacement を呼ばない
    ctx, mod, api = _setup(tmp_path, [_entry(900, (100, 100, 700, 600))], place={"mode": "live"}, mode="dry_run")
    _tick(mod)
    _open(api, 501)
    _tick(mod, 2)
    assert api.set_calls == [] and mod.place_events[0].action == "would_move"


def test_ambiguous_group_is_not_moved(tmp_path: Path) -> None:
    ctx, mod, api = _setup(tmp_path, [_entry(900, (100, 100, 700, 600)), _entry(901, (800, 100, 1400, 600))],
                           place={"mode": "live"}, mode="live")
    _tick(mod)
    _open(api, 501)
    _tick(mod, 2)
    assert api.set_calls == []
    assert mod.place_events[0].key == "ambiguous"


def test_regex_makes_it_unique(tmp_path: Path) -> None:
    ctx, mod, api = _setup(tmp_path, [_entry(900, (100, 100, 700, 600), "^alpha"), _entry(901, (800, 100, 1400, 600), "^beta")],
                           place={"mode": "live"}, mode="live")
    _tick(mod)
    _open(api, 501, "beta doc")
    _tick(mod, 2)
    (hwnd, pl), = api.set_calls
    assert hwnd == 501 and pl.normal_rect == (800, 100, 1400, 600)
    assert mod.place_events[0].rule == "M-4"


def test_elevated_target_is_not_moved(tmp_path: Path) -> None:
    ctx, mod, api = _setup(tmp_path, [_entry(900, (100, 100, 700, 600))], place={"mode": "live"}, mode="live")
    api.elevated_pids.add(501)
    api.unknown_elevation_pids.add(502)
    _tick(mod)
    _open(api, 501)
    _tick(mod, 2)
    assert api.set_calls == [] and mod.place_events[0].key == "elevated"
    api.windows = [w for w in api.windows if w.hwnd != 501]
    _open(api, 502)
    _tick(mod, 2)
    assert api.set_calls == [] and mod.place_events[0].key == "elevated_unknown"


def test_game_or_snooze_absorbs_new_windows(tmp_path: Path) -> None:
    ctx, mod, api = _setup(tmp_path, [_entry(900, (100, 100, 700, 600))], place={"mode": "live"}, mode="live")
    _tick(mod)
    ctx.fg = GAME_FG
    _open(api, 501)
    _tick(mod, 2)
    ctx.fg = SAFE_FG
    _tick(mod, 3)
    assert api.set_calls == []  # ゲーム中に出てきた窓は後から動かさない
    ctx.snoozed = True
    _open(api, 502)
    _tick(mod, 2)
    ctx.snoozed = False
    _tick(mod, 3)
    assert api.set_calls == []
    assert mod.codes["place"] == "snoozed"


def test_hidden_then_shown_window_waits_and_non_targets_are_ignored(tmp_path: Path) -> None:
    ctx, mod, api = _setup(tmp_path, [_entry(900, (100, 100, 700, 600))], place={"mode": "live"}, mode="live")
    _tick(mod)
    api.windows.append(fake_window(601, 601, OTHER, "OtherWnd", "o"))  # 対象外
    api.windows.append(fake_window(501, 501, ED, "EdWnd", "doc", visible=False, proc_start=501))
    _tick(mod, 3)
    assert api.set_calls == [] and mod.placer.pending_count == 1  # 対象外は以後見ない・非表示は待つ
    api.windows[-1] = fake_window(501, 501, ED, "EdWnd", "doc", proc_start=501)
    _tick(mod, 2)
    assert [h for h, _ in api.set_calls] == [501]


def test_display_change_resets_and_disable_stops_polling(tmp_path: Path) -> None:
    ctx, mod, api = _setup(tmp_path, [_entry(900, (100, 100, 700, 600))], place={"mode": "live"}, mode="live")
    _tick(mod)
    for h in ctx.native[WM_DISPLAYCHANGE]:
        h(0, 0)
    _open(api, 501)
    _tick(mod, 3)  # 構成が変わっている最中は判定しない
    assert api.set_calls == []
    assert mod.set_nested("place_new", "enabled", False) is None
    assert not mod._place_timer.active
    calls = api.enum_calls
    mod._place_tick()
    assert api.enum_calls == calls


def test_watcher_prunes_closed_windows() -> None:
    api = FakeWin32(monitors=[MON], windows=[], pid=4242)
    w = NewWindowWatcher(api, lambda raw: True)
    assert w.poll_new() is False  # 最初は覚えるだけ
    api.windows.append(fake_window(501, 501, ED, "EdWnd", "doc"))
    assert w.poll_new() is True
    w.ready()
    w.mark_handled(501)
    assert w.handled_count == 1
    api.windows.clear()
    w.poll_new()
    assert w.handled_count == 0 and w.pending_count == 0
