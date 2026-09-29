# KeyFree モジュールのテスト(偽の ctx.hotkeys)。一覧の状態(AC-1)・前面(AC-6)・押された組(AC-7)・stop(AC-8)・診断(AC-9)・
# 古い host(AC-13)・中止・busy・release_failed・配列と DeskKit のキーの変化・押して確かめる・CLI(QEventLoop で待つ)・設定・利用状況。
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from deskkit.foreground import ForegroundInfo
from deskkit.hotkeys import FailedKey
from deskkit.modules.keyfree import combos, module, scan, trykey
from deskkit.modules.keyfree.fakes import OldHotkeys

C, A, S, WIN = combos.MOD_CONTROL, combos.MOD_ALT, combos.MOD_SHIFT, combos.MOD_WIN
K, J, SPACE, E = 0x4B, 0x4A, 0x20, 0x45
GAME = ForegroundInfo(hwnd=9, pid=99, exe="game.exe", is_game=True, is_fullscreen=False, is_elevated=False)
FULL = ForegroundInfo(hwnd=9, pid=99, exe="video.exe", is_game=False, is_fullscreen=True, is_elevated=False)


def _scan(m: Any, ctx: Any) -> Any:
    assert m.begin_scan() == scan.STARTED
    ctx.hotkeys.flush()
    return m.result()


# ------------------------------------------------------------------ 状態(AC-1・K-7)
def test_states_and_deskkit_not_probed(make_module: Any) -> None:
    m, ctx = make_module()
    hk = ctx.hotkeys
    hk.used.add((C | A, SPACE))
    hk.errors[(C | S, K)] = 1400
    hk.held["host.quick"] = (C | A | S, J)
    res = _scan(m, ctx)
    assert res.reason == "finished"
    assert res.state((C | A, K)) == scan.FREE
    assert res.state((C | A, SPACE)) == scan.USED
    assert res.state((C | S, K)) == scan.ERROR and res.cells[(C | S, K)].error == 1400
    assert res.state((C | A | S, J)) == scan.DESKKIT
    assert res.held_names[(C | A | S, J)] == "host.quick"
    assert (C | A | S, J) not in hk.calls[0]
    assert len(hk.calls[0]) == 346
    assert res.state((C | A, combos.VK_F12)) == scan.RESERVED
    n = res.counts()
    assert n["used"] == 1 and n["deskkit"] == 1 and n["unavailable"] == 1 + 5 and n["free"] == 344
    assert n["tried"] == 346


def test_registry_deskkit_result_is_kept(make_module: Any) -> None:
    """snapshot に無くても、host が "deskkit" と返した組はそのまま DeskKit にする。"""
    m, ctx = make_module()
    m.begin_scan()
    ctx.hotkeys.held["late.key"] = (C | A, K)   # 始めた後に DeskKit が取った
    ctx.hotkeys.flush()
    res = m.result()
    assert res.state((C | A, K)) == scan.DESKKIT
    assert res.deskkit_changed                   # FR-13
    page_texts = [t for _l, t in _page(m).notices()]
    assert "途中で DeskKit のキーが変わりました。もう一度調べてください" in page_texts


def test_batch_size_is_passed(make_module: Any) -> None:
    m, ctx = make_module({"batch_size": 64})
    m.begin_scan()
    assert ctx.hotkeys.batch_sizes == [64]


def test_include_win_setting(make_module: Any) -> None:
    m, ctx = make_module({"include_win": True})
    m.begin_scan()
    assert len(ctx.hotkeys.calls[0]) == 956


def test_start_does_not_probe(make_module: Any) -> None:
    """INV-5: start() とタイマーでは試さない。"""
    m, ctx = make_module()
    for t in list(ctx.timers):
        t.cb()
    assert ctx.hotkeys.calls == []
    assert ctx.tray and ctx.tray[0][0] == module.TRAY_LABEL
    ctx.tray[0][1]()
    assert ctx.shown == 1          # FR-15: 画面を開くだけ
    assert ctx.hotkeys.calls == []


