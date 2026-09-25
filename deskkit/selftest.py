# 自己検査(FR-17)。一時フォルダを DESKKIT_HOME にして host をトレイ非表示で起動し、ダミーモジュールで
# 隔離・イベント配送・設定検証・ネイティブハンドラ例外・CLI 振り分けを検査する。exit 0/1。
# 引数でモジュール名や all を渡すと、各モジュールの selftest.run() も実行する。
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from deskkit.catalog import MODULE_NAMES


def _check(results: list[tuple[str, bool, str]], name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'OK' if ok else 'NG'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def run_host() -> int:
    print("host selftest")
    tmp = tempfile.mkdtemp(prefix="deskkit-selftest-")
    os.environ["DESKKIT_HOME"] = tmp
    from deskkit import paths

    settings = {
        "version": 1,
        "game_processes": ["selftest-game.exe"],
        "host": {"handler_error_limit": 3, "log_retention_days": 1, "notification_style": "balloon"},
        "modules": {
            **{n: {"enabled": False} for n in MODULE_NAMES},
            "_selftest_ok": {"enabled": True},
            "_selftest_badstart": {"enabled": True},
            "_selftest_badnative": {"enabled": True},
            "_selftest_badsection": {"enabled": "yes"},
        },
    }
    p = paths.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(settings), encoding="utf-8")

    from PySide6.QtWidgets import QApplication

    from deskkit import win32
    from deskkit.host import Host, dpi_setup

    dpi = dpi_setup()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    host = Host(app, dpi, start_ipc=False, show_tray=False)  # type: ignore[arg-type]
    host.start()
    results: list[tuple[str, bool, str]] = []

    def pump() -> None:
        for _ in range(5):
            app.processEvents()

    pump()
    real = [m for m in sys.modules if m.startswith("deskkit.modules.")]
    _check(results, "AC-3 無効なモジュールを import していない", not real, str(real))
    st = {n: s.state for n, s in host.loader.slots.items()}
    _check(results, "AC-4 start で例外のモジュールだけ停止中", st.get("_selftest_badstart") == "stopped", str(st))
    _check(results, "AC-4 他のモジュールは動作中", st.get("_selftest_ok") == "running", str(st))
    _check(results, "D-9 壊れたセクションのモジュールだけ停止中", st.get("_selftest_badsection") == "stopped", str(st))

    ok_mod = importlib.import_module("deskkit.selftest_modules._selftest_ok")
    mm = host.tray._modules.get("_selftest_ok")  # noqa: SLF001
    acts = [a for a in (mm.menu.actions() if mm else []) if a.text() == "ダミー項目"]
    if acts:
        acts[0].trigger()
    _check(results, "AC-4 動作中モジュールのトレイ項目が動く", bool(ok_mod.tray_hits))

    host.events.emit("test", "layout.apply", {"layout": None, "source": "selftest", "request_id": "t1", "wait_s": 0})
    _check(results, "イベント配送(layout.apply)", bool(ok_mod.received) and ok_mod.received[-1].get("request_id") == "t1")
    try:
        host.events.emit("test", "unknown.event", {})
        _check(results, "AC-9 未登録イベントは例外にならない", True)
    except Exception as e:  # noqa: BLE001
        _check(results, "AC-9 未登録イベントは例外にならない", False, repr(e))

    _check(results, "AC-6 クリップボード購読者0で AddClipboardFormatListener を呼ばない",
           host.native.subscriber_count(win32.WM_CLIPBOARDUPDATE) == 0 and host.native.clipboard_listener_calls == 0)

    bad = importlib.import_module("deskkit.selftest_modules._selftest_badnative")
    limit = 3
    for _ in range(limit - 1):
        win32.user32.SendMessageW(host.native.hwnd, win32.WM_DISPLAYCHANGE, 32, 0)
        pump()
    before = host.loader.slots["_selftest_badnative"].state
    win32.user32.SendMessageW(host.native.hwnd, win32.WM_DISPLAYCHANGE, 32, 0)
    pump()
    after = host.loader.slots["_selftest_badnative"].state
    _check(results, "AC-5 nativeEvent ハンドラの例外で host が落ちない", len(bad.calls) == limit, f"calls={len(bad.calls)}")
    _check(results, "AC-5 handler_error_limit 回目で停止中", before == "running" and after == "stopped", f"{before}->{after}")

    code, out = host.handle_cli(["_selftest_ok", "ping"])
    _check(results, "FR-13 CLI をモジュールへ転送", (code, out) == (0, "pong"), f"{code} {out}")
    code, _ = host.handle_cli(["modeshift", "--list"])
    _check(results, "無効モジュールへの CLI は終了コード 11", code == 11, str(code))

    p.write_text('{"version": 1, "modules": {', encoding="utf-8")
    mtime = p.stat().st_mtime_ns
    time.sleep(0.05)
    host.reload()
    pump()
    _check(results, "AC-13 構文エラーでも動き続け、ファイルを書き換えない",
           p.stat().st_mtime_ns == mtime and host.loader.slots["_selftest_ok"].state == "running")

    # ---- v0.2(docs/INTERFACES_v0.2.md)
    from deskkit.events import REGISTERED_EVENTS

    _check(results, "v0.2 イベント modeshift.reverted / host.snooze_changed が登録済み",
           {"modeshift.reverted", "host.snooze_changed"} <= REGISTERED_EVENTS)
    ctx = host.loader.slots["_selftest_ok"].ctx
    snooze_got: list[dict[str, object]] = []
    host.events.on("_selftest_ok", "host.snooze_changed", lambda pl: snooze_got.append(dict(pl)))
    before_snooze = ctx.is_snoozed() if ctx is not None else None
    host.snooze_for(30)
    during = ctx.is_snoozed() if ctx is not None else None
    host.resume()
    after_snooze = ctx.is_snoozed() if ctx is not None else None
    _check(results, "H2 ctx.is_snoozed() が一時停止に追従", (before_snooze, during, after_snooze) == (False, True, False),
           f"{before_snooze}/{during}/{after_snooze}")
    _check(results, "H2 host.snooze_changed を送る", [bool(x.get("snoozed")) for x in snooze_got] == [True, False], str(snooze_got))
    _check(results, "ctx.list_modes() が list を返す", ctx is not None and ctx.list_modes() == [])

    shown: list[str] = []
    real_busy, real_show = host._foreground_busy, host._show_note
    host._foreground_busy = lambda: True  # type: ignore[method-assign]
    host._show_note = lambda n: shown.append(n.title)  # type: ignore[method-assign]
    try:
        host.notify("host", "保留テスト1", "", None)
        host.notify("host", "保留テスト2", "", None)
        host.notify("host", "エラーはすぐ", "", None, level="error")
        held_ok = shown == ["エラーはすぐ"] and host.hold.count == 2
        host._foreground_busy = lambda: False  # type: ignore[method-assign]
        host.hold._tick()
        _check(results, "H1 ゲーム・全画面中は error 以外を保留し、まとめて出す",
               held_ok and shown[-1] == "保留中の通知 2 件", str(shown))
    finally:
        host._foreground_busy, host._show_note = real_busy, real_show  # type: ignore[method-assign]

    p.write_text(json.dumps(settings), encoding="utf-8")  # 構文エラーの検査で壊したファイルを戻す
    n_before = len(host.snapshots.list())
    host.settings.write_game_processes(["selftest-game.exe", "selftest-2.exe"])
    host.snapshot_now()
    _check(results, "H3 設定を書くと世代が増える", len(host.snapshots.list()) == n_before + 1,
           f"{n_before} -> {len(host.snapshots.list())}")
    rep = host.diagnostics_report()
    home_s = str(Path.home()).lower()
    _check(results, "H4 診断レポートにユーザーのフォルダを含めない", "診断レポート" in rep and home_s not in rep.lower()
           and tmp.lower() not in rep.lower())

    from deskkit.hotkeys import format_hotkey, parse_hotkey

    _check(results, "ホットキー表記の往復", format_hotkey(*parse_hotkey("ctrl+shift+space")) == "Ctrl+Shift+Space")
    _check(results, "AC-11 DPI awareness が Per-Monitor V2", dpi == "per_monitor_aware_v2", dpi)

    host.loader.stop_all()
    host.hotkeys.unregister_all()
    host.native.close_native()
    ng = [r for r in results if not r[1]]
    print(f"host selftest: {len(results) - len(ng)}/{len(results)} OK")
    return 0 if not ng else 1


def run_module(name: str) -> int:
    print(f"{name} selftest")
    try:
        mod = importlib.import_module(f"deskkit.modules.{name}.selftest")
    except ImportError as e:
        print(f"  {name} の selftest を読み込めません: {e}")
        return 1
    return int(mod.run())


def main(args: list[str]) -> int:
    target = args[0] if args else "host"
    if target == "host":
        return run_host()
    if target == "all":
        codes = [run_host()]
        for n in MODULE_NAMES:
            if Path(__file__).with_name("modules").joinpath(n).exists() or getattr(sys, "frozen", False):
                codes.append(run_module(n))
        return 0 if all(c == 0 for c in codes) else 1
    if target in MODULE_NAMES:
        return run_module(target)
    print(f"不明な対象: {target}")
    return 1
