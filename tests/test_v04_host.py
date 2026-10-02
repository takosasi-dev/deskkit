# v0.4 の本体機能のテスト: ホットキーの試して外す(H4-2〜H4-5)・通知の置き換えの鍵(H4-6)・表示名(H4-7)・色(H4-1)。
# Win32 は偽物(RegisterHotKey を本当には呼ばない)。docs/INTERFACES_v0.4.md §2.1・§2.2 の形を確かめる。
from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from deskkit.catalog import MODULE_NAMES

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY = 0x0312


def _pump(app: Any, n: int = 20) -> None:
    for _ in range(n):
        app.processEvents()


def _wait_for(app: Any, cond: Any, timeout: float = 5.0) -> None:
    """別スレッドの結果が GUI スレッドへ届くまで、イベントを回しながら待つ。"""
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.002)
    app.processEvents()


class FakeApi:
    """偽の Win32Api。used の組は 1409、errors の組はその番号で失敗する。呼び出しの順番を calls に残す。"""

    def __init__(self, used: set[tuple[int, int]] | None = None, errors: dict[tuple[int, int], int] | None = None) -> None:
        self.used = used or set()
        self.errors = errors or {}
        self.calls: list[tuple[Any, ...]] = []
        self.held: dict[tuple[int, int], tuple[int, int]] = {}  # (hwnd, id) → (mods, vk)
        self.queue: list[tuple[int, int, int]] = []
        self.last_error = 0
        self.unregister_fail: int = 0  # 0 以外なら、その回数だけ UnregisterHotKey を失敗させる
        self.unregister_raise: int = 0
        self.gate: threading.Event | None = None  # あれば register の前で待つ(busy の確認用)
        self.on_register: Any = None
        self._lock = threading.Lock()

    def register_hotkey(self, hwnd: int, hid: int, mods: int, vk: int) -> bool:
        if self.gate is not None:
            self.gate.wait(5)
        with self._lock:
            self.calls.append(("reg", hwnd, hid, mods, vk, threading.get_ident()))
            key = (mods & 0xF, vk)
            if key in self.used:
                self.last_error = 1409
                return False
            if key in self.errors:
                self.last_error = self.errors[key]
                return False
            self.held[(hwnd, hid)] = key
        if self.on_register is not None:
            self.on_register(hwnd, hid, mods, vk)
        return True

    def unregister_hotkey(self, hwnd: int, hid: int) -> bool:
        with self._lock:
            self.calls.append(("unreg", hwnd, hid, threading.get_ident()))
            if self.unregister_raise:
                self.unregister_raise -= 1
                raise OSError("boom")
            if self.unregister_fail:
                self.unregister_fail -= 1
                self.last_error = 1419
                return False
            self.held.pop((hwnd, hid), None)
            return True

    def get_last_error(self) -> int:
        return self.last_error

    def peek_message(self, msg_min: int, msg_max: int, remove: bool) -> tuple[int, int, int] | None:
        with self._lock:
            self.calls.append(("peek", remove))
            if not remove or not self.queue:
                return None
            return self.queue.pop(0)

    def thread_held(self) -> int:
        return sum(1 for (hwnd, _hid) in self.held if hwnd == 0)


def _registry(api: FakeApi) -> Any:
    from overlaykit import HotkeyRegistry

    return HotkeyRegistry(1234, api)


def _run(reg: Any, combos: list[tuple[int, int]], batch: int = 16) -> tuple[list[Any], list[Any], Any]:
    batches: list[Any] = []
    dones: list[Any] = []
    task = reg.probe(combos, batches.append, dones.append, batch)
    assert task.wait(5)
    return batches, dones, task


