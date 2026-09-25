# フルスキャン: ダウンロードフォルダ直下を1回列挙して、ファイルとフォルダに分ける(サブフォルダは再帰しない)。
# 監視イベントは「スキャンのきっかけ」にだけ使い、判断は常にこの実状態から行う(D-2)。
from __future__ import annotations

import time
from dataclasses import dataclass, field

from deskkit.modules.dropsort._win32 import FileInfo, Win32Api


@dataclass
class Snapshot:
    path: str
    files: list[FileInfo] = field(default_factory=list)
    dirs: list[FileInfo] = field(default_factory=list)
    elapsed_ms: float = 0.0

    def names(self) -> set[str]:
        return {f.name.lower() for f in self.files}


def scan_dir(api: Win32Api, path: str) -> Snapshot:
    """直下を列挙する。フォルダ(リパースポイントのフォルダを含む)は dirs に分け、処理対象にしない。"""
    t0 = time.perf_counter()
    snap = Snapshot(path)
    for e in api.list_dir(path):
        (snap.dirs if e.is_dir else snap.files).append(e)
    snap.files.sort(key=lambda f: f.name.lower())
    snap.elapsed_ms = (time.perf_counter() - t0) * 1000
    return snap
