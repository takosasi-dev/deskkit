# KeyFree の自己検査。偽の ctx.hotkeys(Windows のホットキーの登録は本当には行わない)と偽の配列で、一覧の数(K-4)・状態の分け方(K-1・K-7)・
# Windows 用の印とおすすめ(K-5・FR-11)・配列に無い記号(K-10)・中止と前面(FR-6・FR-8)・CLI の終了コード(FR-14)・古い host(FR-17)を確かめる。
# %LOCALAPPDATA%\DeskKit には触れない(一時フォルダを使う)。run() は 0=合格 / 1=不合格。
from __future__ import annotations

import tempfile
from pathlib import Path

from deskkit.modules.keyfree import combos, scan
from deskkit.modules.keyfree.fakes import FakeCtx, FakeKeyboard, OldHotkeys, full_symbols
from deskkit.modules.keyfree.module import KeyFreeModule

C, A, S, WIN = combos.MOD_CONTROL, combos.MOD_ALT, combos.MOD_SHIFT, combos.MOD_WIN
VK_K, VK_SPACE, VK_J, VK_E, VK_PRINT = 0x4B, 0x20, 0x4A, 0x45, 0x2C


class _Result:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, name: str, ok: bool) -> None:
        print(f"  [{'OK' if ok else 'NG'}] {name}")
        if not ok:
            self.failed += 1


def _module(tmp: Path, section: dict[str, object] | None = None, chars: dict[int, str] | None = None) -> tuple[KeyFreeModule, FakeCtx]:
    ctx = FakeCtx(tmp / f"d{len(list(tmp.iterdir()))}", section)
    m = KeyFreeModule(ctx, kb=FakeKeyboard(full_symbols() if chars is None else chars))
    m.start()
    return m, ctx


def _counts(r: _Result) -> None:
    print("組み合わせの数(K-4・K-10)")
    full = full_symbols()
    p = combos.build(False, full)
    r.check("既定は 347 組", len(p.probe) == 347)
    pw = combos.build(True, full)
    r.check("Windows キーも調べると 956 組", len(pw.probe) == 956)
    bad = [c for c in pw.probe if c[1] == combos.VK_F12 or c == (C | A, combos.VK_DELETE) or c == (WIN, combos.VK_L)]
    r.check("F12・Ctrl+Alt+Delete・Win+L は渡さない", not bad)
    less = dict(full)
    less.pop(0xE2)
    r.check("配列に無い記号が1つあると M 個減る", len(combos.build(False, less).probe) == 347 - 4
            and 0xE2 not in combos.build(False, less).keys)


def _states(r: _Result, tmp: Path) -> None:
    print("状態の分け方(K-1・K-7)")
    m, ctx = _module(tmp)
    hk = ctx.hotkeys
    hk.used.add((C | A, VK_SPACE))
    hk.errors[(C | S, VK_K)] = 1400
    hk.held["host.quick"] = (C | A | S, VK_J)
    m.begin_scan()
    hk.flush()
    res = m.result()
    ok = res is not None and res.reason == "finished"
    r.check("一覧が最後まで進む", ok)
    if res is None:
        return
    r.check("成功は空き", res.state((C | A, VK_K)) == scan.FREE)
    r.check("1409 は使用中", res.state((C | A, VK_SPACE)) == scan.USED)
    r.check("1400 は調べられない", res.state((C | S, VK_K)) == scan.ERROR and res.cells[(C | S, VK_K)].error == 1400)
    r.check("DeskKit の組は DeskKit で、probe に渡さない",
            res.state((C | A | S, VK_J)) == scan.DESKKIT and (C | A | S, VK_J) not in hk.calls[0])
    r.check("batch_size(既定 32)を渡す", hk.batch_sizes[0] == 32)
    recs = m.recommendations()
    r.check("おすすめは英字・数字で Windows 用でない空き", bool(recs) and all(
        res.state(c) == scan.FREE and not combos.windows_mark(*c) and (0x41 <= c[1] <= 0x5A or 0x30 <= c[1] <= 0x39)
        for c in recs))
    r.check("PrintScreen の組は Windows 用", combos.windows_mark(C | A, VK_PRINT) and combos.windows_mark(WIN | C, VK_K))
    m.stop()


def _cancel(r: _Result, tmp: Path) -> None:
    print("中止と前面(FR-6・FR-8・INV-6)")
    from deskkit.foreground import ForegroundInfo

    m, ctx = _module(tmp)
    game = ForegroundInfo(hwnd=9, pid=99, exe="game.exe", is_game=True, is_fullscreen=True, is_elevated=False)
    ctx.fg = game
    r.check("ゲームが前なら始めない", m.begin_scan() == scan.BLOCKED and not ctx.hotkeys.calls)
    ctx.fg = ForegroundInfo(hwnd=5, pid=4242, exe="editor.exe", is_game=False, is_fullscreen=False, is_elevated=False)
    m.begin_scan()
    ctx.hotkeys.flush(batches=1)
    ctx.fg = game
    ctx.run_timers(scan.FG_INTERVAL_MS)
    ctx.hotkeys.flush()
    res = m.result()
    r.check("途中でゲームが前に来ると cancel", res is not None and res.reason == "cancelled" and res.stop_why == "foreground")
    m.stop()


def _cli(r: _Result, tmp: Path) -> None:
    print("CLI の終了コード(FR-14)")
    m, ctx = _module(tmp)
    hk = ctx.hotkeys
    hk.auto = False
    hk.used.add((C | A, VK_SPACE))
    hk.held["host.quick"] = (C | A | S, VK_J)

    def cli(*args: str) -> int:
        box: list[int] = []
        # 偽物は flush で結果を返す。handle_cli の待ちに入る前に結果が要るので、probe のたびにすぐ流す
        orig = hk.probe

        def probe_now(*a: object, **k: object) -> object:
            h = orig(*a, **k)  # type: ignore[arg-type]
            hk.flush()
            return h

        hk.probe = probe_now  # type: ignore[method-assign]
        try:
            box.append(m.handle_cli(list(args))[0])
        finally:
            hk.probe = orig  # type: ignore[method-assign]
        return box[0]

    r.check("空きは 0", cli("check", "Ctrl+Alt+K") == 0)
    r.check("使用中は 20", cli("check", "Ctrl+Alt+Space") == 20)
    r.check("DeskKit は 21", cli("check", "Ctrl+Alt+Shift+J") == 21)
    r.check("F12 は 22(試さない)", cli("check", "Ctrl+Alt+F12") == 22)
    r.check("書き方の誤りは 1", cli("check", "Ctrl+Alt+Nope") == 1 and cli("check", "K") == 1)
    r.check("scan は 0", cli("scan") == 0)
    m.stop()


def _old_host(r: _Result, tmp: Path) -> None:
    print("古い host(FR-17)")
    ctx = FakeCtx(tmp / "old", hotkeys=OldHotkeys())
    m = KeyFreeModule(ctx, kb=FakeKeyboard())
    m.start()
    r.check("probe が無ければ例外なしで unsupported", m.begin_scan() == scan.UNSUPPORTED and m.notice is not None)
    m.stop()


def run() -> int:
    print("KeyFree 自己検査")
    r = _Result()
    with tempfile.TemporaryDirectory(prefix="keyfree-selftest-") as d:
        tmp = Path(d)
        _counts(r)
        _states(r, tmp)
        _cancel(r, tmp)
        _cli(r, tmp)
        _old_host(r, tmp)
    print("合格" if r.failed == 0 else f"不合格({r.failed} 件)")
    return 0 if r.failed == 0 else 1
