# v0.2 の本体機能のテスト: 一時停止(H2)・通知の保留(H1)・設定の世代(H3)・診断レポート(H4)・ctx の新 API。
from __future__ import annotations

import datetime as _dt
import getpass
import json
import os
import types
from pathlib import Path
from typing import Any

from deskkit.events import REGISTERED_EVENTS


def _pump(app: Any, n: int = 10) -> None:
    for _ in range(n):
        app.processEvents()


# ------------------------------------------------------------------ 契約
def test_new_events_registered() -> None:
    assert {"modeshift.reverted", "host.snooze_changed"} <= REGISTERED_EVENTS


def test_list_modes_skips_broken() -> None:
    from deskkit.context import list_modes

    sec = {"modes": [{"name": "game", "label": "ゲーム"}, {"name": "study"}, {"label": "名前なし"}, "x", {"name": 3},
                     {"name": "game", "label": "重複"}, {"name": "dev", "label": ""}]}
    assert list_modes(sec) == [("game", "ゲーム"), ("study", "study"), ("dev", "dev")]
    assert list_modes({}) == [] and list_modes({"modes": "bad"}) == []


# ------------------------------------------------------------------ H2 一時停止
class _Clock:
    def __init__(self) -> None:
        self.t = _dt.datetime(2026, 9, 25, 12, 0, 0)

    def __call__(self) -> _dt.datetime:
        return self.t


def test_snooze_timed_and_resume(qapp: Any) -> None:
    from deskkit.snooze import SnoozeManager

    clock = _Clock()
    sm = SnoozeManager(lambda: None, lambda: False, now=clock)
    changes: list[dict[str, Any]] = []
    sm.changed.connect(lambda: changes.append(sm.payload()))
    assert not sm.is_snoozed()
    sm.snooze(30)
    assert sm.is_snoozed()
    assert changes[-1] == {"snoozed": True, "until": "2026-09-25T12:30:00"}
    assert "12:30 まで" in sm.status_text()
    clock.t += _dt.timedelta(minutes=31)
    assert not sm.is_snoozed()  # タイマーより前でも時刻で判定する(別スレッドからの参照も正しい)
    sm._expire()
    assert changes[-1] == {"snoozed": False, "until": None} and sm.auto_resumed
    sm.snooze(None)
    assert sm.is_snoozed() and sm.payload()["until"] is None and "再開するまで" in sm.status_text()
    sm.resume()
    assert not sm.is_snoozed() and changes[-1]["snoozed"] is False


def test_snooze_follows_quns_only_when_enabled(qapp: Any) -> None:
    from deskkit.snooze import SnoozeManager

    state = {"quns": "presentation_mode", "follow": False}
    sm = SnoozeManager(lambda: state["quns"], lambda: bool(state["follow"]))
    assert not sm.is_snoozed()
    state["follow"] = True
    sm.sync_settings()
    assert sm.is_snoozed() and sm.payload() == {"snoozed": True, "until": None}
    state["quns"] = "quiet_time"
    sm._quns_at = 0.0
    assert not sm.is_snoozed()
    sm._poll.stop()


# ------------------------------------------------------------------ H1 通知の保留
def _hold(busy: dict[str, bool], shown: list[Any], enabled: bool = True) -> Any:
    from deskkit.notify_hold import NotificationHold, summary_note

    return NotificationHold(lambda: busy["v"], lambda: enabled, shown.append,
                            lambda held, n: summary_note(held, n, lambda s: s.upper(), None), poll_ms=10)


def test_hold_summarizes_after_game(qapp: Any) -> None:
    from deskkit.notify_hold import Note

    busy = {"v": True}
    shown: list[Any] = []
    h = _hold(busy, shown)
    for i in range(5):
        assert h.offer(Note("dropsort", f"t{i}", "本文", None, "warn" if i == 1 else "info"))
    assert not h.offer(Note("clipshelf", "err", "", None, "error"))  # error はすぐ出す
    assert shown == [] and h.count == 5
    busy["v"] = False
    h._tick()
    assert len(shown) == 1
    s = shown[0]
    assert s.title == "保留中の通知 5 件" and s.level == "warn"
    assert "t4" in s.text and "t2" in s.text and "t1" not in s.text and "ほか 2 件" in s.text
    assert "本文" not in s.text
    assert h.count == 0 and not h._timer.isActive()


def test_hold_single_note_is_shown_as_is(qapp: Any) -> None:
    from deskkit.notify_hold import Note

    busy = {"v": True}
    shown: list[Any] = []
    h = _hold(busy, shown)
    n = Note("layoutkeep", "一件だけ", "x", None, "info")
    h.offer(n)
    busy["v"] = False
    h._tick()
    assert shown == [n]


def test_hold_flushes_before_next_when_safe_and_disabled_setting(qapp: Any) -> None:
    from deskkit.notify_hold import Note

    busy = {"v": True}
    shown: list[Any] = []
    h = _hold(busy, shown)
    h.offer(Note("a", "1", "", None, "info"))
    h.offer(Note("a", "2", "", None, "info"))
    busy["v"] = False
    assert not h.offer(Note("a", "3", "", None, "info"))  # 呼び出し側がすぐ出す。保留分は先にまとめて出す
    assert [x.title for x in shown] == ["保留中の通知 2 件"]
    off = _hold({"v": True}, shown, enabled=False)
    assert not off.offer(Note("a", "4", "", None, "info"))


def test_hold_busy_error_does_not_lose_notes(qapp: Any) -> None:
    from deskkit.notify_hold import Note, NotificationHold

    def boom() -> bool:
        raise OSError("x")

    h = NotificationHold(boom, lambda: True, lambda _n: None, lambda _h, _n: _h[0])
    assert not h.offer(Note("a", "1", "", None, "info"))


