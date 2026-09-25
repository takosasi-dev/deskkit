# 作業スレッドまわりのテスト: CLI(host の IPC 経由)をメインスレッドで実行しないこと、
# stop() がメインスレッドを長く止めず、処理中の1件を途中で打ち切らないこと。
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from deskkit.modules.dropsort import module as module_mod
from deskkit.modules.dropsort import selftest as st
from deskkit.modules.dropsort.fakewin32 import FakeWin32
from deskkit.modules.dropsort.module import DropSortModule

from .helpers import FakeCtx, wait_worker

DL = st.DL
PDF = "C:\\Docs\\PDF"


def _module(tmp_path: Path) -> tuple[DropSortModule, FakeCtx, FakeWin32, st.FakeClock]:
    fake = FakeWin32(DL)
    fake.mkdirs(PDF)
    ctx = FakeCtx(tmp_path, {"watch_mode": "poll", "stable_seconds": 0, "rules": [st.rule("pdf", PDF, [".pdf"])]})
    clock = st.FakeClock()
    m = DropSortModule(ctx, api=fake, clock=clock)
    return m, ctx, fake, clock


def test_cli_runs_on_worker_and_keeps_event_loop(qapp: Any, tmp_path: Path) -> None:
    from PySide6.QtCore import QTimer

    m, ctx, fake, clock = _module(tmp_path)
    m.start()
    try:
        fake.add_file(DL + "\\old.pdf", 10, mtime=clock.t - 100)
        m._kick()
        wait_worker(m, ctx)  # 基準線(old.pdf は sort-existing の対象)
        threads: list[str] = []
        orig = fake.MoveFileExW

        def slow_move(src: str, dst: str) -> int:
            threads.append(threading.current_thread().name)
            time.sleep(0.3)
            return orig(src, dst)

        fake.MoveFileExW = slow_move  # type: ignore[method-assign]
        ticks: list[str] = []
        QTimer.singleShot(50, lambda: ticks.append(threading.current_thread().name))
        code, text = m.handle_cli(["sort-existing", "--apply"])
        assert code == 0, text
        assert fake.names_in(PDF) == ["old.pdf"]
        assert threads and threads[0].startswith("dropsort"), threads  # 移動は作業スレッドで
        assert ticks == [threading.main_thread().name], "CLI の実行中にメインスレッドのイベントが止まっていた"
        assert not ctx.errors, ctx.errors
    finally:
        m.stop()


def test_cli_without_qt_app_or_worker_still_answers(tmp_path: Path) -> None:
    m, _ctx, _fake, _clock = _module(tmp_path)
    code, text = m.handle_cli(["status"])  # start 前(作業スレッドなし)でもその場で答える
    assert code == 0 and DL in text


def test_stop_finishes_current_file_then_stops(tmp_path: Path) -> None:
    m, ctx, fake, clock = _module(tmp_path)
    m.start()
    m._kick()
    wait_worker(m, ctx)  # 基準線(空)
    for i in range(10):
        fake.add_file(DL + f"\\f{i}.pdf", 10, mtime=clock.t)
    started = threading.Event()
    orig = fake.MoveFileExW

    def slow_move(src: str, dst: str) -> int:
        started.set()
        time.sleep(0.2)
        return orig(src, dst)

    fake.MoveFileExW = slow_move  # type: ignore[method-assign]
    m._kick()
    assert started.wait(5)
    t0 = time.monotonic()
    m.stop()
    took = time.monotonic() - t0
    assert took < 2.0, took
    moved = fake.names_in(PDF)
    left = fake.names_in(DL)
    assert 1 <= len(moved) <= 2, moved  # 処理中の1件は終える・残りは次回
    assert len(moved) + len(left) == 10  # 途中で消えたファイルは無い


def test_stop_wait_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module_mod, "STOP_WAIT_S", 0.2)
    m, ctx, fake, clock = _module(tmp_path)
    m.start()
    m._kick()
    wait_worker(m, ctx)
    fake.add_file(DL + "\\a.pdf", 10, mtime=clock.t)
    fake.add_file(DL + "\\b.pdf", 10, mtime=clock.t)
    started = threading.Event()
    release = threading.Event()
    orig = fake.MoveFileExW
    worker: list[threading.Thread] = []

    def blocked_move(src: str, dst: str) -> int:
        worker.append(threading.current_thread())
        started.set()
        release.wait(10)
        return orig(src, dst)

    fake.MoveFileExW = blocked_move  # type: ignore[method-assign]
    m._kick()
    assert started.wait(5)
    t0 = time.monotonic()
    m.stop()
    assert time.monotonic() - t0 < 1.5  # 上限で待つのをやめる
    assert fake.names_in(PDF) == []  # まだ1件目の途中
    release.set()
    worker[0].join(5)
    assert not worker[0].is_alive()
    assert len(fake.names_in(PDF)) == 1 and len(fake.names_in(DL)) == 1  # 1件目は最後まで終え、2件目には進まない
