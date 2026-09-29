# 本体の ffmpeg 部品(v0.4 で SendPrep から移した)の共有の決まり: 管理役が1つ・展開先ごとの鍵・v0.3 の展開先の片づけ。
from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from deskkit import ffmpeg as F


def _bundle(tmp: Path, body: bytes = b"MZfake-ffmpeg") -> Path:
    bd = tmp / "bundle"
    bd.mkdir()
    with zipfile.ZipFile(bd / F.BUNDLE_ZIP, "w") as zf:
        zf.writestr("ffmpeg/bin/ffmpeg.exe", body)
        zf.writestr("ffmpeg/LICENSE.txt", "LGPL")
    (bd / F.BUNDLE_SHA).write_text(hashlib.sha256(body).hexdigest() + "\n", encoding="ascii")
    return bd


def test_shared_is_one_instance_under_deskkit_home() -> None:
    a = F.shared()
    b = F.shared()
    assert a is b
    assert a.root == F.shared_root()
    assert F.shared_root().name == "ffmpeg"


def test_same_root_shares_lock(tmp_path: Path) -> None:
    m1 = F.FfmpegManager(tmp_path / "ff")
    m2 = F.FfmpegManager(tmp_path / "ff")
    m3 = F.FfmpegManager(tmp_path / "other")
    assert m1._lock is m2._lock
    assert m1._lock is not m3._lock


def test_ensure_extracts_and_second_manager_reuses(tmp_path: Path) -> None:
    bd = _bundle(tmp_path)
    m1 = F.FfmpegManager(tmp_path / "ff", lambda: bd)
    exe = m1.ensure()
    assert exe.read_bytes() == b"MZfake-ffmpeg" and (exe.parent / "LICENSE.txt").is_file()
    calls: list[int] = []
    m2 = F.FfmpegManager(tmp_path / "ff", lambda: bd)
    assert m2.ensure(lambda: calls.append(1)) == exe
    assert calls == []  # 展開済みなので「準備中」は出ない


def test_errors_have_codes(tmp_path: Path) -> None:
    with pytest.raises(F.FfmpegError) as ei:
        F.FfmpegManager(tmp_path / "a", lambda: None).ensure()
    assert ei.value.code == F.CODE_MISSING
    bd = _bundle(tmp_path)
    (bd / F.BUNDLE_SHA).write_text("0" * 64, encoding="ascii")
    m = F.FfmpegManager(tmp_path / "b", lambda: bd)
    with pytest.raises(F.FfmpegError) as ei2:
        m.ensure()
    assert ei2.value.code == F.CODE_BROKEN and m.state() == F.STATE_VERIFY_FAILED


def test_cleanup_legacy_removes_only_own_files(tmp_path: Path) -> None:
    old = tmp_path / "sendprep" / "ffmpeg"
    d = old / "0123456789abcdef"
    d.mkdir(parents=True)
    (d / "ffmpeg.exe").write_bytes(b"x")
    (d / "LICENSE.txt").write_text("l")
    other = old / "keep-me"
    other.mkdir()
    (other / "note.txt").write_text("user")
    F.cleanup_legacy(old)
    assert not d.exists()
    assert (other / "note.txt").is_file()  # 知らない名前のフォルダには触れない
    empty = tmp_path / "e" / "ffmpeg"
    (empty / "fedcba9876543210").mkdir(parents=True)
    (empty / "fedcba9876543210" / "ffmpeg.exe").write_bytes(b"x")
    F.cleanup_legacy(empty)
    assert not empty.exists()
