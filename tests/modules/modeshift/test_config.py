# 設定の検証(FR-1 / AC-2)と定義ハッシュ(D-3)、powercfg 出力の解析(D-9)。
from __future__ import annotations

from pathlib import Path

from deskkit.modules.modeshift import cli
from deskkit.modules.modeshift.actions.power import PowercfgPower, parse_active, parse_list
from deskkit.modules.modeshift.config import (
    definition_hash,
    fill_defaults,
    normalize_action,
    safe_url,
    validate_section,
)
from deskkit.modules.modeshift.fakes import sample_section

from .conftest import Env

# 実機(日本語 Windows 11)の powercfg 出力の書式(GUID はこの PC の実物)
LIST_JA = """
既存の電源設定 (* アクティブ)
-----------------------------------
電源設定の GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (バランス)
電源設定の GUID: 89ee7eba-0db4-4b3a-8c33-69689521f195  (dynabook 標準) *
"""
ACTIVE_JA = "電源設定の GUID: 89ee7eba-0db4-4b3a-8c33-69689521f195  (dynabook 標準)\n"


def test_ac2_invalid_modes_only_those_disabled(tmp_path: Path) -> None:
    cfg = validate_section(sample_section(tmp_path), {"game.exe"})
    assert cfg.mode("game").valid and cfg.mode("study").valid  # type: ignore[union-attr]
    assert "未知のアクション種別" in cfg.mode("bad_type").reason()  # type: ignore[union-attr]
    assert "game.exe" in cfg.mode("bad_close").reason()  # type: ignore[union-attr]
    assert "http / https" in cfg.mode("bad_url").reason()  # type: ignore[union-attr]
    assert [m.name for m in cfg.valid_modes()] == ["game", "study"]


def test_ac2_list_shows_reasons(env: Env) -> None:
    code, text = cli.handle(env.svc, ["--list"])
    assert code == 0
    lines = {ln.split("\t")[0]: ln for ln in text.splitlines()}
    assert "\t無効\t" in lines["bad_type"] and "wallpaper" in lines["bad_type"]
    assert "\t無効\t" in lines["bad_close"]
    assert "\t無効\t" in lines["bad_url"]
    assert "\t確認済み\t" in lines["game"]


def test_other_validation_rules(tmp_path: Path) -> None:
    fs_games: set[str] = set()
    assert normalize_action({"type": "power_plan", "guid": "not-a-guid"}, fs_games)[1]
    assert normalize_action({"type": "master_volume", "level": 1.5}, fs_games)[1]
    assert normalize_action({"type": "app_volume", "exe": "a.exe", "level": -0.1}, fs_games)[1]
    assert normalize_action({"type": "launch_app", "path": str(tmp_path / "nope.exe")}, fs_games)[1]
    assert normalize_action({"type": "open_path", "path": r"C:\tools\x.lnk"}, fs_games)[1]
    assert not normalize_action({"type": "open_url", "url": "https://example.com/a?b=c"}, fs_games)[1]
    assert normalize_action({"type": "open_url", "url": "javascript:alert(1)"}, fs_games)[1]
    assert safe_url("https://example.com/a?token=x#f") == "https://example.com/a?…"


def test_duplicate_hotkey_disables_later_only(tmp_path: Path) -> None:
    sec = sample_section(tmp_path)
    sec["modes"][1]["hotkey"] = "Ctrl+Alt+G"   # game と同じ
    cfg = validate_section(sec, {"game.exe"})
    assert cfg.mode("game").hotkey == "Ctrl+Alt+G"  # type: ignore[union-attr]
    st = cfg.mode("study")
    assert st is not None and st.valid and st.hotkey is None and st.warnings


def test_hash_changes_with_definition(tmp_path: Path) -> None:
    sec = sample_section(tmp_path)
    h1 = validate_section(sec, set()).mode("study").def_hash  # type: ignore[union-attr]
    sec["modes"][1]["label"] = "別の表示名"          # 表示名はハッシュに含めない
    assert validate_section(sec, set()).mode("study").def_hash == h1  # type: ignore[union-attr]
    sec["modes"][1]["actions"][0]["level"] = 0.2
    assert validate_section(sec, set()).mode("study").def_hash != h1  # type: ignore[union-attr]
    assert definition_hash("a", []) != definition_hash("b", [])


def test_auto_switch_requires_poll_interval(tmp_path: Path) -> None:
    sec = sample_section(tmp_path)
    sec["auto_switch"] = {"enabled": True, "poll_interval_s": None, "rules": [{"exe": "g.exe", "mode": "game", "on_exit": "undo"}]}
    cfg = validate_section(sec, set())
    assert cfg.auto_error and not cfg.auto_active
    sec["auto_switch"]["poll_interval_s"] = 2
    assert validate_section(sec, set()).auto_active


def test_fill_defaults() -> None:
    sec, changed = fill_defaults({"enabled": True})
    assert changed and sec["preview"] == "unconfirmed_only" and sec["allow_force_kill"] is False
    assert sec["auto_switch"] == {"enabled": False, "poll_interval_s": None, "rules": []}
    _, changed2 = fill_defaults(sec)
    assert not changed2


def test_powercfg_parse_real_format() -> None:
    schemes = parse_list(LIST_JA)
    assert [s.guid for s in schemes] == ["381b4222-f694-41f0-9685-ff5bb260df2e", "89ee7eba-0db4-4b3a-8c33-69689521f195"]
    assert [s.active for s in schemes] == [False, True]
    assert schemes[0].name == "バランス"
    assert parse_active(ACTIVE_JA) == "89ee7eba-0db4-4b3a-8c33-69689521f195"
    assert parse_active("予期しない出力") is None


def test_powercfg_setactive_uses_arg_list_and_reads_back() -> None:
    calls: list[list[str]] = []
    state = {"active": "381b4222-f694-41f0-9685-ff5bb260df2e"}

    def runner(args: list[str]) -> tuple[int, str]:
        calls.append(list(args))
        if args[0] == "/setactive":
            state["active"] = args[1]
            return 0, ""
        if args[0] == "/getactivescheme":
            return 0, f"電源設定の GUID: {state['active']}  (x)"
        return 0, LIST_JA

    p = PowercfgPower(runner)
    ok, _ = p.set_active("89ee7eba-0db4-4b3a-8c33-69689521f195")
    assert ok
    assert calls == [["/setactive", "89ee7eba-0db4-4b3a-8c33-69689521f195"], ["/getactivescheme"]]
    assert p.set_active("x; del") == (False, "GUID の形式ではない")


def test_powercfg_unexpected_output_fails_power_steps(env: Env) -> None:
    env.sys.power.broken = True
    code, text = cli.handle(env.svc, ["game"])
    assert code == 4
    row = [r for r in env.ops() if r["type"] == "power_plan"][0]
    assert row["result"] == "failed" and row["reason"] == "出力書式が想定外"
    assert not env.sys.power.set_calls
