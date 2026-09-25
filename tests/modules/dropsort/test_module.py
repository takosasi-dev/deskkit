# host 結合部(DropSortModule)のテスト: 既定値の書き戻し・スキャンの流れ・通知の保留(AC-18)・トレイ・CLI・設定変更。
from __future__ import annotations

from pathlib import Path

import pytest

from deskkit.modules.dropsort import selftest as st
from deskkit.modules.dropsort.config import ConfigError
from deskkit.modules.dropsort.fakewin32 import FakeWin32
from deskkit.modules.dropsort.module import DropSortModule
from deskkit.modules.dropsort.notifier import Notifier

from .helpers import FULL_FG, GAME_FG, SAFE_FG, FakeCtx, wait_worker

DL = st.DL
PDF = "C:\\Docs\\PDF"


def _module(tmp_path: Path, rules: list[dict[str, object]], **over: object) -> tuple[DropSortModule, FakeCtx, FakeWin32, st.FakeClock]:
    fake = FakeWin32(DL)
    fake.mkdirs(PDF)
    ctx = FakeCtx(tmp_path, {"watch_mode": "poll", "rules": rules, **over})
    clock = st.FakeClock()
    m = DropSortModule(ctx, api=fake, clock=clock)
    return m, ctx, fake, clock


def _scan(m: DropSortModule, ctx: FakeCtx) -> None:
    m._kick()
    wait_worker(m, ctx)


def test_defaults_written_back(tmp_path: Path) -> None:
    ctx = FakeCtx(tmp_path, {})
    DropSortModule(ctx, api=FakeWin32(DL))
    assert ctx.writes and ctx.writes[0][0]["temp_extensions"] and ctx.writes[0][0]["rules"] == []


def test_invalid_config_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        DropSortModule(FakeCtx(tmp_path, {"watch_mode": "x"}), api=FakeWin32(DL))


def test_flow_moves_and_notifies(tmp_path: Path) -> None:
    m, ctx, fake, clock = _module(tmp_path, [st.rule("pdf", PDF, [".pdf"])])
    m.start()
    try:
        _scan(m, ctx)  # 基準線
        assert any("開始" in n[0] for n in ctx.notes)
        fake.add_file(DL + "\\a.pdf", 10, mtime=clock.t)
        _scan(m, ctx)
        assert m.last_cycle is not None and m.last_cycle.pending == 1
        follow = [t for t in ctx.timers if t.single and t.active]
        assert follow, "安定待ちの再スキャンが予約されていない"
        clock.advance(10)
        _scan(m, ctx)
        assert fake.names_in(PDF) == ["a.pdf"]
        assert any("整理しました" in n[0] for n in ctx.notes)
        assert "今日 1 件移動" in ctx.status
        code, text = m.handle_cli(["undo"])
        assert code == 0 and fake.names_in(DL) == ["a.pdf"]
        assert not ctx.errors, ctx.errors
    finally:
        m.stop()


def test_notifications_deferred_during_game(tmp_path: Path) -> None:
    ctx = FakeCtx(tmp_path)
    n = Notifier(ctx)
    ctx.fg = GAME_FG
    n.push("1 件を整理しました", "a", "ok")
    n.push("要確認", "b", "warn")
    assert ctx.notes == []
    ctx.fg = FULL_FG
    n.poll()
    assert ctx.notes == []
    ctx.fg = SAFE_FG
    n.poll()
    assert len(ctx.notes) == 1 and "2 件" in ctx.notes[0][0] and ctx.notes[0][2] == "warn"
    n.push("x", "y")
    assert len(ctx.notes) == 2


def test_pause_blocks_processing(tmp_path: Path) -> None:
    m, ctx, fake, clock = _module(tmp_path, [st.rule("pdf", PDF, [".pdf"])])
    m.start()
    try:
        _scan(m, ctx)
        m.set_paused(True)
        assert ctx.writes[-1][0]["paused"] is True and m._tray["pause"].checked
        fake.add_file(DL + "\\a.pdf", 10, mtime=clock.t)
        for _ in range(3):
            clock.advance(10)
            _scan(m, ctx)
        assert fake.names_in(PDF) == [] and ctx.status == "一時停止中"
        m.set_paused(False)
        for _ in range(2):
            clock.advance(10)
            _scan(m, ctx)
        assert fake.names_in(PDF) == ["a.pdf"]
    finally:
        m.stop()


def test_tray_and_hotkey(tmp_path: Path) -> None:
    m, ctx, _fake, _clock = _module(tmp_path, [], hotkeys={"undo_last": "Ctrl+Alt+Z"})
    m.start()
    try:
        labels = [t.label for t in ctx.tray]
        assert labels[:3] == ["一時停止", "直前の1件を元に戻す", "試運転の結果を見る…"]
        assert ctx.hotkeys.registered == {"undo_last": "Ctrl+Alt+Z"} and "undo_last" in ctx.hotkeys.callbacks
        ctx.hotkeys.callbacks["undo_last"]()
        wait_worker(m, ctx)
        assert any("ありません" in n[1] for n in ctx.notes)
    finally:
        m.stop()


def test_update_settings_validates(tmp_path: Path) -> None:
    m, ctx, _fake, _clock = _module(tmp_path, [])
    m.start()
    try:
        sec = m.settings_dict()
        sec["full_scan_interval_s"] = 12
        m.update_settings(sec)
        assert m.cfg.full_scan_interval_s == 12
        bad = m.settings_dict()
        bad["stable_seconds"] = -1
        with pytest.raises(ConfigError):
            m.update_settings(bad)
        assert m.cfg.stable_seconds == 5
    finally:
        m.stop()


def test_watch_dead_falls_back(tmp_path: Path) -> None:
    class DeadWatch:
        def open_dir(self, path: str) -> tuple[int | None, int]:
            return None, 5

    fake = FakeWin32(DL)
    ctx = FakeCtx(tmp_path, {"watch_mode": "rdcw"})
    m = DropSortModule(ctx, api=fake, watch_api=DeadWatch())
    m.start()
    try:
        m._watcher._thread.join(2)
        ctx.drain()
        assert m._watch_dead and "監視停止中" in m.watch_label and m._watcher is None
    finally:
        m.stop()


def test_create_folder_only_on_request(tmp_path: Path) -> None:
    m, _ctx, fake, _clock = _module(tmp_path, [])
    assert not fake.is_dir("C:\\New\\Sub")
    assert m.create_folder("C:\\New\\Sub") == 0 and fake.is_dir("C:\\New\\Sub")