# ------------------------------------------------------------------ H4-2 probe(overlaykit の registry)
def test_probe_states_and_deskkit_combo_is_not_tried(qapp: Any) -> None:
    api = FakeApi(used={(MOD_CONTROL | MOD_ALT, 0x20)}, errors={(MOD_CONTROL | MOD_SHIFT, 0x41): 1400})
    reg = _registry(api)
    reg.register("host.quick", MOD_CONTROL | MOD_ALT, 0x4B)  # DeskKit 自身が持つ組
    combos = [(MOD_CONTROL | MOD_ALT, 0x20), (MOD_CONTROL | MOD_SHIFT, 0x41), (MOD_CONTROL | MOD_ALT | MOD_SHIFT, 0x4B),
              (MOD_CONTROL | MOD_ALT, 0x4B)]
    batches, dones, task = _run(reg, combos)
    results = [r for b in batches for r in b.results]
    assert [(r.state, r.error) for r in results] == [("used", 0), ("error", 1400), ("free", 0), ("deskkit", 0)]
    assert dones[-1].reason == "finished" and dones[-1].checked == 4 and dones[-1].error == 0
    assert task.done and reg.probe_holding() == 0 and api.thread_held() == 0
    tried = [(c[3] & 0xF, c[4]) for c in api.calls if c[0] == "reg" and c[1] == 0]
    assert (MOD_CONTROL | MOD_ALT, 0x4B) not in tried  # (b) deskkit の組は試さない
    assert reg.names() == ["host.quick"]  # 自分の登録は壊さない


def test_probe_register_then_unregister_back_to_back_on_one_thread(qapp: Any) -> None:
    api = FakeApi()
    reg = _registry(api)
    combos = [(MOD_CONTROL | MOD_ALT, vk) for vk in range(0x41, 0x41 + 20)]
    batches, dones, _ = _run(reg, combos, batch=8)
    calls = [c for c in api.calls if c[0] != "peek"]
    ids = []
    for i in range(0, len(calls), 2):
        reg_c, unreg_c = calls[i], calls[i + 1]
        assert reg_c[0] == "reg" and unreg_c[0] == "unreg"  # (a) 間に何も挟まない
        assert reg_c[1] == 0 and unreg_c[1] == 0  # hWnd は NULL
        assert reg_c[2] == unreg_c[2]
        assert reg_c[3] & MOD_NOREPEAT  # 普段の登録と同じく MOD_NOREPEAT つきで試す
        assert reg_c[5] == unreg_c[3] != threading.get_ident()  # (a) 専用スレッドで、登録と解除は同じスレッド
        ids.append(reg_c[2])
    assert len(set(ids)) == len(ids) and all(0 <= i <= 0xBFFF for i in ids)  # (c)
    assert [len(b.results) for b in batches] == [8, 8, 4]
    assert all(r.mods == MOD_CONTROL | MOD_ALT for b in batches for r in b.results)  # MOD_NOREPEAT は含めない
    assert api.calls[0] == ("peek", False)  # (d) 初めにキューを作らせる
    assert dones[-1].checked == 20


def test_probe_collects_pressed_between_batches(qapp: Any) -> None:
    api = FakeApi()
    reg = _registry(api)
    pressed_lp = ((0x4B << 16) | (MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT))

    def on_register(hwnd: int, hid: int, mods: int, vk: int) -> None:
        if vk == 0x42:  # 2つ目を試している間に押された
            api.queue.append((WM_HOTKEY, hid, pressed_lp))

    api.on_register = on_register
    batches, _dones, _ = _run(reg, [(MOD_CONTROL | MOD_ALT, 0x41 + i) for i in range(4)], batch=2)
    assert batches[0].pressed == [(MOD_CONTROL | MOD_ALT | MOD_SHIFT, 0x4B)]
    assert batches[1].pressed == []
    assert ("peek", True) in api.calls


def test_probe_cancel_stops_before_next_and_releases(qapp: Any) -> None:
    api = FakeApi()
    reg = _registry(api)
    batches: list[Any] = []
    dones: list[Any] = []
    holder: dict[str, Any] = {}

    def on_register(hwnd: int, hid: int, mods: int, vk: int) -> None:
        if vk == 0x43:
            holder["task"].cancel()  # 別スレッドからの cancel と同じ(次の組の前で止まる)

    api.on_register = on_register
    gate = threading.Event()
    api.gate = gate
    holder["task"] = reg.probe([(MOD_CONTROL | MOD_ALT, 0x41 + i) for i in range(10)], batches.append, dones.append, 4)
    gate.set()
    assert holder["task"].wait(5)
    assert dones[-1].reason == "cancelled" and dones[-1].checked == 3
    assert [r.vk for b in batches for r in b.results] == [0x41, 0x42, 0x43]  # それまでの結果は届く
    assert reg.probe_holding() == 0 and api.thread_held() == 0


