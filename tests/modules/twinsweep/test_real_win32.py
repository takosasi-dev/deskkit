# 実機のテスト(pytest -m win32_real)。本物のごみ箱への送り(一時フォルダのファイルだけ)と、§8・AC-11 の 2,000 枚の実測。
# 実測の検体は一時フォルダに合成し、終わったら消す。
from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from deskkit.modules.twinsweep.job import ScanRequest, run_scan

pytestmark = pytest.mark.win32_real


def test_real_recycle_of_temp_file(tmp_path: Path) -> None:
    from deskkit.fileops import recycle

    p = tmp_path / "twinsweep-real-recycle.bin"
    p.write_bytes(b"x" * 20_000)
    r = recycle([p], None)
    assert r.sent_count == 1
    assert not p.exists()


def _perf_image(seed: int) -> Any:
    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(seed)
    small = Image.fromarray(rng.integers(0, 256, (9, 16, 3), dtype=np.uint8), "RGB")
    img = small.resize((1920, 1080), Image.Resampling.BICUBIC)
    a = np.asarray(img, dtype=np.int16) + rng.integers(-5, 6, (1080, 1920, 3), dtype=np.int16)
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGB")


def make_perf_set(root: Path, total: int = 2000, dup_ratio: float = 0.2) -> dict[str, str]:
    """(複製のパス → 元のパス)。元は total*(1-dup_ratio) 枚、複製は縮小・再圧縮・明るさ違いを順に。"""
    from PIL import ImageEnhance

    n_dup = int(total * dup_ratio)
    n_orig = total - n_dup
    root.mkdir(parents=True, exist_ok=True)
    dup_of: dict[str, str] = {}
    for i in range(n_orig):
        img = _perf_image(10_000 + i)
        src = root / f"p{i:05d}.jpg"
        img.save(src, "JPEG", quality=88)
        if i < n_dup:
            dst = root / f"d{i:05d}.jpg"
            kind = i % 3
            if kind == 0:
                img.resize((1280, 720)).save(dst, "JPEG", quality=88)
            elif kind == 1:
                img.save(dst, "JPEG", quality=60)
            else:
                ImageEnhance.Brightness(img).enhance(1.10).save(dst, "JPEG", quality=88)
            dup_of[os.path.normcase(str(dst))] = os.path.normcase(str(src))
    return dup_of


def measure(root: Path, cache: Path, dup_of: dict[str, str]) -> dict[str, Any]:
    req = ScanRequest(roots=[str(root)], recursive=True, level="normal", exact_only=False, excluded=[], cache_path=cache)
    t0 = time.perf_counter()
    first = run_scan(req, threading.Event(), lambda *_a: None)
    t1 = time.perf_counter()
    second = run_scan(req, threading.Event(), lambda *_a: None)
    t2 = time.perf_counter()
    assert first.model is not None and second.model is not None
    res: dict[str, Any] = {"files": first.files, "first_s": round(t1 - t0, 1), "second_s": round(t2 - t1, 1),
                           "first_stage_ms": first.stage_ms, "second_stage_ms": second.stage_ms, "levels": {}}
    for level in ("strict", "normal", "loose"):
        m = second.model.rebuild(level, False)
        gid: dict[str, set[int]] = {}
        for g in m.groups:
            for p in g.photos:
                gid.setdefault(os.path.normcase(p.path), set()).add(g.gid)
        found = sum(1 for d, o in dup_of.items() if gid.get(d, set()) & gid.get(o, set()))
        mixed = sum(1 for g in m.groups if len({os.path.basename(p.path)[1:6] for p in g.photos}) > 1)
        res["levels"][level] = {"groups": len(m.groups), "found": found, "dups": len(dup_of),
                                "rate": round(found / max(1, len(dup_of)) * 100, 1), "mixed_groups": mixed}
    return res


def test_ac11_2000_photos(tmp_path_factory: pytest.TempPathFactory) -> None:
    base = tmp_path_factory.mktemp("twinsweep-perf")
    try:
        root = base / "photos"
        t0 = time.perf_counter()
        dup_of = make_perf_set(root)
        gen = time.perf_counter() - t0
        res = measure(root, base / "cache.db", dup_of)
        res["generate_s"] = round(gen, 1)
        res["bytes"] = sum(p.stat().st_size for p in root.iterdir())
        print("\nTWINSWEEP_PERF", res)
        assert res["files"] == 2000
        assert res["first_s"] < 180
        assert res["second_s"] < 30
    finally:
        shutil.rmtree(base, ignore_errors=True)  # 自分で作った検体だけを消す
