# L1 名前付きプリセット: v0.1 形式(1構成1レイアウト)の読み込みと移行(控えを残す)・複数プリセットの保存・既定の切替・
# 名前の変更・削除(.deleted として残す)・プリセット指定の適用・layout.apply の preset(契約 §2)・クイックアクション。
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from deskkit.modules.layoutkeep.model import MIGRATED_PRESET_NAME, Layout
from deskkit.modules.layoutkeep.selftest import Scenario

from .fakes import FakeCtx, make_module


def _started(tmp_path: Path, sc: Scenario, **kw: Any) -> tuple[FakeCtx, Any]:
    ctx, mod = make_module(tmp_path, sc.api, {"targets": [{"exe": t} for t in sc.targets], **kw})
    mod.start()
    return ctx, mod


def _write_v1(mod: Any, layout: Layout) -> Path:
    p: Path = mod.store.layout_path(layout.signature)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(layout.to_json(), ensure_ascii=False), encoding="utf-8")
    return p


def _replies(ctx: FakeCtx) -> list[dict[str, Any]]:
    return [p for e, p in ctx.emitted if e == "layout.applied"]


def _apply_event(ctx: FakeCtx, **payload: Any) -> dict[str, Any]:
    for h in ctx.handlers["layout.apply"]:
        h(payload)
    return _replies(ctx)[-1]


def test_v1_file_is_read_as_one_default_preset_without_rewriting(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario)
    p = _write_v1(mod, scenario.layout)
    before = p.read_bytes()
    ps = mod.store.load_set(scenario.layout.signature)
    assert ps is not None and ps.from_v1 and [x.name for x in ps.presets] == [MIGRATED_PRESET_NAME]
    lay = mod.store.load_layout(scenario.layout.signature)
    assert lay is not None and len(lay.windows) == len(scenario.layout.windows)
    # 読むだけ・計画するだけでは書き換えない
    assert mod.apply_current("tray") == "dry_run"
    assert p.read_bytes() == before
    assert not list(p.parent.glob("*.v1-backup-*"))


