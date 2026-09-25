# deskkit.fileops.recycle の振る舞いを偽の RecycleApi で確かめる(v0.3 共通 H-2〜H-4・VAC-5)。
from __future__ import annotations

from pathlib import Path

import pytest

from deskkit import fileops


class FakeApi:
    def __init__(self, files: set[Path], drive: int = fileops.DRIVE_FIXED, rc: int = 0, abort_on: Path | None = None) -> None:
        self.files = set(files)
        self.drive = drive
        self.rc = rc
        self.abort_on = abort_on
        self.calls: list[Path] = []

    def drive_type(self, path: Path) -> int:
        return self.drive

    def delete_to_recycle_bin(self, path: Path, parent_hwnd: int | None) -> tuple[int, bool]:
        self.calls.append(path)
        if path == self.abort_on:
            return 0x4C7, True
        if self.rc == 0:
            self.files.discard(path)
        return self.rc, False

    def exists(self, path: Path) -> bool:
        return path in self.files


def test_sends_existing_files_on_fixed_drive(tmp_path: Path) -> None:
    a, b = tmp_path / "a.jpg", tmp_path / "b.jpg"
    api = FakeApi({a, b})
    r = fileops.recycle([a, b], api=api)
    assert r.sent == [a, b]
    assert r.skipped == []


def test_removable_drive_is_skipped_and_file_kept(tmp_path: Path) -> None:
    a = tmp_path / "a.jpg"
    api = FakeApi({a}, drive=2)  # DRIVE_REMOVABLE
    r = fileops.recycle([a], api=api)
    assert r.sent == []
    assert r.skipped == [(a, "skipped_no_recycle_bin")]
    assert api.calls == []
    assert a in api.files


def test_missing_file_is_not_found(tmp_path: Path) -> None:
    a = tmp_path / "gone.jpg"
    r = fileops.recycle([a], api=FakeApi(set()))
    assert r.skipped == [(a, "not_found")]


def test_sharing_violation_is_in_use(tmp_path: Path) -> None:
    a = tmp_path / "a.jpg"
    r = fileops.recycle([a], api=FakeApi({a}, rc=fileops.ERROR_SHARING_VIOLATION))
    assert r.count("in_use") == 1


def test_abort_stops_remaining(tmp_path: Path) -> None:
    a, b, c = (tmp_path / f"{n}.jpg" for n in "abc")
    api = FakeApi({a, b, c}, abort_on=b)
    r = fileops.recycle([a, b, c], api=api)
    assert r.sent == [a]
    assert r.skipped == [(b, "aborted"), (c, "aborted")]
    assert api.calls == [a, b]


@pytest.mark.parametrize("name", ["rel.jpg", "C:/x/*.jpg", "C:/x/?.jpg"])
def test_relative_or_wildcard_paths_are_refused(name: str) -> None:
    p = Path(name)
    api = FakeApi({p})
    r = fileops.recycle([p], api=api)
    assert r.skipped == [(p, "failed")]
    assert api.calls == []


def test_flags_include_undo_and_nuke_warning() -> None:
    assert fileops.RECYCLE_FLAGS & fileops.FOF_ALLOWUNDO
    assert fileops.RECYCLE_FLAGS & fileops.FOF_WANTNUKEWARNING


@pytest.mark.win32_real
def test_real_recycle_of_temp_file(tmp_path: Path) -> None:
    p = tmp_path / "deskkit_fileops_probe.txt"
    p.write_text("probe", encoding="utf-8")
    r = fileops.recycle([p])
    assert r.sent == [p] or r.count("skipped_no_recycle_bin") == 1
