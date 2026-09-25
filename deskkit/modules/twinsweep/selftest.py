# TwinSweep の自己検査(AC-12)。一時フォルダに合成した写真で、特徴・グループ分け(G-1〜G-4)・キャッシュ(AC-8)・
# G-5/INV-2 を通し、偽のごみ箱で送る流れと、操作記録にファイル名が出ないこと(INV-4)を確かめる。
# 実機のごみ箱・%LOCALAPPDATA%\DeskKit には触れない。検体は終わったら消す。run() は 0=合格 / 1=不合格。
from __future__ import annotations

import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

MARK = "TsSelftestName"


class _Result:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, name: str, ok: bool) -> None:
        print(f"  [{'OK' if ok else 'NG'}] {name}")
        if not ok:
            self.failed += 1


class _FakeRecycle:
    """本当には消さない偽のごみ箱。"""

    def __init__(self) -> None:
        self.sent: set[str] = set()

    def drive_type(self, path: Path) -> int:
        return 3

    def delete_to_recycle_bin(self, path: Path, parent_hwnd: int | None) -> tuple[int, bool]:
        self.sent.add(os.path.normcase(str(path)))
        return 0, False

    def exists(self, path: Path) -> bool:
        return os.path.lexists(path) and os.path.normcase(str(path)) not in self.sent


def _image(seed: int, size: tuple[int, int]) -> Any:
    from deskkit.modules.twinsweep.hashing import numpy, pil_image

    np = numpy()
    pil = pil_image()
    rng = np.random.default_rng(seed)
    small = pil.fromarray(rng.integers(0, 256, (6, 8, 3), dtype=np.uint8), "RGB")
    img = small.resize(size, pil.Resampling.BICUBIC)
    a = np.asarray(img, dtype=np.int16) + rng.integers(-12, 13, (size[1], size[0], 3), dtype=np.int16)
    return pil.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGB")


def _make(root: Path) -> dict[str, Path]:
    d = root / f"{MARK}_album"
    d.mkdir(parents=True)
    img = _image(1, (1200, 900))
    files = {k: d / f"{MARK}_{k}.jpg" for k in ("orig", "small", "recomp", "bright", "other", "copy")}
    img.save(files["orig"], "JPEG", quality=92)
    img.resize((600, 450)).save(files["small"], "JPEG", quality=90)
    img.save(files["recomp"], "JPEG", quality=55)
    from PIL import ImageEnhance
    ImageEnhance.Brightness(img).enhance(1.10).save(files["bright"], "JPEG", quality=90)
    _image(2, (1200, 900)).save(files["other"], "JPEG", quality=90)
    files["copy"].write_bytes(files["orig"].read_bytes())
    return files


def run() -> int:
    print("TwinSweep 自己検査")
    r = _Result()
    tmp = Path(tempfile.mkdtemp(prefix="twinsweep-selftest-"))
    try:
        _checks(r, tmp)
    except Exception as e:  # noqa: BLE001 - 自己検査の失敗として数える
        r.check(f"例外なく終わる({type(e).__name__})", False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)  # VAC-4 例外: 自己検査の一時フォルダの検体
    print("合格" if r.failed == 0 else f"不合格({r.failed} 件)")
    return 0 if r.failed == 0 else 1


def _checks(r: _Result, tmp: Path) -> None:
    from deskkit.fileops import recycle
    from deskkit.modules.twinsweep.hashing import compute_features
    from deskkit.modules.twinsweep.job import ScanRequest, run_scan
    from deskkit.modules.twinsweep.oplog import OpsLog
    from deskkit.modules.twinsweep.scanner import check_root, excluded_dirs

    files = _make(tmp / "photos")
    opened: list[int] = []

    def counting(path: str, size: int, mtime_ns: int) -> Any:
        opened.append(1)
        return compute_features(path, size, mtime_ns)

    req = ScanRequest(roots=[str(tmp / "photos")], recursive=True, level="normal", exact_only=False, excluded=[],
                      cache_path=tmp / "data" / "cache.db")
    out = run_scan(req, threading.Event(), lambda *_a: None, compute=counting)
    m = out.model
    r.check("スキャンが終わる", out.status == "done" and m is not None)
    if m is None:
        return
    r.check("6 枚を数える", out.files == 6)
    ex = m.exact_groups()
    r.check("バイト単位で同じ2枚が「まったく同じ写真」(AC-3)",
            len(ex) == 1 and {Path(p.path).name for p in ex[0].photos} == {files["orig"].name, files["copy"].name})
    sim = m.similar_groups()
    names = {Path(p.path).name for g in sim for p in g.photos}
    r.check("縮小・再圧縮・明るさ違いが同じグループ、無関係は入らない(AC-1)",
            len(sim) == 1 and {files["small"].name, files["recomp"].name, files["bright"].name} <= names
            and files["other"].name not in names)
    r.check("各グループに「残す」がある(FR-10)", m.invariant_ok())
    last_keep = next(pid for pid in (g.recommended for g in m.groups) if not m.can_trash(pid))
    r.check("最後の「残す」は「ごみ箱へ」にできない(G-5)", m.set_keep(last_keep, False) is False)

    opened.clear()
    out2 = run_scan(req, threading.Event(), lambda *_a: None, compute=counting)
    r.check("2 回目は画像を開かない(AC-8)", not opened and out2.opened == 0)

    (tmp / "appdata").mkdir()
    r.check("調べないフォルダ(FR-3)", check_root(tmp / "appdata", excluded_dirs({"APPDATA": str(tmp / "appdata")})) == "forbidden")

    m2 = out2.model
    if m2 is None:
        r.check("2 回目の結果がある", False)
        return
    saved = dict(m2.keep)
    for p in m2.groups[0].photos:
        m2.keep[p.pid] = False
    r.check("送る直前の確認が「残す」の無いグループを見つける(INV-2)", not m2.invariant_ok())
    m2.keep = saved
    api = _FakeRecycle()
    chosen = [Path(p.path) for p in m2.selected()]
    res = recycle(chosen, None, api=api)
    r.check("偽のごみ箱へ送れる(本物のファイルはそのまま)", res.sent_count == len(chosen) and all(p.exists() for p in chosen))
    m2.remove_photos([p.pid for p in m2.selected()])
    r.check("送ったあと、送った写真が外れ、1枚になったグループが消える(FR-15)",
            not m2.selected() and all(len(g.photos) >= 2 for g in m2.groups) and len(m2.exact_groups()) == 0)

    ops = OpsLog(tmp / "data" / "ops.jsonl")
    ops.write("scan", files=out.files, groups=len(m.groups), ms=out.elapsed_ms)
    ops.write("recycle", files=len(chosen), sent=res.sent_count, bytes=1)
    text = (tmp / "data" / "ops.jsonl").read_text(encoding="utf-8")
    r.check("操作記録にファイル名・フォルダ名が無い(INV-4)", MARK not in text and str(tmp) not in text)
