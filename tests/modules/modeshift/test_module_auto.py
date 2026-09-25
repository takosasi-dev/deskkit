# トレイ(FR-2)・ホットキー(FR-3)の組み立てと、自動切替(FR-23)の検知ロジック(偽の実装で)。
from __future__ import annotations

from pathlib import Path

from deskkit.modules.modeshift.autoswitch import AutoSwitcher
from deskkit.modules.modeshift.config import AutoRule
from deskkit.modules.modeshift.fakes import FakeCtx, fake_system, populate, sample_section
from deskkit.modules.modeshift.module import ModeShiftModule


def test_tray_and_hotkeys(tmp_path: Path) -> None:
    sysm = fake_system()
    populate(sysm)
    ctx = FakeCtx(tmp_path / "d", sample_section(tmp_path), games=frozenset({"game.exe"}))
    mod = ModeShiftModule(ctx, backends=sysm.backends())
    mod.start()
    labels = [t.label for t in ctx.tray if t.submenu == "モード"]
    assert labels[:2] == ["ゲーム(未確認)", "勉強(未確認)"]      # 有効なモードだけ、定義順
    assert "プレビュー…" in labels and any(x.startswith("元に戻す") for x in labels)
    assert ctx.hotkeys.registered == {"mode.game": "Ctrl+Alt+G", "mode.study": "Ctrl+Alt+S", "undo": "Ctrl+Alt+Z"}
    assert any("無効な設定" in n[0] for n in ctx.notifications)          # FR-1 の通知
    assert mod.service is not None
    assert set(ctx.hotkeys.callbacks) == {"mode.game", "mode.study", "undo"}
    mod.stop()


def test_hotkey_failure_is_notified_on_reload(tmp_path: Path) -> None:
    sysm = fake_system()
    ctx = FakeCtx(tmp_path / "d", sample_section(tmp_path))
    ctx.hotkeys.refuse.add("Ctrl+Alt+S")
    mod = ModeShiftModule(ctx, backends=sysm.backends())
    mod.start()
    assert "mode.study" not in ctx.hotkeys.registered
    mod.save_section(ctx.settings_dict())
    assert any("ホットキーを登録できません" in n[0] for n in ctx.notifications)
    mod.stop()


def test_autoswitch_edges() -> None:
    seq = [{"a.exe"}, {"a.exe", "game.exe"}, {"a.exe", "game.exe"}, {"a.exe"}]
    it = iter(seq)
    appeared: list[str] = []
    vanished: list[str] = []
    rules = [AutoRule("game.exe", "game", "undo"), AutoRule("x.exe", "game", "undo", error="bad")]
    sw = AutoSwitcher(lambda: next(it), 1.0, rules, lambda r: appeared.append(r.mode), lambda r: vanished.append(r.exe),
                      lambda fn: fn())
    for _ in seq:
        sw.poll_once()
    assert appeared == ["game"] and vanished == ["game.exe"]


def test_autoswitch_already_running_at_start_is_not_trigger() -> None:
    appeared: list[str] = []
    sw = AutoSwitcher(lambda: {"game.exe"}, 1.0, [AutoRule("game.exe", "game", "none")],
                      lambda r: appeared.append(r.mode), lambda r: None, lambda fn: fn())
    sw.poll_once()
    sw.poll_once()
    assert appeared == []


def test_usage_counts_runs_not_steps(tmp_path: Path) -> None:
    from deskkit.modules.modeshift import cli
    from deskkit.modules.modeshift.config import validate_section

    sysm = fake_system()
    populate(sysm)
    sec = sample_section(tmp_path)
    for m, md in zip(sec["modes"], validate_section(sec, {"game.exe"}).modes, strict=True):
        m["confirmed_hash"] = md.def_hash
    ctx = FakeCtx(tmp_path / "d", sec, games=frozenset({"game.exe"}))
    mod = ModeShiftModule(ctx, backends=sysm.backends())
    mod.start()
    assert mod.service is not None
    cli.handle(mod.service, ["game", "--dry-run"])     # 数えない
    cli.handle(mod.service, ["game"])                  # 8 行だが 1 回
    cli.handle(mod.service, ["--undo"])                # undo 1 回(戻す項目 3 行)
    sysm.procs.close_behavior["mail.exe"] = "notray"
    sysm.procs.add("mail.exe")
    cli.handle(mod.service, ["game"])                  # still_running を含む 1 回
    series = {s.key: s for s in mod.usage(7)}
    assert len(series["switches"].per_day) == 7
    assert series["switches"].per_day[-1] == 2 and series["switches"].primary and series["switches"].hint
    assert sum(series["switches"].per_day[:-1]) == 0
    assert series["undos"].per_day[-1] == 1
    assert series["problems"].per_day[-1] == 1 and series["problems"].good_when == "low"
    mod.stop()


def test_quick_actions_rebuilt_with_modes(tmp_path: Path) -> None:
    sysm = fake_system()
    ctx = FakeCtx(tmp_path / "d", sample_section(tmp_path), games=frozenset({"game.exe"}))
    mod = ModeShiftModule(ctx, backends=sysm.backends())
    mod.start()
    labels = [q[0] for q in ctx.quick]
    assert labels == ["ゲーム をプレビュー", "勉強 をプレビュー", "元に戻す内容を確かめる"]
    game = ctx.quick[0]
    assert "game" in game[2].split() and "ゲーム" in game[2].split()
    assert game[4] is not None and game[4]() is True
    assert ctx.quick[2][4] is not None and ctx.quick[2][4]() is False     # 記録が無いので無効
    sec = ctx.settings_dict()
    sec["modes"][1]["label"] = "集中"
    mod.save_section(sec)
    assert [q[0] for q in ctx.quick][:2] == ["ゲーム をプレビュー", "集中 をプレビュー"]
    mod.stop()


def test_colors_follow_theme_at_runtime() -> None:
    from deskkit.modules.modeshift import visuals
    from deskkit.ui import theme

    before = theme.MODE
    import deskkit.catalog as catalog

    mods = catalog.MODULES
    try:
        theme.set_mode("dark")
        dark = (visuals.accent(), visuals.result_style("ok")[0], visuals.mode_accent({"accent": "info"}, 0))
        theme.set_mode("light")
        light = (visuals.accent(), visuals.result_style("ok")[0], visuals.mode_accent({"accent": "info"}, 0))
        assert dark != light
        assert light[2] == theme.INFO
    finally:
        catalog.MODULES = mods
        theme.set_mode(before)
