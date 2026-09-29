# MojiFix のテスト共通部品: 偽 ctx・生のバイト列から組み立てる zip・ワーカーの完了待ち。
# 本番のデータ(%LOCALAPPDATA%\DeskKit)と利用者のファイルには触れない。検体はすべて一時フォルダの中で作る(Qt は offscreen)。
from __future__ import annotations

import copy
import io
import logging
import os
import struct
import time
import zlib
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
            if r.exc_text:
                out.append(r.exc_text)
        return "\n".join(out)


@pytest.fixture
def logs() -> Iterator[ListHandler]:
    h = ListHandler()
    lg = logging.getLogger("deskkit.mojifix")
    lg.addHandler(h)
    old = lg.level
    lg.setLevel(logging.DEBUG)
    yield h
    lg.removeHandler(h)
    lg.setLevel(old)


class FakeCtx:
    def __init__(self, data_dir: Path, section: dict[str, Any] | None = None) -> None:
        self.name = "mojifix"
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.log = logging.getLogger("deskkit.mojifix")
        self._section: dict[str, Any] = {"enabled": True, **(section or {})}
        self.tray: list[tuple[str, Callable[[], None]]] = []
        self.notifications: list[tuple[str, str]] = []
        self.status = ""
        self.writes: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.shown = 0
        self.pending: list[Callable[[], None]] = []
        self.parent: Any = None

    def settings(self) -> dict[str, Any]:
        return dict(self._section)

    def settings_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._section)

    def write_settings(self, section: dict[str, Any], *, restart: bool = False) -> None:
        self._section = {**copy.deepcopy(section), "enabled": True}
        self.writes.append(copy.deepcopy(section))

    def add_tray_action(self, label: str, cb: Callable[[], None], **_k: Any) -> None:
        self.tray.append((label, cb))

    def set_tray_status(self, text: str) -> None:
        self.status = text

    def notify(self, title: str, text: str, *_a: Any, **_k: Any) -> None:
        self.notifications.append((title, text))

    def safe(self, fn: Callable[..., Any], label: str | None = None) -> Callable[..., Any]:
        def w(*a: Any, **k: Any) -> Any:
            try:
                return fn(*a, **k)
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{label}: {type(e).__name__}: {e}")
                return None

        return w

    def call_soon(self, fn: Callable[[], None]) -> None:
        self.pending.append(fn)  # テストでは drain() で GUI スレッド役として実行する

    def drain(self) -> None:
        while self.pending:
            self.safe(self.pending.pop(0), "call_soon")()

    def show_page(self) -> None:
        self.shown += 1

    def window_parent(self) -> Any:
        return self.parent

    def is_snoozed(self) -> bool:
        return False


@pytest.fixture(scope="session")
def qapp() -> Any:
    from PySide6.QtWidgets import QApplication

    from deskkit.ui import theme

    app = QApplication.instance() or QApplication([])
    theme.apply(app)  # type: ignore[arg-type]
    return app


@pytest.fixture
def make_module(tmp_path: Path, qapp: Any) -> Iterator[Callable[..., tuple[Any, FakeCtx]]]:
    made: list[Any] = []

    def factory(section: dict[str, Any] | None = None, **kw: Any) -> tuple[Any, FakeCtx]:
        from deskkit.modules.mojifix.module import MojiFixModule

        ctx = FakeCtx(tmp_path / f"data{len(made)}", section)
        kw.setdefault("fallback_dir", lambda: tmp_path / "Documents" / "MojiFix")
        kw.setdefault("free_bytes", lambda _p: 1 << 50)
        kw.setdefault("env", {})
        m = MojiFixModule(ctx, **kw)
        m.start()
        made.append(m)
        return m, ctx

    yield factory
    for m in made:
        m.stop()


def settle(m: Any, ctx: FakeCtx, timeout: float = 60.0) -> None:
    """ワーカーが止まり、call_soon で積まれた画面側の処理が無くなるまで回す(続けて始まる処理も待つ)。"""
    t0 = time.monotonic()
    while True:
        m.worker.wait(timeout)
        time.sleep(0.02)
        ctx.drain()
        if not m.busy() and not ctx.pending:
            return
        if time.monotonic() - t0 > timeout:
            raise TimeoutError("worker did not finish")


# ------------------------------------------------------------------ 生の zip
def raw_zip(entries: list[tuple[bytes, bytes]], *, flag: int = 0, method: int = 0,
            extra: dict[bytes, bytes] | None = None, create_system: int = 0, external: dict[bytes, int] | None = None,
            sizes: dict[bytes, tuple[int, int]] | None = None, crc_override: dict[bytes, int] | None = None) -> bytes:
    """名前を生のバイト列のまま入れた zip を組む(Mac で作ったような、印の無い UTF-8 の名前も作れる)。
    method=8 なら deflate。sizes で宣言の (圧縮後, 展開後) を偽れる。"""
    extra = extra or {}
    external = external or {}
    sizes = sizes or {}
    crc_override = crc_override or {}
    buf = io.BytesIO()
    cd = io.BytesIO()
    for raw, data in entries:
        crc = crc_override.get(raw, zlib.crc32(data) & 0xFFFFFFFF)
        if method == 8:
            co = zlib.compressobj(9, zlib.DEFLATED, -15)
            body = co.compress(data) + co.flush()
        else:
            body = data
        csize, usize = sizes.get(raw, (len(body), len(data)))
        ex = extra.get(raw, b"")
        off = buf.tell()
        buf.write(struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, flag, method, 0, 0x21, crc, csize, usize, len(raw), len(ex)))
        buf.write(raw)
        buf.write(ex)
        buf.write(body)
        ver_made = (create_system << 8) | 20
        cd.write(struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, ver_made, 20, flag, method, 0, 0x21, crc, csize, usize,
                             len(raw), len(ex), 0, 0, 0, external.get(raw, 0), off))
        cd.write(raw)
        cd.write(ex)
    cdb = cd.getvalue()
    start = buf.tell()
    buf.write(cdb)
    buf.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(entries), len(entries), len(cdb), start, 0))
    return buf.getvalue()


def unicode_extra(raw: bytes, name: str, crc: int | None = None) -> bytes:
    uni = name.encode("utf-8")
    c = (zlib.crc32(raw) & 0xFFFFFFFF) if crc is None else crc
    return struct.pack("<HHBI", 0x7075, 1 + 4 + len(uni), 1, c) + uni


ADDRESS = "氏名,住所,電話番号\r\n山田太郎,東京都千代田区一丁目,03-1234-5678\r\n佐藤花子,大阪府大阪市北区,06-9876-5432\r\n"