def test_probe_exception_releases_and_reports_error(qapp: Any) -> None:
    api = FakeApi()
    api.unregister_raise = 1  # 登録できた直後の解除で例外 → finally で解除し直す
    reg = _registry(api)
    _batches, dones, _ = _run(reg, [(MOD_CONTROL | MOD_ALT, 0x41), (MOD_CONTROL | MOD_ALT, 0x42)])
    assert dones[-1].reason == "error"
    assert reg.probe_holding() == 0 and api.thread_held() == 0


def test_probe_release_failed_stops_and_retries(qapp: Any) -> None:
    api = FakeApi()
    api.unregister_fail = 1
    reg = _registry(api)
    _batches, dones, _ = _run(reg, [(MOD_CONTROL | MOD_ALT, 0x41), (MOD_CONTROL | MOD_ALT, 0x42)])
    assert dones[-1].reason == "release_failed" and dones[-1].error == 1419 and dones[-1].checked == 0
    assert not [c for c in api.calls if c[0] == "reg" and c[4] == 0x42]  # そこで止まる
    assert reg.probe_holding() == 0 and api.thread_held() == 0  # 解除し直せたので預かりは0


def test_probe_only_one_at_a_time(qapp: Any) -> None:
    api = FakeApi()
    gate = threading.Event()
    api.gate = gate
    reg = _registry(api)
    d1: list[Any] = []
    d2: list[Any] = []
    t1 = reg.probe([(MOD_CONTROL | MOD_ALT, 0x41)], lambda b: None, d1.append)
    t2 = reg.probe([(MOD_CONTROL | MOD_ALT, 0x42)], lambda b: None, d2.append)
    assert t2.done and [(d.reason, d.checked) for d in d2] == [("busy", 0)]
    assert reg.probe_active()
    gate.set()
    assert t1.wait(5) and d1[-1].reason == "finished"
    assert not [c for c in api.calls if c[0] == "reg" and c[4] == 0x42]
    t3 = reg.probe([(MOD_CONTROL | MOD_ALT, 0x42)], lambda b: None, d2.append)  # 終われば次を始められる
    assert t3.wait(5) and d2[-1].reason == "finished"


def test_registry_cancel_probe_waits(qapp: Any) -> None:
    api = FakeApi()
    reg = _registry(api)
    dones: list[Any] = []
    task = reg.probe([(MOD_CONTROL | MOD_ALT, 0x41 + i % 26) for i in range(5000)], lambda b: None, dones.append, 128)
    assert reg.cancel_probe(5)
    assert task.done and dones and dones[-1].reason in ("cancelled", "finished") and reg.probe_holding() == 0


# ------------------------------------------------------------------ H4-2〜H4-4 ctx.hotkeys(本物の Host・偽の Win32Api)
@pytest.fixture
def host(qapp: Any, home: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    from deskkit import paths
    from deskkit.host import Host

    settings = {"version": 1, "host": {"quick_action_hotkey": "Ctrl+Alt+Space"},
                "modules": {**{n: {"enabled": False} for n in MODULE_NAMES}, "_selftest_ok": {"enabled": True}}}
    p = paths.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(settings), encoding="utf-8")
    h = Host(qapp, "per_monitor_aware_v2", start_ipc=False, show_tray=False)
    api = FakeApi(used={(MOD_CONTROL | MOD_ALT, 0x20)})  # 日報 2026-09-25 と同じく Ctrl+Alt+Space はほかのアプリが持つ
    h.hotkeys.registry._api = api
    h.fake_api = api
    monkeypatch.setattr(h, "_foreground_busy", lambda: False)
    monkeypatch.setattr(h, "_show_note", lambda n: None)
    h.start()
    _pump(qapp)
    yield h
    h.loader.stop_all()
    h.hotkeys.unregister_all()
    if h._window is not None:
        h._window.deleteLater()
    h.native.close_native()
    _pump(qapp)


def _ctx(host: Any) -> Any:
    return host.loader.slots["_selftest_ok"].ctx