def test_double_start_ignored(make_module: Any) -> None:
    m, ctx = make_module()
    assert m.begin_scan() == scan.STARTED
    assert m.begin_scan() == scan.RUNNING
    assert len(ctx.hotkeys.calls) == 1


# ------------------------------------------------------------------ 前面(AC-6・FR-8)
def test_foreground_game_blocks_start(make_module: Any) -> None:
    m, ctx = make_module()
    ctx.fg = GAME
    assert m.begin_scan() == scan.BLOCKED
    assert ctx.hotkeys.calls == []
    assert m.notice == ("warn", module.TEXT_BLOCKED)
    ctx.fg = FULL
    assert m.begin_scan() == scan.BLOCKED


def test_own_window_in_front_is_allowed(make_module: Any) -> None:
    import os

    m, ctx = make_module()
    ctx.fg = ForegroundInfo(hwnd=1, pid=os.getpid(), exe="deskkit", is_game=False, is_fullscreen=None, is_elevated=None)
    assert m.begin_scan() == scan.STARTED


def test_foreground_switch_cancels(make_module: Any) -> None:
    m, ctx = make_module()
    m.begin_scan()
    ctx.hotkeys.flush(batches=2)
    assert m.result().running
    assert ctx.active_timers(scan.FG_INTERVAL_MS)
    ctx.run_timers(scan.FG_INTERVAL_MS)          # まだ安全
    assert not ctx.hotkeys.jobs[0].handle.cancelled
    ctx.fg = GAME
    ctx.run_timers(scan.FG_INTERVAL_MS)
    assert ctx.hotkeys.jobs[0].handle.cancelled
    ctx.hotkeys.flush()
    res = m.result()
    assert res.reason == "cancelled" and res.stop_why == "foreground"
    assert res.state((C | A, 0x41)) == scan.FREE        # それまでの結果は残す
    assert any(c.state == scan.PENDING for c in res.cells.values())
    assert not ctx.active_timers(scan.FG_INTERVAL_MS)
    assert ("warn", module.TEXT_BLOCKED) in _page(m).notices()


def test_foreground_error_is_blocked(make_module: Any) -> None:
    m, ctx = make_module()

    def boom() -> Any:
        raise OSError("x")

    ctx.foreground = boom
    assert m.begin_scan() == scan.BLOCKED


# ------------------------------------------------------------------ 中止(FR-6)・stop(AC-8)
def test_user_cancel_keeps_results(make_module: Any) -> None:
    m, ctx = make_module()
    m.begin_scan()
    ctx.hotkeys.flush(batches=1)
    m.cancel_scan()
    ctx.hotkeys.flush()
    res = m.result()
    assert res.reason == "cancelled" and res.stop_why == "user"
    assert res.counts()["free"] == 32
    assert ("info", "途中で止めました") in _page(m).notices()


def test_stop_cancels_scan_and_try(make_module: Any) -> None:
    m, ctx = make_module()
    m.begin_scan()
    ctx.hotkeys.flush(batches=1)
    handle = ctx.hotkeys.jobs[0].handle
    m.stop()
    assert handle.cancelled
    m2, ctx2 = make_module()
    ctx2.hotkeys.flush()
    _scan(m2, ctx2)
    assert m2.try_key((C | A, K)) is None
    assert ctx2.hotkeys.registered == {trykey.HOTKEY_NAME: (C | A, K)}
    m2.stop()
    assert ctx2.hotkeys.registered == {}
    assert trykey.HOTKEY_NAME in ctx2.hotkeys.unregister_log


def test_cancel_before_handle_is_forwarded(make_module: Any) -> None:
    m, ctx = make_module()
    orig = ctx.hotkeys.probe

    def probe(*a: Any, **k: Any) -> Any:
        m.cancel_scan()          # probe() から戻る前に中止が頼まれた
        return orig(*a, **k)

    ctx.hotkeys.probe = probe
    m.begin_scan()
    assert ctx.hotkeys.jobs[0].handle.cancelled


