# PagePress のテスト共通部品: 偽 ctx・偽の PDFium・偽のショートカット・ジョブが終わるまで回す補助。
# 本番のデータ(%LOCALAPPDATA%\DeskKit)・本物の「送る」フォルダ・利用者のファイルには触れない(Qt は offscreen)。
from __future__ import annotations

import copy
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DESKKIT_HOME", str(tmp_path / "home"))


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def text(self) -> str:
        out = []
        for r in self.records:
            out.append(r.getMessage())
            if r.exc_info:
                out.append(logging.Formatter().formatException(r.exc_info))
        return "\n".join(out)


@pytest.fixture
def logs() -> Iterator[ListHandler]:
    h = ListHandler()
    lg = logging.getLogger("deskkit.pagepress")
    lg.addHandler(h)
    root = logging.getLogger()
    root.addHandler(h)
    old = lg.level
    lg.setLevel(logging.DEBUG)
    yield h
    lg.removeHandler(h)
    root.removeHandler(h)
    lg.setLevel(old)


class FakeTimer:
    def __init__(self, ms: int, cb: Callable[[], None], single_shot: bool) -> None:
        self.ms, self.cb, self.single_shot = ms, cb, single_shot
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class FakeCtx:
    def __init__(self, data_dir: Path, section: dict[str, Any] | None = None) -> None:
        self.name = "pagepress"
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.log = logging.getLogger("deskkit.pagepress")
        self._section: dict[str, Any] = {"enabled": True, **(section or {})}
        self.tray: list[tuple[str, Callable[[], None]]] = []
        self.notifications: list[tuple[str, str, str]] = []
        self.status = ""
        self.writes: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.shown = 0
        self.timers: list[FakeTimer] = []
        self._pending: list[Callable[[], None]] = []
        self._mu = threading.Lock()
        self.parent: Any = None

    def settings(self) -> dict[str, Any]:
        return dict(self._section)

    def settings_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._section)

    def write_settings(self, section: dict[str, Any], restart: bool = False) -> None:
        self._section = {**copy.deepcopy(section), "enabled": True}
        self.writes.append(copy.deepcopy(section))

    def add_tray_action(self, label: str, cb: Callable[[], None], **_k: Any) -> None:
        self.tray.append((label, cb))

    def set_tray_status(self, text: str) -> None:
        self.status = text

    def notify(self, title: str, text: str, on_click: Any = None, level: str = "info", replace_key: Any = None) -> None:
        self.notifications.append((title, text, level))

    def is_snoozed(self) -> bool:
        return False

    def safe(self, fn: Callable[..., Any], label: str | None = None) -> Callable[..., Any]:
        def w(*a: Any, **k: Any) -> Any:
            try:
                return fn(*a, **k)
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{label}: {type(e).__name__}: {e}")
                return None

        return w

    def call_soon(self, fn: Callable[[], None]) -> None:
        with self._mu:
            self._pending.append(fn)

    def drain(self) -> int:
        n = 0
        while True:
            with self._mu:
                if not self._pending:
                    return n
                fn = self._pending.pop(0)
            self.safe(fn, "call_soon")()
            n += 1

    def start_timer(self, ms: int, cb: Callable[[], None], single_shot: bool = False) -> FakeTimer:
        t = FakeTimer(ms, cb, single_shot)
        self.timers.append(t)
        return t

    def show_page(self) -> None:
        self.shown += 1

    def window_parent(self) -> Any:
        return self.parent


@pytest.fixture
def ctx(tmp_path: Path) -> FakeCtx:
    return FakeCtx(tmp_path / "home" / "pagepress")


def pump(ctx: FakeCtx, pred: Callable[[], bool], timeout: float = 30.0) -> None:
    """GUI スレッド役として call_soon を回しながら pred を待つ。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        ctx.drain()
        if pred():
            return
        time.sleep(0.01)
    ctx.drain()
    assert pred(), "時間内に終わりませんでした"


class FakeLink:
    """偽の IShellLinkW。作ったショートカットを辞書に持ち、ファイルも作る。"""

    def __init__(self) -> None:
        self.links: dict[str, tuple[str, str]] = {}

    def create(self, path: Path, target: str, args: str, workdir: str, icon: str, description: str) -> None:
        path.write_bytes(b"lnk")
        self.links[str(path)] = (target, args)

    def read(self, path: Path) -> tuple[str, str] | None:
        return self.links.get(str(path))


class FakeBackend:
    """偽の PDFium。呼ばれたスレッドを記録し、gate が閉じている間は描画で待つ(AC-13)。"""

    def __init__(self, gate: threading.Event | None = None, fail_pages: frozenset[int] = frozenset()) -> None:
        self.calls: list[tuple[str, int, int | None]] = []   # (関数, スレッド id, ページ)
        self.gate = gate
        self.fail_pages = fail_pages
        self.rendered: list[int] = []
        self.started = threading.Event()

    def open(self, path: str) -> Any:
        self.calls.append(("open", threading.get_ident(), None))
        return {"path": path}

    def render(self, doc: Any, index: int, side: int) -> Any:
        from PySide6.QtGui import QImage

        self.calls.append(("render", threading.get_ident(), index))
        self.started.set()
        if self.gate is not None:
            self.gate.wait(10)
        if index in self.fail_pages:
            raise RuntimeError("cannot render")
        self.rendered.append(index)
        img = QImage(side // 2, side, QImage.Format.Format_RGB888)
        img.fill(0xFFFFFF)
        return img

    def close(self, doc: Any) -> None:
        self.calls.append(("close", threading.get_ident(), None))

    def version(self) -> str:
        self.calls.append(("version", threading.get_ident(), None))
        return "fake"


def make_module(ctx: FakeCtx, tmp_path: Path, **kw: Any) -> Any:
    from deskkit.modules.pagepress.module import PagePressModule
    from deskkit.modules.pagepress.render import Renderer

    backend = kw.pop("backend", None) or FakeBackend()
    kw.setdefault("renderer_factory", lambda deliver: Renderer(deliver, backend_factory=lambda: backend))
    kw.setdefault("fallback_dir", lambda: tmp_path / "Documents" / "PagePress")
    kw.setdefault("shell_link", FakeLink())
    kw.setdefault("sendto_folder", tmp_path / "SendTo")
    m = PagePressModule(ctx, **kw)
    m.backend = backend
    return m


@pytest.fixture
def module(ctx: FakeCtx, tmp_path: Path) -> Iterator[Any]:
    m = make_module(ctx, tmp_path)
    m.start()
    yield m
    m.stop()


def add_and_wait(m: Any, ctx: FakeCtx, tab: str, paths: list[Path]) -> None:
    m.add_paths(tab, paths)
    pump(ctx, lambda: m.probing[tab] == 0)


def job_done(m: Any) -> bool:
    """ジョブが終わり、結果が GUI スレッド役に届いた(results に入った)。"""
    return m.job is not None and m.job.finished and m._counted is m.job and m.worker.idle()


def run_job(m: Any, ctx: FakeCtx, tab: str, **kw: Any) -> Any:
    err = m.start_job(tab, **kw)
    assert err is None, err
    pump(ctx, lambda: job_done(m), timeout=120)
    return m.job