def test_ctx_probe_calls_back_on_gui_thread_after_return(host: Any, qapp: Any) -> None:
    ctx = _ctx(host)
    main = threading.get_ident()
    seen: list[tuple[str, int, Any]] = []
    handle = ctx.hotkeys.probe([(MOD_CONTROL | MOD_ALT, 0x20), (MOD_CONTROL | MOD_ALT | MOD_SHIFT, 0x4B)],
                               lambda b: seen.append(("batch", threading.get_ident(), b)),
                               lambda d: seen.append(("done", threading.get_ident(), d)))
    assert seen == []  # 戻る前には呼ばない
    _wait_for(qapp, lambda: bool(seen) and seen[-1][0] == "done")
    assert [s[0] for s in seen] == ["batch", "done"]
    assert all(s[1] == main for s in seen)  # GUI スレッド
    assert [r.state for r in seen[0][2].results] == ["used", "free"]
    assert seen[1][2].reason == "finished" and handle.done
    assert host.hotkeys.registry.probe_holding() == 0


def test_ctx_probe_busy_is_reported_on_gui_thread(host: Any, qapp: Any) -> None:
    ctx = _ctx(host)
    gate = threading.Event()
    host.fake_api.gate = gate
    d1: list[Any] = []
    d2: list[Any] = []
    ctx.hotkeys.probe([(MOD_CONTROL | MOD_ALT, 0x41)], lambda b: None, d1.append)
    h2 = ctx.hotkeys.probe([(MOD_CONTROL | MOD_ALT, 0x42)], lambda b: None, d2.append)
    assert d2 == [] and h2.done
    _pump(qapp)
    assert [d.reason for d in d2] == ["busy"]
    gate.set()
    _wait_for(qapp, lambda: bool(d1))
    assert d1[-1].reason == "finished"


def test_module_stop_cancels_probe_and_releases(host: Any, qapp: Any) -> None:
    ctx = _ctx(host)
    api = host.fake_api
    started = threading.Event()
    release = threading.Event()

    def on_register(hwnd: int, hid: int, mods: int, vk: int) -> None:
        if hwnd == 0:
            started.set()
            release.wait(0.01)

    api.on_register = on_register
    dones: list[Any] = []
    handle = ctx.hotkeys.probe([(MOD_CONTROL | MOD_ALT, 0x41 + i % 26) for i in range(2000)], lambda b: None, dones.append)
    assert started.wait(5)
    host.loader.stop_one("_selftest_ok")  # stop → host が cancel して預かりを0に戻してから戻る
    assert handle.done
    assert host.hotkeys.registry.probe_holding() == 0 and api.thread_held() == 0
    _pump(qapp)
    assert dones == []  # 止まったモジュールには届けない


def test_snapshot_held_and_failed(host: Any, qapp: Any) -> None:
    ctx = _ctx(host)
    snap = ctx.hotkeys.snapshot()
    assert [(f.name, f.mods, f.vk, f.error) for f in snap.failed] == [("host.quick", MOD_CONTROL | MOD_ALT, 0x20, 1409)]
    assert snap.held == []
    assert ctx.hotkeys.register_text("demo_key", "Ctrl+Alt+Shift+K")
    assert not ctx.hotkeys.register_text("broken", "Ctrl+Nope")  # 表記を読めない物は failed に入れない
    api = host.fake_api
    api.errors[(MOD_CONTROL | MOD_SHIFT, 0x4C)] = 5
    assert not ctx.hotkeys.register_text("other_err", "Ctrl+Shift+L")
    snap = ctx.hotkeys.snapshot()
    assert [(k.name, k.mods, k.vk) for k in snap.held] == [("_selftest_ok.demo_key", MOD_CONTROL | MOD_ALT | MOD_SHIFT, 0x4B)]
    assert {(f.name, f.error) for f in snap.failed} == {("host.quick", 1409), ("_selftest_ok.other_err", 5)}
    ctx.hotkeys.unregister("other_err")
    assert {f.name for f in ctx.hotkeys.snapshot().failed} == {"host.quick"}
    # probe は snapshot の組を deskkit にする
    got: list[Any] = []
    ctx.hotkeys.probe([(MOD_CONTROL | MOD_ALT | MOD_SHIFT, 0x4B)], got.append, lambda d: None)
    _wait_for(qapp, lambda: bool(got))
    assert got[0].results[0].state == "deskkit"