# ------------------------------------------------------------------ 押された組(AC-7・FR-7)
def test_pressed_combo_shown_and_logged_as_count(make_module: Any) -> None:
    m, ctx = make_module()
    m.begin_scan()
    ctx.hotkeys.pressed_plan = [(C | A | S, K)]
    ctx.hotkeys.flush()
    res = m.result()
    assert res.pressed == [(C | A | S, K)]
    texts = [t for _l, t in _page(m).notices()]
    assert any("Ctrl+Alt+Shift+K が押されました" in t for t in texts)
    log = ctx.handler.text()
    assert "pressed=1" in log
    assert m.diagnostics()["pressed"] == 1


# ------------------------------------------------------------------ busy・release_failed・配列
def test_busy_restores_previous_result(make_module: Any) -> None:
    m, ctx = make_module()
    first = _scan(m, ctx)
    ctx.hotkeys.busy = True
    m.begin_scan()
    ctx.hotkeys.flush()
    assert m.result() is first
    assert m.scanner.last_busy
    assert ("warn", module.TEXT_BUSY) in _page(m).notices()


def test_release_failed(make_module: Any) -> None:
    m, ctx = make_module()
    ctx.hotkeys.release_fail = True
    res = _scan(m, ctx)
    assert res.reason == "release_failed"
    assert any("キーを返せませんでした" in t for _l, t in _page(m).notices())


def test_layout_change_is_reported(make_module: Any) -> None:
    m, ctx = make_module()
    m.begin_scan()
    m.kb.chars[0xBA] = ":"          # type: ignore[attr-defined]
    ctx.hotkeys.flush()
    res = m.result()
    assert res.layout_changed
    assert res.chars[0xBA] == ";"   # 表示名は始めた時点の配列のまま
    assert ("warn", "配列が変わりました。もう一度調べてください") in _page(m).notices()


def test_probe_exception_is_failed(make_module: Any) -> None:
    m, ctx = make_module()

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("x")

    ctx.hotkeys.probe = boom
    assert m.begin_scan() == scan.FAILED
    assert m.result().reason == "error"
    assert not m.scanner.running


# ------------------------------------------------------------------ 古い host(AC-13・FR-17)
def test_old_host_unsupported(make_module: Any) -> None:
    m, ctx = make_module(hotkeys=OldHotkeys())
    assert not m.supported()
    assert m.begin_scan() == scan.UNSUPPORTED
    assert m.handle_cli(["scan"])[0] == module.EXIT_UNAVAILABLE
    assert m.handle_cli(["check", "Ctrl+Alt+K"])[0] == module.EXIT_UNAVAILABLE
    assert ctx.status == "この DeskKit では使えません"


# ------------------------------------------------------------------ 1つだけ・調べ直す(FR-5・FR-9)
def test_parse_text_messages(make_module: Any) -> None:
    m, _ctx = make_module()
    assert m.parse_text("Ctrl+Alt+Space") == ((C | A, SPACE), None)
    assert m.parse_text("Win+E") == ((WIN, E), None)
    assert m.parse_text("K") == (None, module.TEXT_NO_MODS)
    assert m.parse_text("F5") == (None, module.TEXT_NO_MODS)
    assert m.parse_text("Ctrl+Alt+Nope") == (None, module.TEXT_BAD_SYNTAX)
    assert m.parse_text("") == (None, module.TEXT_BAD_SYNTAX)


def test_check_one_single_modifier(make_module: Any) -> None:
    m, ctx = make_module()
    got: list[scan.SingleResult] = []
    assert m.check((WIN, E), got.append) == scan.STARTED
    assert ctx.hotkeys.calls == [[(WIN, E)]] and ctx.hotkeys.batch_sizes == [1]
    ctx.hotkeys.flush()
    assert got[0].state == scan.FREE


def test_check_reserved_and_deskkit_without_probe(make_module: Any) -> None:
    m, ctx = make_module()
    ctx.hotkeys.held["jotdrop.open_input"] = (C | A, J)
    got: list[scan.SingleResult] = []
    assert m.check((WIN, combos.VK_L), got.append) == scan.DONE
    assert m.check((C | A, combos.VK_DELETE), got.append) == scan.DONE
    assert m.check((S, combos.VK_F12), got.append) == scan.DONE
    assert m.check((C | A, J), got.append) == scan.DONE
    assert [r.state for r in got] == [scan.RESERVED, scan.RESERVED, scan.RESERVED, scan.DESKKIT]
    assert got[3].holder == "jotdrop.open_input"
    assert ctx.hotkeys.calls == []


