# LayoutKeep テストの共通フィクスチャ。データは tmp_path に置き、実機のウィンドウと %LOCALAPPDATA% に触れない。
from __future__ import annotations

from pathlib import Path

import pytest

from deskkit.modules.layoutkeep.selftest import Scenario, build_scenario


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DESKKIT_HOME", str(tmp_path / "home"))


@pytest.fixture
def scenario() -> Scenario:
    return build_scenario()
