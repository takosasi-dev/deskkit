# 特徴の計算(AC-1・§10 の回転と撮影日時)・列挙(FR-3〜FR-5・AC-7・§10)・キャッシュ(G-7・§10)のテスト。
from __future__ import annotations

import ctypes
import os
import sqlite3
import threading
from datetime import date, timedelta
from pathlib import Path

import pytest

from deskkit.modules.twinsweep.cache import FeatureCache, clear_cache, row_count
from deskkit.modules.twinsweep.grouping import LEVELS, Photo, similar_groups
from deskkit.modules.twinsweep.hashing import FileChangedError, compute_features
from deskkit.modules.twinsweep.scanner import (
    FILE_ATTRIBUTE_HIDDEN,
    FILE_ATTRIBUTE_OFFLINE,
    check_root,
    enumerate_images,
    excluded_dirs,
)
from tests.modules.twinsweep.conftest import base_image, brighter, save_jpeg


def feat(p: Path) -> tuple[Path, object]:
    st = p.stat()
    return p, compute_features(str(p), st.st_size, st.st_mtime_ns)


def as_photo(i: int, p: Path) -> Photo:
    st = p.stat()
    f = compute_features(str(p), st.st_size, st.st_mtime_ns)
    assert f.dhash is not None
    return Photo(i, str(p), str(p.parent), st.st_size, st.st_mtime_ns, f.sha256, f.dhash, f.width, f.height, f.taken_at,
                 f.sharpness)


def test_ac1_variants_group_together_and_unrelated_stays_out(tmp_path: Path) -> None:
    img = base_image(1, (1600, 1200))
    orig = save_jpeg(img, tmp_path / "orig.jpg", 92)
    small = save_jpeg(img.resize((800, 600)), tmp_path / "small.jpg", 90)
    recomp = save_jpeg(img, tmp_path / "recomp.jpg", 55)
    bright = save_jpeg(brighter(img), tmp_path / "bright.jpg", 90)
    other = save_jpeg(base_image(2, (1600, 1200)), tmp_path / "other.jpg", 90)
    photos = [as_photo(i, p) for i, p in enumerate([orig, small, recomp, bright, other])]
    groups = similar_groups(photos, LEVELS["normal"])
    assert len(groups) == 1
    assert {p.pid for p in groups[0]} == {0, 1, 2, 3}


def test_features_size_orientation_and_exif_date(tmp_path: Path) -> None:
    from PIL import Image

    img = base_image(3, (800, 600))
    exif = Image.Exif()
    exif[0x0112] = 6  # 90 度回転
    exif.get_ifd(0x8769)[0x9003] = "2023:07:08 09:10:11"
    p = save_jpeg(img, tmp_path / "rot.jpg", 90, exif=exif.tobytes())
    _, f = feat(p)
    assert f.width == 600 and f.height == 800  # type: ignore[attr-defined]
    assert f.taken_at == "2023-07-08T09:10:11"  # type: ignore[attr-defined]
    # 回転を適用した dHash は、あらかじめ回した画像の dHash と一致する
    upright = save_jpeg(img.transpose(Image.Transpose.ROTATE_270), tmp_path / "upright.jpg", 90)
    _, g = feat(upright)
    assert bin(f.dhash ^ g.dhash).count("1") <= 2  # type: ignore[attr-defined]