def test_format_parse_roundtrip_for_keyfree_keys(host: Any) -> None:
    hk = _ctx(host).hotkeys
    keys = list(range(0x41, 0x5B)) + list(range(0x30, 0x3A)) + list(range(0x70, 0x88))
    keys += [0xBA, 0xBB, 0xBC, 0xBD, 0xBE, 0xBF, 0xC0, 0xDB, 0xDC, 0xDD, 0xDE, 0xE2]
    keys += [0x20, 0x0D, 0x09, 0x08, 0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x13, 0x2C]
    assert len(keys) == 88
    mods_list = [MOD_CONTROL | MOD_ALT, MOD_CONTROL | MOD_SHIFT, MOD_ALT | MOD_SHIFT, MOD_CONTROL | MOD_ALT | MOD_SHIFT,
                 MOD_WIN | MOD_CONTROL, MOD_WIN | MOD_ALT | MOD_SHIFT, MOD_WIN]
    for m in mods_list:
        for vk in keys:
            text = hk.format(m, vk)
            assert hk.parse(text) == (m, vk), text
    assert hk.format(MOD_CONTROL | MOD_ALT | MOD_SHIFT, 0x4B) == "Ctrl+Alt+Shift+K"
    assert hk.format(MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x20) == "Ctrl+Alt+Space"
    assert hk.format(MOD_CONTROL, 0xE9) == "Ctrl+VKE9" and hk.parse("Ctrl+VKE9") == (MOD_CONTROL, 0xE9)
    assert hk.parse("ctrl + alt + space") == (MOD_CONTROL | MOD_ALT, 0x20)
    assert hk.parse("K") is None and hk.parse("Ctrl+Nope") is None and hk.parse("") is None
    # register_text が受け付けるのと同じ書き方
    ctx = _ctx(host)
    assert ctx.hotkeys.register_text("fmt", hk.format(MOD_ALT | MOD_SHIFT, 0xBC))


def test_diagnostics_shows_probe_counts_only(host: Any) -> None:
    rep = host.diagnostics_report()
    assert "試して外す: なし / 預かり 0 件" in rep


# ------------------------------------------------------------------ H4-5 Win32Api の形
def test_win32api_protocol_has_peek_and_real_api_signature() -> None:
    import inspect

    from overlaykit.hotkey import Win32Api, _RealApi

    assert "peek_message" in dir(Win32Api)
    assert list(inspect.signature(_RealApi.peek_message).parameters) == ["self", "msg_min", "msg_max", "remove"]


@pytest.mark.win32_real
def test_real_probe_thread_hotkey(qapp: Any) -> None:
    """実機: NULL の hWnd で1組を試し、預かりが0に戻る(組は使われにくい Ctrl+Alt+Shift+F23)。"""
    from overlaykit import HotkeyRegistry

    reg = HotkeyRegistry(0)
    batches: list[Any] = []
    dones: list[Any] = []
    t = reg.probe([(MOD_CONTROL | MOD_ALT | MOD_SHIFT, 0x86)], batches.append, dones.append)
    assert t.wait(5)
    assert dones[-1].reason == "finished" and batches[0].results[0].state in ("free", "used")
    assert reg.probe_holding() == 0


# ------------------------------------------------------------------ H4-6 通知の置き換えの鍵
def _hold(busy: dict[str, bool], shown: list[Any]) -> Any:
    from deskkit.notify_hold import NotificationHold, summary_note

    return NotificationHold(lambda: busy["v"], lambda: True, shown.append,
                            lambda held, n: summary_note(held, n, lambda s: s, None))


def test_replace_key_keeps_only_newest_while_held(qapp: Any) -> None:
    from deskkit.notify_hold import Note

    busy = {"v": True}
    shown: list[Any] = []
    h = _hold(busy, shown)
    h.offer(Note("eyebreak", "目を休めませんか 1", "", None, "info", replace_key="eyebreak.prompt"))
    h.offer(Note("eyebreak", "目を休めませんか 2", "", None, "info", replace_key="eyebreak.prompt"))
    assert h.count == 1
    busy["v"] = False
    h._tick()
    assert [n.title for n in shown] == ["目を休めませんか 2"]  # 1件だけなので元の通知がそのまま出る


