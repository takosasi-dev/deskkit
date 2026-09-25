# TwinSweep のテスト共通部品: 偽 ctx(call_soon はキューに積み、テストが画面のスレッドで流す)・合成画像・偽のごみ箱。
# 本物のごみ箱と %LOCALAPPDATA%\DeskKit には触れない(DESKKIT_HOME を一時フォルダへ向ける)。
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
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        text = self.format(record)
        if record.exc_info:
            text += logging.Formatter().formatException(record.exc_info)
        self.lines.append(text)


class FakeCtx:
    def __init__(self, data_dir: Path, section: dict[str, Any] | None = None) -> None:
        self.name = "twinsweep"
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.log = logging.getLogger(f"deskkit.twinsweep.test{id(self)}")
        self.log.setLevel(logging.DEBUG)
        self.log.propagate = False
        self.handler = ListHandler()
        self.log.addHandler(self.handler)
        self._section: dict[str, Any] = {"enabled": True, **(section or {})}
        self.writes: list[dict[str, Any]] = []
        self.status = ""
        self.notifications: list[tuple[str, str]] = []
        self._queue: list[Callable[[], None]] = []
        self._lock = threading.Lock()
        self.errors: list[str] = []

    def settings(self) -> dict[str, Any]:
        return dict(self._section)

    def settings_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._section)

    def write_settings(self, section: dict[str, Any], *, restart: bool = False) -> None:
        self._section = {**copy.deepcopy(section), "enabled": True}
        self.writes.append(copy.deepcopy(section))

    def set_tray_status(self, text: str) -> None:
        self.status = text

    def notify(self, title: str, text: str, on_click: Any = None, *, level: str = "info") -> None:
        self.notifications.append((title, text))

    def is_snoozed(self) -> bool:
        return False

    def window_parent(self) -> Any:
        return None

    def call_soon(self, fn: Callable[[], None]) -> None:
        with self._lock:
            self._queue.append(fn)

    def drain(self) -> int:
        n = 0
        while True:
            with self._lock:
                if not self._queue:
                    return n
                fn = self._queue.pop(0)
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self.errors.append(type(e).__name__)
            n += 1

    def close(self) -> None:
        self.log.removeHandler(self.handler)


def wait_idle(ctx: FakeCtx, module: Any, timeout: float = 60.0, qapp: Any = None) -> None:
    """モジュールのスレッドが終わり、画面のスレッドへの返しを全部流すまで待つ。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        ctx.drain()
        if qapp is not None:
            qapp.processEvents()
        th = module._thread
        if module.state == "idle" and (th is None or not th.is_alive()):
            ctx.drain()
            if module.state == "idle" and module._thread is None:
                return
        time.sleep(0.01)
    raise AssertionError("timeout")


@pytest.fixture
def make_ctx(tmp_path: Path) -> Iterator[Callable[..., FakeCtx]]:
    made: list[FakeCtx] = []

    def factory(section: dict[str, Any] | None = None) -> FakeCtx:
        c = FakeCtx(tmp_path / "home" / "local" / "twinsweep", section)
        made.append(c)
        return c

    yield factory
    for c in made:
        c.close()


class FakeRecycleApi:
    """ファイルを本当には消さない偽のごみ箱。送ったものは exists が False になる。"""

    def __init__(self, removable: set[str] | None = None, abort_after: int | None = None) -> None:
        self.removable = {os.path.normcase(x) for x in (removable or set())}
        self.sent: list[str] = []
        self.calls = 0
        self.abort_after = abort_after

    def drive_type(self, path: Path) -> int:
        return 2 if any(os.path.normcase(str(path)).startswith(r) for r in self.removable) else 3

    def delete_to_recycle_bin(self, path: Path, parent_hwnd: int | None) -> tuple[int, bool]:
        self.calls += 1
        if self.abort_after is not None and len(self.sent) >= self.abort_after:
            return 0, True
        self.sent.append(os.path.normcase(str(path)))
        return 0, False

    def exists(self, path: Path) -> bool:
        return os.path.lexists(path) and os.path.normcase(str(path)) not in self.sent


# ------------------------------------------------------------------ 合成画像
def base_image(seed: int, size: tuple[int, int] = (800, 600)) -> Any:
    """なめらかな模様+少しの粒。seed が違えば dHash は大きく違う。"""
    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(seed)
    small = Image.fromarray(rng.integers(0, 256, (6, 8, 3), dtype=np.uint8), "RGB")
    img = small.resize(size, Image.Resampling.BICUBIC)
    a = np.asarray(img, dtype=np.int16)
    a = a + rng.integers(-12, 13, a.shape, dtype=np.int16)
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGB")


def save_jpeg(img: Any, path: Path, quality: int = 90, exif: Any = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    kw: dict[str, Any] = {"quality": quality}
    if exif is not None:
        kw["exif"] = exif
    img.save(path, "JPEG", **kw)
    assert path.stat().st_size >= 10 * 1024, "検体は 10KB 以上にする"
    return path


def brighter(img: Any, factor: float = 1.10) -> Any:
    from PIL import ImageEnhance

    return ImageEnhance.Brightness(img).enhance(factor)
