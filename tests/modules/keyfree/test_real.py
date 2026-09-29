# 実機のテスト(win32_real。既定では走らない)。本物の host のホットキー窓口(deskkit.hotkeys.ModuleHotkeys と
# overlaykit の HotkeyRegistry)で probe し、別のプロセス(keyfree_measure.py)が持つ組を「使用中」と出すか(AC-11)、
# 一覧が DeskKit 自身の登録を壊さないか(AC-12)を確かめる。預かった組は必ず finally で返す。
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QObject, Qt, Signal

from deskkit.foreground import ForegroundInfo
from deskkit.modules.keyfree import combos, keynames, scan

pytestmark = pytest.mark.win32_real

C, A, S = combos.MOD_CONTROL, combos.MOD_ALT, combos.MOD_SHIFT
VK_J, VK_F10 = 0x4A, 0x79
HOLDER = str(Path(__file__).with_name("keyfree_measure.py"))


class _Poster(QObject):
    call = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    def _run(self, fn: Any) -> None:
        fn()


@pytest.fixture
def real_module(tmp_path: Path, qapp: Any) -> Iterator[tuple[Any, Any, Any]]:
    from deskkit.hotkeys import HotkeyHub, ModuleHotkeys
    from deskkit.modules.keyfree._win32 import RealKeyboard
    from deskkit.modules.keyfree.fakes import FakeCtx
    from deskkit.modules.keyfree.module import KeyFreeModule

    hub = HotkeyHub(0)       # hWnd=NULL: 登録はこのスレッドに結び付く(テストの間だけ)
    ctx = FakeCtx(tmp_path / "data")
    poster = _Poster()
    ctx.post = lambda fn: poster.call.emit(fn)                          # type: ignore[attr-defined]
    ctx.hotkeys = ModuleHotkeys(hub, ctx)                               # type: ignore[arg-type]
    ctx.fg = ForegroundInfo(hwnd=1, pid=os.getpid(), exe="deskkit", is_game=False, is_fullscreen=False, is_elevated=False)
    m = KeyFreeModule(ctx, kb=RealKeyboard())
    m.start()
    try:
        yield m, ctx, hub
    finally:
        m.stop()
        ctx.hotkeys.release_all()
        hub.unregister_all()
        assert hub.registry.probe_holding() == 0


def _holder(*args: str) -> subprocess.Popen[str]:
    return subprocess.Popen([sys.executable, HOLDER, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                            creationflags=0x08000000)   # CREATE_NO_WINDOW


def _release(p: subprocess.Popen[str]) -> None:
    try:
        if p.stdin is not None:
            p.stdin.close()     # 標準入力を閉じると持っていた組を返して終わる
        p.wait(10)
    finally:
        if p.poll() is None:
            p.kill()            # 自分が起こしたテスト用のプロセスだけ
            p.wait(5)


@pytest.mark.parametrize("norepeat", [False, True])
def test_other_process_key_is_used(real_module: tuple[Any, Any, Any], norepeat: bool) -> None:
    """AC-11: 別のプロセスが持つ組を check すると 20、返した後は 0。MOD_NOREPEAT の有無で変わらないこと。"""
    m, _ctx, _hub = real_module
    args = ["hold", str(C | A | S), str(VK_J), "30"] + (["--norepeat"] if norepeat else [])
    p = _holder(*args)
    try:
        line = (p.stdout.readline() if p.stdout is not None else "").strip()
        if line.startswith("FAIL"):
            pytest.skip("Ctrl+Alt+Shift+J をほかのアプリが使っているので確かめられない")
        assert line == "READY"
        code, out = m.handle_cli(["check", "Ctrl+Alt+Shift+J"])
        assert code == 20, out
    finally:
        _release(p)
    code, out = m.handle_cli(["check", "Ctrl+Alt+Shift+J"])
    assert code == 0, out


def test_scan_keeps_own_keys(real_module: tuple[Any, Any, Any]) -> None:
    """AC-12: 一覧の前後で DeskKit 自身の登録が変わらず、別のプロセスからは使用中のまま(= DeskKit が持ち続けている)。"""
    m, ctx, hub = real_module
    try:
        hub.register("host.keyfree_test", C | A | S, VK_F10)
    except Exception:  # noqa: BLE001
        pytest.skip("Ctrl+Alt+Shift+F10 をほかのアプリが使っているので確かめられない")
    before = ctx.hotkeys.snapshot()
    assert m.begin_scan() == scan.STARTED
    assert m._wait(lambda: not m.scanner.running, 30.0)
    res = m.result()
    assert res is not None and res.reason == "finished"
    assert res.state((C | A | S, VK_F10)) == scan.DESKKIT
    assert not res.deskkit_changed
    assert ctx.hotkeys.snapshot() == before
    assert hub.registry.probe_holding() == 0
    r = subprocess.run([sys.executable, HOLDER, "try", str(C | A | S), str(VK_F10)], timeout=20,
                       creationflags=0x08000000)
    assert r.returncode == 20


def test_real_format_parse_roundtrip(real_module: tuple[Any, Any, Any]) -> None:
    m, _ctx, _hub = real_module
    plan = combos.build(True, keynames.layout_chars(m.kb))
    back: Callable[[str], Any] = m.ctx.hotkeys.parse
    for c in plan.combos():
        assert back(m.format(c)) == c
