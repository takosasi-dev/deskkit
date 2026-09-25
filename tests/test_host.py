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


# ---- 更新直後の起動(単一インスタンスの引き継ぎ・更新ファイルの片付け)
def test_wait_instance_mutex_takes_over_after_release() -> None:
    import ctypes
    import threading
    import uuid

    from deskkit import ipc

    name = f"DeskKit-test-{uuid.uuid4().hex[:8]}"
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    other = k32.CreateMutexW(None, False, "Local\\" + name)  # 「旧プロセス」が持っているミューテックス
    assert other
    assert not ipc.acquire_instance_mutex(name)
    assert not ipc.wait_instance_mutex(0.3, 0.05, name)
    threading.Timer(0.3, lambda: k32.CloseHandle(other)).start()
    assert ipc.wait_instance_mutex(5.0, 0.05, name)


def test_after_update_housekeeping_removes_downloads(home: Path) -> None:
    from deskkit import app, paths

    d = paths.local_dir() / "updates"
    d.mkdir(parents=True)
    (d / "DeskKit-0.2.0.exe").write_bytes(b"MZ")
    (d / "DeskKit-0.2.0.part").write_bytes(b"MZ")
    (d / "keep.txt").write_text("x")
    app.after_update_housekeeping()
    assert sorted(p.name for p in d.iterdir()) == ["keep.txt"]


def test_host_section_types_fall_back_to_defaults(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    bad_host = {"log_retention_days": None, "handler_error_limit": "5", "show_window_on_start": "yes",
                "notification_style": 3, "theme": "neon", "quick_action_hotkey": None, "hold_notifications": 0,
                "settings_history_keep": 10_000, "update": {"auto_check": "no", "repo": 5, "last_check": 12}, "custom": 1}
    p.write_text(json.dumps({"version": 1, "host": bad_host}), encoding="utf-8")
    st = SettingsStore(p)
    assert st.load().ok
    h = st.host()
    assert h["log_retention_days"] == 14 and h["handler_error_limit"] == 5
    assert h["show_window_on_start"] is True and h["hold_notifications"] is True
    assert h["notification_style"] == "toast" and h["theme"] == "dark"
    assert h["quick_action_hotkey"] == ""
    assert h["settings_history_keep"] == 200  # 範囲外は端に寄せる
    assert h["update"]["auto_check"] is True and h["update"]["repo"] == "takosasi-dev/deskkit" and h["update"]["last_check"] is None
    assert h["custom"] == 1  # 知らないキーはそのまま
    int(h["log_retention_days"])  # 起動時の int() で落ちない


def test_host_update_not_a_dict(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"version": 1, "host": {"update": None}}), encoding="utf-8")
    st = SettingsStore(p)
    assert st.load().ok
    assert st.host()["update"]["auto_check"] is True
