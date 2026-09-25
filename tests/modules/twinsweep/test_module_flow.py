# モジュールの流れ: スキャン(スレッド+call_soon)・キャッシュの効き(AC-8)・中止(FR-6)・並べ直し(FR-16)・
# ごみ箱送り(偽のごみ箱。AC-3・AC-5・AC-6・FR-14・FR-15)・INV-4(AC-9)・INV-5(AC-10)・usage・diagnostics。
from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from deskkit.modules.twinsweep import job
from deskkit.modules.twinsweep.hashing import compute_features
from deskkit.modules.twinsweep.module import MSG_FORBIDDEN, TwinSweepModule, merge_defaults
from deskkit.modules.twinsweep.results import KIND_EXACT
from tests.modules.twinsweep.conftest import FakeCtx, FakeRecycleApi, base_image, brighter, save_jpeg, wait_idle

MARK = "ZqSecretName"  # 検体のフォルダ名・ファイル名に入れる目印(ログ・記録に出てはいけない)


class Counter:
    def __init__(self) -> None:
        self.n = 0
        self.lock = threading.Lock()

    def __call__(self, path: str, size: int, mtime_ns: int) -> Any:
        with self.lock:
            self.n += 1
        return compute_features(path, size, mtime_ns)


def make_set(root: Path) -> dict[str, Path]:
    img = base_image(11, (1200, 900))
    d = root / f"{MARK}_album"
    out = {
        "orig": save_jpeg(img, d / f"{MARK}_orig.jpg", 92),
        "small": save_jpeg(img.resize((600, 450)), d / f"{MARK}_small.jpg", 90),
        "bright": save_jpeg(brighter(img), d / "sub" / f"{MARK}_bright.jpg", 90),
        "other": save_jpeg(base_image(12, (1200, 900)), d / f"{MARK}_other.jpg", 90),
    }
    copy = d / f"{MARK}_copy.jpg"
    copy.write_bytes(out["orig"].read_bytes())
    out["copy"] = copy
    bad = d / f"{MARK}_broken.jpg"
    bad.write_bytes(os.urandom(15_000))
    out["bad"] = bad
    return out


def make_module(ctx: FakeCtx, tmp_path: Path, **kw: Any) -> TwinSweepModule:
    env = {"APPDATA": str(tmp_path / "fake_appdata"), "LOCALAPPDATA": str(tmp_path / "fake_local")}
    m = TwinSweepModule(ctx, env=env, open_recycle_bin=lambda: None, **kw)
    m.start()
    return m


def scan(ctx: FakeCtx, m: TwinSweepModule) -> None:
    assert m.start_scan() is None
    wait_idle(ctx, m)


def test_settings_defaults_and_repair() -> None:
    merged, changed = merge_defaults({})
    assert changed and merged == {"folders": [], "recursive": True, "level": "normal", "exact_only": False}
    fixed, changed = merge_defaults({"folders": "x", "recursive": 1, "level": "weird", "exact_only": None})
    assert changed and fixed["folders"] == [] and fixed["level"] == "normal" and fixed["recursive"] is True
    many, _ = merge_defaults({"folders": [f"C:\\{i}" for i in range(12)]})
    assert len(many["folders"]) == 10