def test_first_write_migrates_with_backup_and_keeps_v01_readable_copy(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario)
    sig = scenario.layout.signature
    p = _write_v1(mod, scenario.layout)
    original = p.read_text(encoding="utf-8")
    assert mod.save("gui", "配信用")
    backups = list(p.parent.glob(f"{sig}.json.v1-backup-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == original
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["schema"] == 2 and [x["name"] for x in data["presets"]] == [MIGRATED_PRESET_NAME, "配信用"]
    # 既定は移行したプリセットのまま。v0.1 が読めるよう最上位に既定の写しを置く
    default = next(x for x in data["presets"] if x["id"] == data["default"])
    assert default["name"] == MIGRATED_PRESET_NAME and data["windows"] == default["windows"]
    assert Layout.from_json(data).signature == sig  # v0.1 の読み込み関数でも読める
    # 2回目以降は控えを増やさない
    assert mod.save("gui", "配信用")
    assert len(list(p.parent.glob(f"{sig}.json.v1-backup-*"))) == 1


def test_multiple_presets_default_rename_delete(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario)
    assert mod.save("tray")  # 既定(「基本」を作る)
    sig = mod.last_sig.signature
    assert mod.save("gui", "作業用") and mod.save("gui", "配信用")
    assert mod.preset_names(sig) == [MIGRATED_PRESET_NAME, "作業用", "配信用"]
    ps = mod.store.load_set(sig)
    ids = {x.name: x.id for x in ps.presets}
    # 既定の切替 → 自動復元(preset=None)が使うのは既定
    assert mod.set_default_preset(sig, ids["配信用"]) is None
    assert mod.preset_names(sig)[0] == "配信用"
    assert mod.store.load_set(sig).default().name == "配信用"
    # 名前の変更(重複・予約名は不可)
    assert mod.rename_preset(sig, ids["作業用"], "配信用") == "同じ名前のプリセットが既にあります"
    assert "自動" in (mod.rename_preset(sig, ids["作業用"], "自動") or "")
    assert mod.rename_preset(sig, ids["作業用"], "集中") is None
    assert "集中" in mod.preset_names(sig)
    # 既定を削除 → 残りの先頭が既定になり、外したものは .deleted として残る
    assert mod.delete_preset(sig, ids["配信用"]) is None
    assert mod.preset_names(sig) == [MIGRATED_PRESET_NAME, "集中"]
    assert list(mod.store.layouts_dir.glob(f"{sig}.{ids['配信用']}.json.deleted-*"))
    assert mod.store.prev_path(sig).exists()
    # 最後の1つまで消すと構成ごと改名(恒久削除しない)
    for n in list(mod.preset_names(sig)):
        assert mod.delete_preset(sig, mod.store.load_set(sig).by_name(n).id) is None
    assert not mod.store.has_layout(sig)
    assert list(mod.store.layouts_dir.glob(f"{sig}.json.deleted-*"))


def test_reserved_and_empty_names_are_rejected(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario)
    assert not mod.save("gui", "自動")
    assert not mod.save("gui", "抜く前")
    assert not mod.save("gui", "   ")
    assert not mod.store.has_layout(mod.last_sig.signature)


def test_apply_named_preset_and_unknown(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario, mode="live")
    mod.store.save_layout(scenario.layout, "作業用")
    assert mod.apply_current("gui", preset="作業用") == "applied"
    assert scenario.api.set_calls
    n = len(scenario.api.set_calls)
    assert mod.apply_current("gui", preset="存在しない") == "no_layout"
    assert len(scenario.api.set_calls) == n
    assert "存在しない" in ctx.notes[-1]["text"]
    assert '"preset":"作業用"' in mod.store.oplog_path.read_text(encoding="utf-8")


def test_layout_apply_event_with_preset(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario, names={"aaaaaaaaaaaa": "別構成"})
    mod.store.save_layout(scenario.layout, "作業用")
    r = _apply_event(ctx, layout=None, request_id="p1", preset="作業用")
    assert (r["result"], r["preset"], r["request_id"]) == ("dry_run", "作業用", "p1")
    # 現在の構成に無いプリセット名 → no_layout(preset はそのまま返す)
    r = _apply_event(ctx, layout=None, request_id="p2", preset="配信用")
    assert (r["result"], r["preset"]) == ("no_layout", "配信用")
    # 構成が違う名前 + preset → sig_mismatch(INV-3)
    r = _apply_event(ctx, layout="別構成", request_id="p3", preset="作業用")
    assert (r["result"], r["preset"]) == ("sig_mismatch", "作業用")
    # preset が無い要求は従来どおり(preset キーを返さない)
    r = _apply_event(ctx, layout=None, request_id="p4")
    assert r["result"] == "dry_run" and "preset" not in r
    # 文字列でない preset は no_layout(受けた値をそのまま返す)
    r = _apply_event(ctx, layout=None, request_id="p5", preset=5)
    assert (r["result"], r["preset"]) == ("no_layout", 5)
    assert scenario.api.set_calls == []


def test_layout_apply_event_live_with_preset_and_wait(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario, mode="live", max_wait_s=5)
    mod.store.save_layout(scenario.layout, "作業用")
    for h in ctx.handlers["layout.apply"]:
        h({"layout": None, "request_id": "w1", "preset": "作業用", "wait_s": 3})
    assert _replies(ctx) == []  # mail.exe(未起動)を待っている
    busy = _apply_event(ctx, layout=None, request_id="w2", preset="作業用")
    assert (busy["result"], busy["preset"]) == ("error", "作業用")
    mod._wait.deadline = 0  # 待ち時間切れ
    ctx.timers[-1].fire()
    r = _replies(ctx)[-1]
    assert (r["request_id"], r["result"], r["preset"]) == ("w1", "applied", "作業用")


def test_quick_actions_per_preset(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario)
    mod.store.save_layout(scenario.layout)
    mod.store.save_layout(scenario.layout, "配信用")
    mod.refresh_status()
    labels = [lb for lb, _ in ctx.quick]
    name = mod.display_name(scenario.layout.signature)
    want = f"{name}: プリセット「配信用」を適用(試運転)"
    assert want in labels and f"{name}: プリセット「{MIGRATED_PRESET_NAME}」を適用(試運転)" in labels
    dict(ctx.quick)[want]()
    assert scenario.api.set_calls == []
    assert '"preset":"配信用"' in mod.store.oplog_path.read_text(encoding="utf-8")


def test_entry_regex_edit_targets_the_chosen_preset(tmp_path: Path, scenario: Scenario) -> None:
    ctx, mod = _started(tmp_path, scenario)
    sig = scenario.layout.signature
    mod.store.save_layout(scenario.layout)
    mod.store.save_layout(scenario.layout, "配信用")
    assert mod.set_entry_regex(sig, 5, "^page A", "配信用") is None
    assert mod.store.load_layout(sig, "配信用").windows[5].title_regex == "^page A"
    assert mod.store.load_layout(sig).windows[5].title_regex is None