def test_recheck_updates_table(make_module: Any) -> None:
    m, ctx = make_module()
    ctx.hotkeys.used.add((C | A, SPACE))
    _scan(m, ctx)
    ctx.hotkeys.used.clear()            # 利用者が心当たりのアプリを閉じた
    got: list[scan.SingleResult] = []
    m.check((C | A, SPACE), got.append)
    ctx.hotkeys.flush()
    assert got[0].state == scan.FREE
    assert m.result().state((C | A, SPACE)) == scan.FREE


def test_check_while_scanning_is_busy(make_module: Any) -> None:
    m, ctx = make_module()
    m.begin_scan()
    assert m.check((C | A, K), lambda _r: None) == scan.RUNNING


def test_check_blocked_by_foreground(make_module: Any) -> None:
    m, ctx = make_module()
    ctx.fg = GAME
    assert m.check((C | A, K), lambda _r: None) == scan.BLOCKED
    assert ctx.hotkeys.calls == []


# ------------------------------------------------------------------ 押して確かめる(K-9・FR-12・INV-2)
class _Mono:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def test_try_key_received(make_module: Any) -> None:
    m, ctx = make_module()
    _scan(m, ctx)
    assert m.try_key((C | A, K)) is None
    assert m.trykey.active == (C | A, K)
    ctx.hotkeys.fire(trykey.HOTKEY_NAME)
    assert m.trykey.result == trykey.RECEIVED
    assert m.trykey.active is None
    assert ctx.hotkeys.registered == {}


def test_try_key_timeout_never_exceeds(make_module: Any) -> None:
    mono = _Mono()
    m, ctx = make_module({"try_seconds": 5}, mono=mono)
    _scan(m, ctx)
    m.try_key((C | A, K))
    ends = [t for t in ctx.timers if t.single_shot and t.active]
    assert [t.ms for t in ends] == [5000]
    mono.t += 4.0
    ctx.run_timers(250)
    assert m.trykey.active is not None and m.trykey.remaining() == 1
    mono.t += 1.0
    ctx.run_timers(250)                 # 終わりのタイマーが遅れても、刻みの方で返す
    assert m.trykey.result == trykey.TIMEOUT
    assert ctx.hotkeys.registered == {}


def test_try_key_conflict_marks_used(make_module: Any) -> None:
    m, ctx = make_module()
    _scan(m, ctx)
    ctx.hotkeys.used.add((C | A, K))    # 直前にほかのアプリが取った
    assert m.try_key((C | A, K)) == trykey.CONFLICT
    assert m.result().state((C | A, K)) == scan.USED
    assert ctx.hotkeys.registered == {}
    ctx.hotkeys.errors[(C | A, J)] = 87
    assert m.try_key((C | A, J)) == trykey.FAILED
    assert m.trykey.error == 87


def test_scan_ends_try_first(make_module: Any) -> None:
    """§10: 押して確かめるの途中で「調べる」→ 先に返してから一覧。"""
    m, ctx = make_module()
    _scan(m, ctx)
    m.try_key((C | A, K))
    m.begin_scan()
    assert ctx.hotkeys.registered == {}
    assert m.trykey.result == trykey.CANCELLED
    assert (C | A, K) in ctx.hotkeys.calls[-1]


def test_try_key_refused_while_scanning(make_module: Any) -> None:
    m, ctx = make_module()
    m.begin_scan()
    assert m.try_key((C | A, K)) == scan.RUNNING
    assert ctx.hotkeys.register_log == []


# ------------------------------------------------------------------ CLI(FR-14)
def _cli(m: Any, ctx: Any, *args: str) -> tuple[int, str]:
    ctx.hotkeys.auto = True             # probe の後に QTimer で結果を流す(handle_cli は QEventLoop で待つ)
    return m.handle_cli(list(args))  # type: ignore[no-any-return]