def test_full_flow(make_ctx: Callable[..., FakeCtx], tmp_path: Path) -> None:
    photos = tmp_path / "pics"
    files = make_set(photos)
    ctx = make_ctx({"folders": [str(photos)]})
    counter = Counter()
    api = FakeRecycleApi()
    m = make_module(ctx, tmp_path, recycle_api=api, compute=counter)
    home = tmp_path / "home"
    before_outside = sorted(str(p) for p in tmp_path.rglob("*") if not str(p).startswith(str(home)))

    scan(ctx, m)
    assert ctx.errors == []
    assert m.model is not None and m.last is not None
    assert m.last.files == 6 and m.last.unreadable == 1
    assert counter.n == 6
    texts = [n.text for n in m.scan_notices]
    assert "読めなかった画像: 1 枚" in texts
    # AC-3: バイト単位で同じ2つは「まったく同じ写真」
    exact = m.model.exact_groups()
    assert len(exact) == 1
    assert {Path(p.path).name for p in exact[0].photos} == {files["orig"].name, files["copy"].name}
    # AC-1: 縮小・明るさ違いは似ているグループ、無関係は入らない
    sim = m.model.similar_groups()
    assert len(sim) == 1
    names = {Path(p.path).name for p in sim[0].photos}
    assert files["small"].name in names and files["bright"].name in names and files["other"].name not in names
    # AC-4 / FR-10: いちばん画素数の多いものが「残す」
    assert all(m.model.is_keep(g.recommended) for g in m.model.groups)

    # AC-10: スキャンで DeskKit のデータフォルダの外にファイルが増えない
    after_outside = sorted(str(p) for p in tmp_path.rglob("*") if not str(p).startswith(str(home)))
    assert before_outside == after_outside

    # AC-8: 2回目はキャッシュが効いて開かない
    counter.n = 0
    scan(ctx, m)
    assert counter.n == 0
    assert m.last.opened == 0 and m.last.unreadable == 1

    # AC-6: スキャンのあとに書き換えたファイルは送らない
    assert m.model is not None
    sel = {Path(p.path).name for p in m.model.selected()}
    assert files["small"].name in sel
    time.sleep(0.02)
    with open(files["small"], "ab") as f:
        f.write(b"\0")
    assert m.recycle_selected(None) is None
    wait_idle(ctx, m)
    sent_names = {Path(p).name for p in api.sent}  # api.sent は normcase 済み(小文字)
    assert files["small"].name.lower() not in sent_names
    assert any("スキャンのあとに変わった写真: 1 枚" in n.text for n in m.recycle_notices)
    assert files["copy"].name.lower() in sent_names or files["orig"].name.lower() in sent_names
    assert sent_names == {n.lower() for n in sel} - {files["small"].name.lower()}
    # 送ったものはグループから外れ、1枚になったグループは消える(FR-15)
    assert all(len(g.photos) >= 2 for g in m.model.groups)
    assert not any(os.path.normcase(p.path) in api.sent for g in m.model.groups for p in g.photos)
    assert any("元に戻すには" in n.text for n in m.recycle_notices)
    # 本物のファイルは偽のごみ箱なので残っている
    assert all(p.exists() for p in files.values())

    # AC-9: ログ・ops.jsonl・diagnostics に名前が出ない
    ops_text = (ctx.data_dir / "ops.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(x) for x in ops_text.splitlines()]
    assert [r["op"] for r in rows] == ["scan", "scan", "recycle"]
    assert set(rows[-1]) == {"ts", "op", "files", "groups", "sent", "skipped", "bytes", "ms"}
    blob = ops_text + "\n".join(ctx.handler.lines) + json.dumps(m.diagnostics(), ensure_ascii=False)
    for s in (MARK, "album", "pics", str(tmp_path)):
        assert s not in blob
    diag = m.diagnostics()
    assert diag["cache_rows"] == 6 and diag["last_scan_files"] == 6 and diag["level"] == "normal"
    u = m.usage(7)
    assert u[0].primary and u[0].per_day[-1] == rows[-1]["sent"]
    assert u[1].per_day[-1] == 2
    m.stop()


def test_ac5_broken_invariant_blocks_recycle(make_ctx: Callable[..., FakeCtx], tmp_path: Path) -> None:
    photos = tmp_path / "pics"
    make_set(photos)
    ctx = make_ctx({"folders": [str(photos)]})
    api = FakeRecycleApi()
    m = make_module(ctx, tmp_path, recycle_api=api)
    scan(ctx, m)
    assert m.model is not None
    g = m.model.groups[0]
    # 画面の操作では最後の「残す」は外せない
    assert m.set_keep(g.recommended, False) is False
    # テストで直接壊すと、recycle は呼ばれない
    for p in g.photos:
        m.model.keep[p.pid] = False
    err = m.recycle_selected(None)
    assert err is not None and "残す" in err
    wait_idle(ctx, m)
    assert api.calls == 0
    m.stop()


def test_fr14_no_recycle_bin_and_abort(make_ctx: Callable[..., FakeCtx], tmp_path: Path) -> None:
    photos = tmp_path / "pics"
    make_set(photos)
    ctx = make_ctx({"folders": [str(photos)]})
    api = FakeRecycleApi(removable={str(photos)})
    m = make_module(ctx, tmp_path, recycle_api=api)
    scan(ctx, m)
    assert m.model is not None
    n = len(m.model.selected())
    m.recycle_selected(None)
    wait_idle(ctx, m)
    assert api.calls == 0
    assert any(f"このドライブにはごみ箱が無いため送りませんでした: {n} 枚" in x.text for x in m.recycle_notices)
    assert len(m.model.selected()) == n  # 何も外れていない
    # Windows の恒久削除の確認で「いいえ」→ aborted → 送りませんでした
    m.recycle_api = FakeRecycleApi(abort_after=0)
    m.recycle_selected(None)
    wait_idle(ctx, m)
    assert any("送りませんでした" in x.text and "取り消しました" in x.text for x in m.recycle_notices)
    m.stop()


def test_fr16_regroup_without_recompute(make_ctx: Callable[..., FakeCtx], tmp_path: Path) -> None:
    photos = tmp_path / "pics"
    make_set(photos)
    ctx = make_ctx({"folders": [str(photos)]})
    counter = Counter()
    m = make_module(ctx, tmp_path, compute=counter)
    scan(ctx, m)
    opened = counter.n
    assert m.set_exact_only(True) is None
    wait_idle(ctx, m)
    assert m.model is not None and all(g.kind == KIND_EXACT for g in m.model.groups)
    assert m.set_exact_only(False) is None
    wait_idle(ctx, m)
    assert m.set_level("strict") is None
    wait_idle(ctx, m)
    assert m.model is not None and m.model.level == "strict"
    assert counter.n == opened
    assert ctx.writes[-1]["level"] == "strict"
    m.stop()


def test_fr6_cancel_keeps_features(make_ctx: Callable[..., FakeCtx], tmp_path: Path) -> None:
    photos = tmp_path / "pics"
    for i in range(60):
        save_jpeg(base_image(100 + i, (400, 300)), photos / f"{i}.jpg")
    ctx = make_ctx({"folders": [str(photos)]})
    gate = threading.Event()
    started = Counter()

    def slow(path: str, size: int, mtime_ns: int) -> Any:
        started(path, size, mtime_ns)
        if started.n >= 3:
            gate.wait(5)
        return compute_features(path, size, mtime_ns)

    m = make_module(ctx, tmp_path, compute=slow)
    assert m.start_scan() is None
    end = time.monotonic() + 10
    while started.n < 3 and time.monotonic() < end:
        time.sleep(0.01)
    m.cancel_scan()
    gate.set()
    wait_idle(ctx, m)
    assert m.last is not None and m.last.status == "cancelled"
    assert m.model is None
    assert any("中止しました" in n.text for n in m.scan_notices)
    kept = m.cache_rows()
    assert 1 <= kept < 60  # 調べた分はキャッシュに残る
    m.stop()


def test_forbidden_and_empty_and_too_many(make_ctx: Callable[..., FakeCtx], tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_ctx({})
    m = make_module(ctx, tmp_path)
    appdata = tmp_path / "fake_appdata"
    appdata.mkdir()
    assert m.check_folder(str(appdata)) == "forbidden"
    assert m.add_folder(str(appdata)) == MSG_FORBIDDEN
    assert m.check_folder(str(ctx.data_dir)) == "forbidden"  # DeskKit のデータフォルダ
    assert m.start_scan() is not None  # フォルダが無い
    empty = tmp_path / "empty"
    empty.mkdir()
    assert m.add_folder(str(empty)) is None
    assert m.check_folder(str(empty)) == "duplicate"
    scan(ctx, m)
    assert m.model is not None and m.model.groups == [] and m.last is not None and m.last.files == 0
    for i in range(3):
        save_jpeg(base_image(i, (400, 300)), empty / f"{i}.jpg")
    monkeypatch.setattr(job, "MAX_FILES", 2)
    real = job.ScanRequest

    def small_req(*a: Any, **k: Any) -> Any:
        r = real(*a, **k)
        r.max_files = 2
        return r

    monkeypatch.setattr("deskkit.modules.twinsweep.module.ScanRequest", small_req)
    scan(ctx, m)
    assert any("写真が多すぎます" in n.text for n in m.scan_notices)
    m.stop()


def test_stop_during_scan_returns_quickly(make_ctx: Callable[..., FakeCtx], tmp_path: Path) -> None:
    photos = tmp_path / "pics"
    for i in range(8):
        save_jpeg(base_image(200 + i, (400, 300)), photos / f"{i}.jpg")
    ctx = make_ctx({"folders": [str(photos)]})

    def slow(path: str, size: int, mtime_ns: int) -> Any:
        time.sleep(0.2)
        return compute_features(path, size, mtime_ns)

    m = make_module(ctx, tmp_path, compute=slow)
    m.start_scan()
    time.sleep(0.1)
    t0 = time.monotonic()
    m.stop()
    assert time.monotonic() - t0 < 5.0
    assert m.cache_rows() >= 0