def test_unreadable_and_changed(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(os.urandom(20_000))
    _, f = feat(bad)
    assert f.dhash is None and f.sha256  # type: ignore[attr-defined]
    good = save_jpeg(base_image(4), tmp_path / "g.jpg")
    st = good.stat()
    with pytest.raises(FileChangedError):
        compute_features(str(good), st.st_size + 1, st.st_mtime_ns)
    with pytest.raises(FileChangedError):
        compute_features(str(tmp_path / "gone.jpg"), 1, 1)


def test_png_and_webp_are_read(tmp_path: Path) -> None:
    img = base_image(5, (640, 480))
    png = tmp_path / "a.png"
    img.save(png)
    webp = tmp_path / "a.webp"
    img.save(webp, quality=90)
    for p in (png, webp):
        _, f = feat(p)
        assert f.dhash is not None and f.width == 640  # type: ignore[attr-defined]


# ------------------------------------------------------------------ 列挙
def _big(p: Path, seed: int = 0) -> Path:
    return save_jpeg(base_image(seed), p)


def test_enumerate_filters(tmp_path: Path) -> None:
    root = tmp_path / "photos"
    _big(root / "a.jpg")
    _big(root / "sub" / "b.JPG", 1)
    (root / "tiny.jpg").write_bytes(b"x" * 100)       # 10KB 未満
    (root / "note.txt").write_bytes(b"x" * 20_000)   # 対象外の拡張子
    hidden = _big(root / "h.jpg", 2)
    ctypes.windll.kernel32.SetFileAttributesW(str(hidden), FILE_ATTRIBUTE_HIDDEN)
    cloud = _big(root / "c.jpg", 3)
    ctypes.windll.kernel32.SetFileAttributesW(str(cloud), FILE_ATTRIBUTE_OFFLINE)
    try:
        out = enumerate_images([root], recursive=True, excluded=[])
        names = sorted(Path(f.path).name for f in out.files)
        assert names == ["a.jpg", "b.JPG"]
        assert out.cloud_only == 1
        flat = enumerate_images([root], recursive=False, excluded=[])
        assert [Path(f.path).name for f in flat.files] == ["a.jpg"]
    finally:
        for p in (hidden, cloud):
            ctypes.windll.kernel32.SetFileAttributesW(str(p), 0x80)


def test_ac7_junction_not_followed_and_nested_roots(tmp_path: Path) -> None:
    import _winapi

    root = tmp_path / "photos"
    _big(root / "a.jpg")
    outside = tmp_path / "outside"
    _big(outside / "x.jpg", 1)
    _winapi.CreateJunction(str(outside), str(root / "link"))  # type: ignore[attr-defined]
    only_root = enumerate_images([root], recursive=True, excluded=[])
    assert [Path(f.path).name for f in only_root.files] == ["a.jpg"]
    # 入れ子のフォルダは1回だけ数える
    _big(root / "sub" / "b.jpg", 2)
    nested = enumerate_images([root / "sub", root], recursive=True, excluded=[])
    assert sorted(Path(f.path).name for f in nested.files) == ["a.jpg", "b.jpg"]
    assert all(os.path.normcase(f.root) == os.path.normcase(os.path.realpath(root)) for f in nested.files)


def test_ac7_forbidden_folders(tmp_path: Path) -> None:
    appdata = tmp_path / "Roaming"
    _big(appdata / "pic.jpg")
    env = {"APPDATA": str(appdata), "WINDIR": str(tmp_path / "Windows")}
    ex = excluded_dirs(env)
    assert check_root(appdata, ex) == "forbidden"
    assert check_root(appdata / "..", ex) == "ok"
    (appdata / "deep").mkdir()
    assert check_root(appdata / "deep", ex) == "forbidden"
    assert check_root(tmp_path / "nope", ex) == "missing"
    assert check_root(Path(tmp_path.anchor), ex) == "drive_root"
    out = enumerate_images([appdata], recursive=True, excluded=ex)
    assert out.files == [] and out.forbidden_roots == 1
    # 親を選んでも、除外フォルダの中は調べない
    out2 = enumerate_images([tmp_path], recursive=True, excluded=ex)
    assert not any("Roaming" in f.path for f in out2.files)


def test_fr5_too_many(tmp_path: Path) -> None:
    for i in range(4):
        _big(tmp_path / f"{i}.jpg", i)
    out = enumerate_images([tmp_path], recursive=True, excluded=[], max_files=3)
    assert out.too_many
    ok = enumerate_images([tmp_path], recursive=True, excluded=[], max_files=4)
    assert not ok.too_many and len(ok.files) == 4


def test_enumerate_cancel(tmp_path: Path) -> None:
    _big(tmp_path / "a.jpg")
    ev = threading.Event()
    ev.set()
    assert enumerate_images([tmp_path], recursive=True, excluded=[], cancel=ev).cancelled


# ------------------------------------------------------------------ キャッシュ
def test_cache_roundtrip_and_key(tmp_path: Path) -> None:
    p = _big(tmp_path / "a.jpg")
    st = p.stat()
    f = compute_features(str(p), st.st_size, st.st_mtime_ns)
    db = tmp_path / "c" / "cache.db"
    c = FeatureCache(db)
    c.put(str(p), st.st_size, st.st_mtime_ns, f)
    c.put("C:\\x.jpg", 1, 1, type(f)(f.sha256, None, 0, 0, None, 0.0))
    c.close()
    c2 = FeatureCache(db)
    assert c2.get(str(p), st.st_size, st.st_mtime_ns) == f
    assert c2.get(str(p), st.st_size + 1, st.st_mtime_ns) is None
    assert c2.get("C:\\x.jpg", 1, 1).dhash is None  # type: ignore[union-attr]
    c2.close()
    assert row_count(db) == 2
    big = type(f)(f.sha256, (1 << 64) - 1, 1, 1, None, 0.0)  # 64bit の最上位ビットも保てる
    c3 = FeatureCache(db)
    c3.put("C:\\y.jpg", 2, 2, big)
    assert c3.get("C:\\y.jpg", 2, 2) == big
    c3.close()


def test_cache_broken_is_rebuilt(tmp_path: Path) -> None:
    db = tmp_path / "cache.db"
    db.write_bytes(b"this is not a database" * 100)
    c = FeatureCache(db)
    assert c.rebuilt
    assert c.count() == 0
    c.close()
    assert (tmp_path / "cache.db.broken").exists()


def test_cache_purge_and_clear(tmp_path: Path) -> None:
    db = tmp_path / "cache.db"
    old = FeatureCache(db, today=date.today() - timedelta(days=40))
    from deskkit.modules.twinsweep.hashing import Features

    old.put("C:\\old.jpg", 1, 1, Features("s", 1, 1, 1, None, 0.0))
    old.close()
    new = FeatureCache(db)
    new.put("C:\\new.jpg", 1, 1, Features("s", 1, 1, 1, None, 0.0))
    assert new.purge_unused() == 1
    assert new.count() == 1
    new.close()
    assert clear_cache(db)
    assert not db.exists()
    assert row_count(db) == 0


def test_cache_connection_does_not_cross_threads(tmp_path: Path) -> None:
    c = FeatureCache(tmp_path / "cache.db")
    err: list[BaseException] = []

    def other() -> None:
        try:
            c.count()
        except sqlite3.ProgrammingError as e:
            err.append(e)

    t = threading.Thread(target=other)
    t.start()
    t.join()
    c.close()
    assert err, "別スレッドからは使えない(check_same_thread)"