def test_cli_check_codes(make_module: Any) -> None:
    m, ctx = make_module()
    hk = ctx.hotkeys
    hk.used.add((C | A, SPACE))
    hk.errors[(C | S, K)] = 1400
    hk.held["host.quick"] = (C | A | S, J)
    assert _cli(m, ctx, "check", "Ctrl+Alt+K") == (0, "Ctrl+Alt+K: 空き")
    assert _cli(m, ctx, "check", "Ctrl+Alt+Space")[0] == 20
    assert _cli(m, ctx, "check", "Ctrl+Alt+Shift+J") == (21, "Ctrl+Alt+Shift+J: DeskKit(host.quick)")
    assert _cli(m, ctx, "check", "Ctrl+Shift+K") == (22, "Ctrl+Shift+K: 調べられない(エラー 1400)")
    assert _cli(m, ctx, "check", "Win+L")[0] == 22
    assert _cli(m, ctx, "check", "Ctrl+Alt+Nope") == (1, module.TEXT_BAD_SYNTAX)
    assert _cli(m, ctx, "check", "K") == (1, module.TEXT_NO_MODS)
    assert _cli(m, ctx, "check", "Ctrl + Alt + K")[0] == 0                        # 空白入りの書き方も読む
    assert m.handle_cli(["bogus"])[0] == 2


def test_cli_check_busy_and_blocked(make_module: Any) -> None:
    m, ctx = make_module()
    ctx.hotkeys.busy = True
    assert _cli(m, ctx, "check", "Ctrl+Alt+K")[0] == 24
    ctx.hotkeys.busy = False
    ctx.fg = GAME
    assert _cli(m, ctx, "check", "Ctrl+Alt+K")[0] == 23


def test_cli_check_timeout(make_module: Any, monkeypatch: Any) -> None:
    m, ctx = make_module()
    monkeypatch.setattr(module, "CLI_CHECK_TIMEOUT_S", 0.1)
    code, _ = m.handle_cli(["check", "Ctrl+Alt+K"])   # 偽物は flush されない = 返事が来ない
    assert code == 23
    assert ctx.hotkeys.jobs[0].handle.cancelled


def test_cli_scan_lists_free(make_module: Any) -> None:
    m, ctx = make_module({"include_win": True})
    ctx.hotkeys.used.add((C | A, SPACE))
    code, out = _cli(m, ctx, "scan")
    assert code == 0
    lines = out.splitlines()
    assert len(ctx.hotkeys.calls[0]) == 347         # CLI は既定の範囲
    assert "Ctrl+Alt+Space" not in lines
    assert "Ctrl+Alt+K" in lines
    assert not any("PrintScreen" in s or s.startswith("Win") for s in lines)   # Windows 用は出さない
    assert len(lines) == 347 - 1 - 4                 # 使用中 1 と PrintScreen 4 を除く


def test_cli_scan_busy_cancel_blocked(make_module: Any) -> None:
    m, ctx = make_module()
    ctx.hotkeys.busy = True
    assert _cli(m, ctx, "scan")[0] == 24
    ctx.hotkeys.busy = False
    ctx.fg = GAME
    assert _cli(m, ctx, "scan")[0] == 23
    ctx.fg = ForegroundInfo(hwnd=5, pid=4242, exe="t.exe", is_game=False, is_fullscreen=False, is_elevated=False)
    m.begin_scan()                                   # 画面の一覧が動いている
    assert m.handle_cli(["scan"])[0] == 24


def test_cli_scan_cancelled_is_23(make_module: Any) -> None:
    m, ctx = make_module()
    orig = ctx.hotkeys.probe

    def probe(*a: Any, **k: Any) -> Any:
        h = orig(*a, **k)
        h.cancel()
        return h

    ctx.hotkeys.probe = probe
    assert _cli(m, ctx, "scan")[0] == 23