def test_replace_key_in_summary_and_scoped_by_module(qapp: Any) -> None:
    from deskkit.notify_hold import Note

    busy = {"v": True}
    shown: list[Any] = []
    h = _hold(busy, shown)
    h.offer(Note("eyebreak", "古い声かけ", "", None, "info", replace_key="prompt"))
    h.offer(Note("dropsort", "移動しました", "", None, "info"))
    h.offer(Note("jotdrop", "同じ鍵でも別のモジュール", "", None, "info", replace_key="prompt"))
    h.offer(Note("eyebreak", "新しい声かけ", "", None, "info", replace_key="prompt"))
    h.offer(Note("dropsort", "鍵なしは置き換えない", "", None, "info"))
    assert h.count == 4
    busy["v"] = False
    h._tick()
    s = shown[-1]
    assert s.title == "保留中の通知 4 件"
    assert "新しい声かけ" in s.text and "古い声かけ" not in s.text


def test_replace_key_not_held_shows_both_and_activities_keep_both(host: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    shown: list[Any] = []
    monkeypatch.setattr(host, "_show_note", shown.append)
    ctx = _ctx(host)
    n0 = len(host.activities)
    ctx.notify("a", "", replace_key="k")
    ctx.notify("b", "", replace_key="k")
    assert [n.title for n in shown] == ["a", "b"]  # 保留していないときは普通に出す
    busy = {"v": True}
    monkeypatch.setattr(host, "_foreground_busy", lambda: busy["v"])
    ctx.notify("c", "", replace_key="k")
    ctx.notify("d", "", level="warn", replace_key="k")
    ctx.notify("e", "")  # 今までの呼び方
    assert len(host.activities) == n0 + 5  # 「最近の通知」には全部載る
    busy["v"] = False
    host.hold._tick()
    assert shown[-1].title == "保留中の通知 2 件" and "d" in shown[-1].text and "・_selftest_ok: c" not in shown[-1].text


# ------------------------------------------------------------------ H4-7 表示名
def test_hotkey_label_for_jotdrop() -> None:
    from deskkit.hotkeys import hotkey_label
    from deskkit.ui.main_window import hotkey_label as old_place

    assert hotkey_label("jotdrop.open_input", {}) == "一行メモを書く"
    assert old_place is hotkey_label


def test_unregister_clears_conflict_records(host: Any) -> None:
    # KeyFree P-1: 試しに登録して 1409 で失敗した名前を外したら、「登録できなかったキー」にも残さない
    ctx = _ctx(host)
    api = host.fake_api
    api.errors[(MOD_CONTROL | MOD_SHIFT, 0x4D)] = 1409
    host.hotkeys.conflicts.clear()
    assert not ctx.hotkeys.register_text("try_key", "Ctrl+Shift+M")
    full = "_selftest_ok.try_key"
    assert full in host.hotkeys.failed and [n for n, _ in host.hotkeys.conflicts] == [full]
    ctx.hotkeys.unregister("try_key")
    assert full not in host.hotkeys.failed and host.hotkeys.conflicts == []
    assert "host.quick" in host.hotkeys.failed   # ほかの名前の記録は残る


# ------------------------------------------------------------------ H4-1 色(dataviz の検算の一部: 通常視の差・コントラスト)
def _lin(h: str) -> list[float]:
    out = []
    for i in (1, 3, 5):
        c = int(h[i:i + 2], 16) / 255
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return out


def _oklab(h: str) -> tuple[float, float, float]:
    r, g, b = _lin(h)
    l_ = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m_ = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s_ = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
            1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
            0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_)


def _de(a: str, b: str) -> float:
    x, y = _oklab(a), _oklab(b)
    return 100 * sum((p - q) ** 2 for p, q in zip(x, y, strict=True)) ** 0.5


def _contrast(a: str, b: str) -> float:
    def lum(h: str) -> float:
        r, g, bb = _lin(h)
        return 0.2126 * r + 0.7152 * g + 0.0722 * bb

    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_palettes_adjacent_pairs_and_contrast() -> None:
    from deskkit import catalog
    from deskkit.ui import theme

    light = [theme._LIGHT_MODULE_ACCENTS[n] for n in catalog.MODULE_NAMES]
    dark = [theme._CHART_DARK[n] for n in catalog.MODULE_NAMES]
    ui = [catalog.info(n).accent for n in catalog.MODULE_NAMES]
    for pal, surfaces, floor in ((light, ("#FFFFFF", "#F2F4F9"), 3.0), (dark, ("#151A24", "#1B2130"), 3.0),
                                 (ui, ("#151A24", "#1B2130"), 4.5)):
        assert len(pal) == 15
        for a, b in zip(pal, pal[1:], strict=False):
            assert _de(a, b) >= 15.0, (a, b)  # 通常視の差(validate_palette.js の Normal-vision floor)
        for c in pal:
            for s in surfaces:
                assert _contrast(c, s) >= floor, (c, s)
    # 新しい8色は、どの色とも見分けられる(ダークの画面の色は本体の色 #7C8CFF とも)
    for pal, floor, extra in ((light, 10.0, []), (dark, 8.0, []), (ui, 8.0, ["#7C8CFF"])):
        for i in range(7, 15):
            for j, other in enumerate(pal + extra):
                if j != i:
                    assert _de(pal[i], other) >= floor, (catalog.MODULE_NAMES[i], other)


