# KeyFree のテスト共通部品。偽の ctx.hotkeys と偽の配列(deskkit.modules.keyfree.fakes)で動かし、DESKKIT_HOME を一時フォルダへ向ける。
# どのテストも最後に、ログに組み合わせの名前(Ctrl+ など)が出ていないことを確かめる(AC-9・INV-4)。
from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FORBIDDEN = ("Ctrl+", "Alt+", "Shift+", "Win+")


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DESKKIT_HOME", str(tmp_path / "home"))


@pytest.fixture(scope="session")
def qapp() -> Any:
    from PySide6.QtWidgets import QApplication

    from deskkit.ui import theme

    app = QApplication.instance() or QApplication([])
    theme.apply(app)  # type: ignore[arg-type]
    return app


def assert_no_names(text: str) -> None:
    for s in FORBIDDEN:
        assert s not in text, f"ログ・診断に組み合わせの名前が出ています: {s}"


@pytest.fixture
def make_module(tmp_path: Path, qapp: Any) -> Iterator[Callable[..., Any]]:
    """偽 ctx・偽の配列の KeyFreeModule を作る。テストの最後にログを調べる(AC-9)。"""
    from deskkit.modules.keyfree.fakes import FakeCtx, FakeKeyboard, full_symbols
    from deskkit.modules.keyfree.module import KeyFreeModule

    made: list[tuple[Any, Any]] = []

    def factory(section: dict[str, Any] | None = None, chars: dict[int, str] | None = None, hotkeys: Any = None,
                start: bool = True, **kw: Any) -> tuple[Any, Any]:
        ctx = FakeCtx(tmp_path / f"data{len(made)}", section, hotkeys)
        m = KeyFreeModule(ctx, kb=FakeKeyboard(full_symbols() if chars is None else chars), **kw)
        if start:
            m.start()
        made.append((m, ctx))
        return m, ctx

    yield factory
    for m, ctx in made:
        try:
            m.stop()
        except Exception:  # noqa: BLE001
            pass
        assert_no_names(ctx.handler.text())
        assert_no_names(repr(m.diagnostics()))
