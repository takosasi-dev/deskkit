# 実機の Win32 を使うテスト(pytest -m win32_real)。一時フォルダだけを使い、実際のダウンロードフォルダは
# 場所の解決(読み取り)以外では触らない。
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from deskkit.modules.dropsort import _win32 as W
from deskkit.modules.dropsort.config import fill_defaults, load_config
from deskkit.modules.dropsort.service import DropSortService
from deskkit.modules.dropsort.watcher import DirWatcher

pytestmark = pytest.mark.win32_real

ZONE = "[ZoneTransfer]\r\nZoneId=3\r\nHostUrl=https://example.com/f.pdf?token=SECRET\r\n"


def _svc(data: Path, dl: Path, rules: list[dict[str, object]]) -> DropSortService:
    sec, _ = fill_defaults({"downloads_dir_override": str(dl), "stable_seconds": 0, "rules": rules})
    return DropSortService(W.RealWin32(), data, load_config(sec))


def _write(p: Path, body: str, zone: str | None) -> None:
    p.write_text(body, encoding="utf-8")
    if zone is not None:
        with open(str(p) + ":Zone.Identifier", "w", encoding="utf-8", newline="") as f:
            f.write(zone)


def _zone(p: Path) -> bytes:
    with open(str(p) + ":Zone.Identifier", "rb") as f:
        return f.read()


def test_known_folder_downloads_resolves() -> None:
    p = W.RealWin32().known_folder_downloads()
    assert p and os.path.isdir(p)


def test_same_volume_move_keeps_zone_identifier(tmp_path: Path) -> None:
    dl, dest = tmp_path / "dl", tmp_path / "dest"
    dl.mkdir()
    dest.mkdir()
    (dest / "a.pdf").write_text("existing", encoding="utf-8")
    svc = _svc(tmp_path / "data", dl, [{"name": "pdf", "mode": "apply", "match": {"ext": [".pdf"]}, "dest": str(dest)}])
    assert svc.run_cycle().baseline_created == 0
    _write(dl / "a.pdf", "body", ZONE)
    before = _zone(dl / "a.pdf")
    svc.run_cycle()
    time.sleep(0.05)
    svc.run_cycle()
    moved = dest / "a (1).pdf"
    assert moved.exists() and not (dl / "a.pdf").exists()
    assert _zone(moved) == before  # AC-5
    assert (dest / "a.pdf").read_text(encoding="utf-8") == "existing"  # AC-10(上書きしない)
    log = (tmp_path / "data" / "oplog.jsonl").read_text(encoding="utf-8")
    assert "http" not in log and "SECRET" not in log  # AC-14
    u = svc.undo(1)
    assert u.restored and (dl / "a.pdf").exists()
    assert _zone(dl / "a.pdf") == before


def test_exclusive_open_detects_holder(tmp_path: Path) -> None:
    p = tmp_path / "x.bin"
    p.write_bytes(b"x")
    api = W.RealWin32()
    assert api.try_exclusive_open(str(p)) == 0
    with open(p, "rb"):
        assert api.try_exclusive_open(str(p)) == W.ERROR_SHARING_VIOLATION


def test_move_refuses_overwrite_and_cross_volume_flag(tmp_path: Path) -> None:
    api = W.RealWin32()
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("a")
    b.write_text("b")
    assert api.MoveFileExW(str(a), str(b)) in W.EXISTS_ERRORS
    assert b.read_text() == "b"
    assert api.CopyFileExW(str(a), str(b)) in W.EXISTS_ERRORS


def _other_volume() -> str | None:
    """一時フォルダと別のボリュームで、書き込めるドライブのルート(無ければ None)。"""
    api = W.RealWin32()
    tmp_root = (api.volume_root(tempfile.gettempdir()) or "").upper()
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        vi = api.volume_info(root)
        if vi and not vi.is_remote and root.upper() != tmp_root and vi.drive_type in (W.DRIVE_FIXED, W.DRIVE_REMOVABLE):
            return root
    return None


def test_cross_volume_motw_policy(tmp_path: Path) -> None:
    root = _other_volume()
    if root is None:
        pytest.skip("別ボリュームが無い")
    api = W.RealWin32()
    vi = api.volume_info(root)
    assert vi is not None
    other = Path(tempfile.mkdtemp(prefix="dropsort-xvol-", dir=root))
    try:
        dl = tmp_path / "dl"
        dl.mkdir()
        svc = _svc(tmp_path / "data", dl, [{"name": "x", "mode": "apply", "match": {}, "dest": str(other)}])
        svc.run_cycle()
        _write(dl / "m.pdf", "motw", ZONE)
        _write(dl / "plain.txt", "plain", None)
        svc.run_cycle()
        time.sleep(0.05)
        svc.run_cycle()
        assert (other / "plain.txt").read_text(encoding="utf-8") == "plain" and not (dl / "plain.txt").exists()
        ops = {r["src"].rsplit("\\", 1)[1]: r for r in svc.oplog.all()}
        if vi.named_streams:
            assert (other / "m.pdf").exists()  # AC-6
            assert _zone(other / "m.pdf") == ZONE.encode("utf-8")
        else:
            assert (dl / "m.pdf").exists() and not (other / "m.pdf").exists()  # AC-7
            assert ops["m.pdf"]["op"] == "refused" and ops["m.pdf"]["reason"] == "motw_unsupported_fs"
    finally:
        shutil.rmtree(other, ignore_errors=True)


def test_watcher_signals_and_stops(tmp_path: Path) -> None:
    hits: list[int] = []
    dead: list[str] = []
    w = DirWatcher(W.RealWatchApi(), str(tmp_path), lambda: hits.append(1), dead.append)
    w.start()
    (tmp_path / "x.txt").write_text("x")
    deadline = time.monotonic() + 3
    while not hits and time.monotonic() < deadline:
        time.sleep(0.05)
    t0 = time.monotonic()
    assert w.stop()
    assert time.monotonic() - t0 < 2.0
    assert hits and not dead
