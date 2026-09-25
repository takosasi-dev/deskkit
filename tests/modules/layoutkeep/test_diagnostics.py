# diagnostics()(契約 §1): 件数・モード・真偽・理由コードだけを返し、タイトル・exe 名・パス・シグネチャを含まない。
from __future__ import annotations

from pathlib import Path

from deskkit.modules.layoutkeep.selftest import Scenario

from .fakes import make_module


def test_diagnostics_counts_and_codes_only(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = make_module(tmp_path, scenario.api, {"targets": [{"exe": t} for t in scenario.targets]})
    mod.start()
    mod.store.save_layout(scenario.layout)
    mod.store.save_layout(scenario.layout, "配信用")
    assert mod.snapshot_now("cli") == "saved"
    mod.apply_current("tray")
    ctx.snoozed = True
    d = mod.diagnostics()
    assert d["signatures"] == 1 and d["presets"] == 2 and d["current_presets"] == 2
    assert d["auto_snapshots"] == 1 and d["mode"] == "dry_run" and d["place_new_mode"] == "dry_run"
    assert d["last_apply"] == "dry_run" and d["last_snapshot"] == "saved" and d["last_place"] == "none"
    assert d["snoozed"] is True and d["auto_snapshot"] is True and d["place_new"] is False
    assert all(isinstance(v, (str, int, bool)) for v in d.values())
    blob = " ".join(str(v) for v in d.values()).lower()
    assert ".exe" not in blob and "\\" not in blob and scenario.layout.signature not in blob
    for w in scenario.api.windows:
        if len(w.title) >= 3:
            assert w.title.lower() not in blob


def test_diagnostics_when_disabled(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = make_module(tmp_path, scenario.api, {}, awareness="unaware")
    mod.start()
    d = mod.diagnostics()
    assert d["disabled"] is True and d["signatures"] == 0


def test_fake_ctx_contract_helpers(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = make_module(tmp_path, scenario.api, {})
    assert ctx.is_snoozed() is False and ctx.list_modes() == []
    assert mod.snoozed() is False
    ctx.snoozed = True
    assert mod.snoozed() is True