# ------------------------------------------------------------------ 15 モジュールでも窓が低い画面に収まる(サイドバーはスクロール)
def test_window_fits_low_screens_with_15_modules(host: Any, qapp: Any) -> None:
    host.show_window("plugsave")
    _pump(qapp)
    w = host._window
    assert w.minimumSizeHint().height() <= 660  # 1366×768 の作業領域に収まる(setMinimumSize と同じ高さまで)
    assert w.side_scroll.widget() is w.sidebar and len(w.sidebar.items) == len(MODULE_NAMES) + 4
    w.hide()


# ------------------------------------------------------------------ v0.4.1 ctx.set_quick_action_hotkey(KeyFree から変える)
def test_ctx_set_quick_action_hotkey(host: Any, qapp: Any) -> None:
    ctx = _ctx(host)
    host.show_window("settings")
    _pump(qapp)
    page = host._window.settings_page
    seen: list[str] = []
    host.signals.quick_hotkey_changed.connect(seen.append)
    assert "host.quick" in host.hotkeys.failed  # fixture では Ctrl+Alt+Space がほかのアプリに取られている
    assert ctx.set_quick_action_hotkey("Ctrl+Alt+Shift+K") is True
    assert host.settings.host()["quick_action_hotkey"] == "Ctrl+Alt+Shift+K"
    assert host.hotkeys.combos["host.quick"] == (MOD_CONTROL | MOD_ALT | MOD_SHIFT, 0x4B)
    assert "host.quick" not in host.hotkeys.failed
    assert seen == ["Ctrl+Alt+Shift+K"] and page.qa_key.text() == "Ctrl+Alt+Shift+K"
    # 取られている組: 保存はするが False、競合として知らせる
    assert ctx.set_quick_action_hotkey("Ctrl+Alt+Space") is False
    assert host.settings.host()["quick_action_hotkey"] == "Ctrl+Alt+Space" and "host.quick" in host.hotkeys.failed
    # 競合の通知の本文は表示名(v0.4.1)
    sent: list[tuple[str, str]] = []
    orig = host.notify
    host.notify = lambda src, title, text, *a, **k: (sent.append((title, text)), orig(src, title, text, *a, **k))
    ctx.set_quick_action_hotkey("Ctrl+Alt+Space")
    host.notify = orig
    assert ("登録できなかったホットキーがあります", "DeskKit クイックアクション(Ctrl+Alt+Space)") in sent
    # KeyFree から選んだとき(revert_on_fail)は、取れなければ前のキーに戻す(v0.4.1 レビュー 6)
    assert ctx.set_quick_action_hotkey("Ctrl+Alt+Shift+K") is True
    sent.clear()
    host.notify = lambda src, title, text, *a, **k: (sent.append((title, text)), orig(src, title, text, *a, **k))
    assert ctx.set_quick_action_hotkey("Ctrl+Alt+Space", revert_on_fail=True) is False
    host.notify = orig
    assert host.settings.host()["quick_action_hotkey"] == "Ctrl+Alt+Shift+K"
    assert host.hotkeys.combos["host.quick"] == (MOD_CONTROL | MOD_ALT | MOD_SHIFT, 0x4B)
    assert "host.quick" not in host.hotkeys.failed and sent == [] and page.qa_key.text() == "Ctrl+Alt+Shift+K"
    # 空にすると使わない(成功扱い)
    assert ctx.set_quick_action_hotkey("") is True
    assert "host.quick" not in host.hotkeys.combos and page.qa_key.text() == ""
    host._window.hide()