# ------------------------------------------------------------------ 設定・利用状況・診断
def test_settings_normalize() -> None:
    cfg, changed = module.normalize({"enabled": True, "batch_size": 1000, "try_seconds": "x", "recommend_count": 0,
                                     "include_win": 1, "recommend_order": ["Alt+Shift", "bad"]})
    assert changed
    assert cfg["batch_size"] == 128 and cfg["try_seconds"] == 10 and cfg["recommend_count"] == 1
    assert cfg["include_win"] is False
    assert cfg["recommend_order"] == ["Alt+Shift"]
    assert "enabled" not in cfg
    same, changed2 = module.normalize(dict(module.DEFAULTS))
    assert not changed2 and same == module.DEFAULTS


def test_defaults_written_back(make_module: Any) -> None:
    _m, ctx = make_module()
    assert ctx.writes and ctx.writes[0]["batch_size"] == 32 and ctx.writes[0]["recommend_order"][0] == "Ctrl+Alt+Shift"


def test_set_option(make_module: Any) -> None:
    m, ctx = make_module()
    assert m.set_option("try_seconds", 99) is None
    assert m.cfg["try_seconds"] == 30 and ctx.writes[-1]["try_seconds"] == 30


def test_usage_and_ops_have_counts_only(make_module: Any) -> None:
    now = datetime(2026, 9, 28, 12, 0)
    m, ctx = make_module(wall=lambda: now)
    _scan(m, ctx)
    m.check((C | A, K), lambda _r: None)
    ctx.hotkeys.flush()
    m.note_copy()
    rows = [json.loads(s) for s in m.ops.path.read_text(encoding="utf-8").splitlines()]
    assert [r["event"] for r in rows] == ["scan", "check", "copy"]
    raw = m.ops.path.read_text(encoding="utf-8")
    for s in ("Ctrl", "Alt", "Shift", "Win"):
        assert s not in raw
    series = {s.key: s for s in m.usage(7)}
    assert series["scans"].primary
    from deskkit.usage import count_jsonl

    assert sum(count_jsonl(m.ops.path, 7, lambda r: r.get("event") in ("scan", "check"), today=now.date())) == 2
    assert sum(count_jsonl(m.ops.path, 7, lambda r: r.get("event") == "copy", today=now.date())) == 1


def test_ops_prune(make_module: Any) -> None:
    now = datetime(2026, 9, 28, 12, 0)
    m, ctx = make_module(wall=lambda: now, start=False)
    m.ops.path.parent.mkdir(parents=True, exist_ok=True)
    old = (now - timedelta(days=200)).isoformat(timespec="seconds")
    new = (now - timedelta(days=1)).isoformat(timespec="seconds")
    m.ops.path.write_text(f'{{"ts": "{old}", "event": "scan"}}\nbroken\n{{"ts": "{new}", "event": "copy"}}\n', encoding="utf-8")
    m.start()
    assert [json.loads(s)["event"] for s in m.ops.path.read_text(encoding="utf-8").splitlines()] == ["copy"]


def test_diagnostics_counts_only(make_module: Any) -> None:
    m, ctx = make_module()
    assert m.diagnostics() == {"tried": 0, "free": 0, "used": 0, "deskkit": 0, "unavailable": 0, "pressed": 0, "ms": 0}
    ctx.hotkeys.used.add((C | A, SPACE))
    _scan(m, ctx)
    d = m.diagnostics()
    assert d["tried"] == 347 and d["used"] == 1 and d["free"] == 346 and d["unavailable"] == 5
    assert set(d) == {"tried", "free", "used", "deskkit", "unavailable", "pressed", "ms"}
    assert all(isinstance(v, int) for v in d.values())


def test_status_text(make_module: Any) -> None:
    m, ctx = make_module()
    assert ctx.status == "まだ調べていません"
    m.begin_scan()
    assert ctx.status == "調べています"
    ctx.hotkeys.flush()
    assert ctx.status == "空き 347 組"


def test_snapshot_failed_keys_passed_through(make_module: Any) -> None:
    m, ctx = make_module()
    ctx.hotkeys.failed = [FailedKey("host.quick", C | A, SPACE, 1409)]
    snap = m.snapshot()
    assert snap is not None and snap.failed[0].error == 1409


# ------------------------------------------------------------------ 画面の部品(テストの補助)
def _page(m: Any) -> Any:
    from deskkit.modules.keyfree.page import KeyFreePage

    return KeyFreePage(m)
