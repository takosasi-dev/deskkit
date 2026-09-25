# DropSort テスト共通: 本番のデータ置き場を汚さないよう DESKKIT_HOME を一時フォルダへ向け、Qt は offscreen で動かす。
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DESKKIT_HOME", str(tmp_path / "deskkit-home"))


@pytest.fixture(scope="session")
def qapp() -> Iterator[object]:
    from PySide6.QtWidgets import QApplication

    from deskkit.ui import theme

    app = QApplication.instance() or QApplication([])
    theme.apply(app)  # type: ignore[arg-type]
    yield app
