# §8 の実測の材料(win32_real。既定では走らない。`pytest -m win32_real -s tests/modules/plugsave/test_measure_real.py`)。
# コピー元もバックアップ先も一時フォルダ(この PC のシステムドライブの NTFS)。USB メモリ・SSD での実測は docs/v0.4/plugsave.md の「実機で確かめること」。
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from deskkit.modules.plugsave import copier
from deskkit.modules.plugsave.copier import BackupRequest, BackupRun
from deskkit.modules.plugsave.fakes import FakeDriveApi
from deskkit.modules.plugsave.planner import Source

N_FILES = 10_000
SIZE = 10 * 1024          # 合計 約 100MB(1GB は実機の確認で)


def _once(src: Path, drive: Path, api: FakeDriveApi) -> tuple[float, copier.BackupOutcome]:
    req = BackupRequest(root=str(drive) + os.sep, fs="NTFS", pc="PC", sources=[Source(str(src), "S")])
    t0 = time.perf_counter()
    out = BackupRun(req, api).run()
    return time.perf_counter() - t0, out


@pytest.mark.win32_real
def test_measure_first_and_second(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "src"
    blob = os.urandom(SIZE)
    for i in range(N_FILES):
        d = src / f"d{i // 500:02}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"f{i:05}.bin").write_bytes(blob)
    results: dict[str, float] = {}
    for label, fsync in (("fsync", True), ("nofsync", False)):
        drive = tmp_path / f"drive_{label}"
        drive.mkdir()
        api = FakeDriveApi()
        api.add("Q", str(drive), total=1 << 40, free=1 << 40)
        if not fsync:
            monkeypatch.setattr(copier.os, "fsync", lambda _fd: None)
        t1, o1 = _once(src, drive, api)
        t2, o2 = _once(src, drive, api)
        monkeypatch.undo()
        assert o1.new == N_FILES and o2.unchanged == N_FILES and o2.copied == 0
        results[f"{label}_first"] = t1
        results[f"{label}_second"] = t2
    print({k: round(v, 2) for k, v in results.items()})
