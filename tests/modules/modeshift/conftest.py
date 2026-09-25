# ModeShift テスト共通: 偽の OS 実装と偽の ctx で service を組み立てる(実機・%LOCALAPPDATA% には触らない)。
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from deskkit.modules.modeshift.config import validate_section
from deskkit.modules.modeshift.fakes import FakeCtx, FakeSystem, FakeUi, fake_system, populate, sample_section
from deskkit.modules.modeshift.service import ModeShiftService


class Env:
    def __init__(self, tmp: Path, *, confirm: bool, threaded: bool = False,
                 edit: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.tmp = tmp
        self.sys: FakeSystem = fake_system()
        populate(self.sys)
        sec = sample_section(tmp)
        if edit is not None:
            edit(sec)
        if confirm:
            cfg = validate_section(sec, {"game.exe"})
            for m, md in zip(sec["modes"], cfg.modes, strict=True):
                m["confirmed_hash"] = md.def_hash
        self.ctx = FakeCtx(tmp / "data", sec, games=frozenset({"game.exe"}))
        self.ui = FakeUi()
        self.svc = ModeShiftService(self.ctx, self.sys.backends(), self.ui, threaded=threaded)

    @property
    def ops_path(self) -> Path:
        return self.tmp / "data" / "ops.jsonl"

    def ops(self) -> list[dict[str, Any]]:
        if not self.ops_path.exists():
            return []
        return [json.loads(x) for x in self.ops_path.read_text(encoding="utf-8").splitlines() if x.strip()]


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path, confirm=True)


@pytest.fixture
def env_unconfirmed(tmp_path: Path) -> Env:
    return Env(tmp_path, confirm=False)
