# host 部品の単体テスト(設定の検証と原子的書き込み・ホットキー表記・イベントバス・自動起動コマンド)。
from __future__ import annotations

import json
from pathlib import Path

import pytest

from deskkit.events import EventBus
from deskkit.hotkeys import format_hotkey, parse_hotkey
from deskkit.settings import SettingsError, SettingsStore


def test_settings_created_with_all_modules_disabled(tmp_path: Path) -> None:
    st = SettingsStore(tmp_path / "settings.json")
    res = st.load()
    assert res.ok and res.created
    data = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert all(sec == {"enabled": False} for sec in data["modules"].values())


def test_syntax_error_keeps_last_good_and_does_not_write(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    st = SettingsStore(p)
    st.load()
    st.set_enabled("dropsort", True)
    p.write_text('{"version": 1,', encoding="utf-8")
    before = p.read_bytes()
    res = st.load()
    assert not res.ok and res.line == 1
    assert st.is_enabled("dropsort")  # 前回正常値のまま
    with pytest.raises(SettingsError):
        st.write_host({"x": 1})
    assert p.read_bytes() == before


def test_broken_section_isolated(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"version": 1, "modules": {"dropsort": [], "clipshelf": {"enabled": True}}}), encoding="utf-8")
    st = SettingsStore(p)
    res = st.load()
    assert res.ok
    assert "dropsort" in st.module_errors and not st.is_enabled("dropsort")
    assert st.is_enabled("clipshelf")


def test_write_module_preserves_other_keys(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    st = SettingsStore(p)
    st.load()
    st.write_module("layoutkeep", {"enabled": True, "mode": "dry_run"})
    st.write_game_processes(["game.exe"])
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["modules"]["layoutkeep"]["mode"] == "dry_run"
    assert data["game_processes"] == ["game.exe"]
    assert st.game_processes() == frozenset({"game.exe"})


@pytest.mark.parametrize("text", ["Ctrl+Shift+Space", "Alt+F4", "Ctrl+Alt+V", "Win+Shift+Num5", "Ctrl+;"])
def test_hotkey_roundtrip(text: str) -> None:
    assert format_hotkey(*parse_hotkey(text)) == text


@pytest.mark.parametrize("bad", ["", "Space", "Ctrl+", "Hyper+A", "Ctrl+NoSuchKey"])
def test_hotkey_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_hotkey(bad)


def test_eventbus_registered_only() -> None:
    bus = EventBus()
    got: list[dict[str, object]] = []
    assert bus.on("a", "layout.apply", lambda p: got.append(dict(p)))
    assert not bus.on("a", "nope.event", lambda p: None)
    bus.emit("b", "unknown.event", {})
    bus.emit("b", "layout.apply", {"request_id": "x"})
    assert got == [{"request_id": "x"}]
    bus.drop_owner("a")
    bus.emit("b", "layout.apply", {"request_id": "y"})
    assert len(got) == 1