# ------------------------------------------------------------------ H3 設定の世代
def test_snapshots_dedupe_prune_and_read(tmp_path: Path) -> None:
    from deskkit.settings import default_settings
    from deskkit.snapshots import SnapshotStore

    store = SnapshotStore(tmp_path / "hist", lambda: 3)
    base = default_settings()
    t0 = _dt.datetime(2026, 9, 25, 10, 0, 0)
    assert store.save(base, now=t0) is not None
    assert store.save(base, now=t0 + _dt.timedelta(seconds=1)) is None  # 同じ内容は増やさない
    ui_only = json.loads(json.dumps(base))
    ui_only["host"]["usage_view"] = "table"
    ui_only["host"]["update"]["last_check"] = "2026-09-25T10:00:00"
    assert store.save(ui_only, now=t0 + _dt.timedelta(seconds=2)) is None  # 表示状態だけの違いも増やさない
    for i in range(5):
        d = json.loads(json.dumps(base))
        d["game_processes"] = [f"g{i}.exe"]
        assert store.save(d, now=t0 + _dt.timedelta(minutes=i + 1)) is not None
    snaps = store.list()
    assert len(snaps) == 3  # 古いものから消える
    assert store.read(snaps[0])["game_processes"] == ["g4.exe"]
    (tmp_path / "hist" / "unrelated.json").write_text("{}", encoding="utf-8")
    store.prune()
    assert (tmp_path / "hist" / "unrelated.json").exists()  # 自分の命名以外は消さない


def test_settings_on_written_hook(tmp_path: Path) -> None:
    from deskkit.settings import SettingsStore

    st = SettingsStore(tmp_path / "settings.json")
    st.load()
    hits: list[int] = []
    st.on_written = lambda: hits.append(1)
    st.write_host({"theme": "light"})
    st.set_enabled("dropsort", True)
    assert len(hits) == 2


# ------------------------------------------------------------------ H4 診断レポート
class _FakeApi:
    def register_hotkey(self, hwnd: int, hid: int, mods: int, vk: int) -> bool:
        return True

    def unregister_hotkey(self, hwnd: int, hid: int) -> bool:
        return True

    def get_last_error(self) -> int:
        return 0


def _fake_host(tmp_path: Path) -> Any:
    from deskkit.hotkeys import HotkeyHub
    from deskkit.loader import Slot
    from deskkit.settings import SettingsStore
    from overlaykit import HotkeyRegistry

    user = getpass.getuser()
    profile = os.environ.get("USERPROFILE") or str(Path.home())
    st = SettingsStore(tmp_path / "settings.json")
    st.load()
    st.set_enabled("dropsort", True)
    hub = HotkeyHub.__new__(HotkeyHub)
    hub.registry = HotkeyRegistry(0, _FakeApi())
    hub.conflicts, hub.combos, hub.failed, hub._callbacks = [], {}, {}, {}
    hub.register("host.quick", 0x3, 0x20)
    hub.note_conflict("clipshelf.plain_text", "Ctrl+Shift+V")

    class _Mod:
        def diagnostics(self) -> dict[str, Any]:
            return {"rules": 3, "dry_run": True, "last_dir": f"{profile}\\Downloads\\x.pdf", "who": user,
                    "site": "https://example.com/secret?q=1", "app": "notepad.exe", "obj": object()}

    ctx = types.SimpleNamespace(error_totals={"event:layout.apply": 2, f"tray:{user} のモード": 1})
    slots = {
        "dropsort": Slot("dropsort", "running", None, _Mod(), ctx),
        "layoutkeep": Slot("layoutkeep", "stopped", f"FileNotFoundError: '{profile}\\AppData\\x.json' ({user})"),
    }
    updates = types.SimpleNamespace(cfg=lambda: {"auto_check": True, "repo": "takosasi-dev/deskkit", "last_check": None},
                                    state="idle")
    return types.SimpleNamespace(settings=st, loader=types.SimpleNamespace(slots=slots), hotkeys=hub, updates=updates,
                                 dpi_awareness="per_monitor_aware_v2", snooze=None, hold=None, snapshots=None)


def test_diagnostics_report_has_no_personal_data(qapp: Any, tmp_path: Path) -> None:
    from deskkit.diagnostics import build_report

    user = getpass.getuser()
    profile = os.environ.get("USERPROFILE") or str(Path.home())
    rep = build_report(_fake_host(tmp_path))
    low = rep.lower()
    assert user.lower() not in low
    assert profile.lower() not in low
    assert str(tmp_path).lower() not in low
    assert "example.com" not in low and "notepad.exe" not in low and ":\\" not in rep
    assert "downloads" not in low and "x.pdf" not in low and "appdata" not in low
    # 状態は載っている
    assert "dropsort: 有効=はい 状態=running" in rep
    assert "rules: 3" in rep and "dry_run: true" in rep
    assert "host.quick = Ctrl+Alt+Space" in rep
    assert "clipshelf.plain_text = Ctrl+Shift+V" in rep
    assert "event:layout.apply=2" in rep and "tray:*=1" in rep
    assert "layoutkeep: 有効=いいえ 状態=stopped 理由=FileNotFoundError" in rep


def test_scrub() -> None:
    from deskkit.diagnostics import scrub

    assert scrub(r"C:\Users\someone\a b.txt") == "<path> b.txt"
    assert scrub(r"\\server\share\x") == "<path>"
    assert scrub("see http://x.y/z now") == "see <url> now"
    assert scrub("game.EXE crashed") == "<exe> crashed"
    assert scrub("x" * 500).endswith("…")
