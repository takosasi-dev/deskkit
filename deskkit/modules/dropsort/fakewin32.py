# selftest と pytest 用の偽 Win32 層。ファイル・ボリューム・Zone.Identifier をメモリ上で模擬する。
# 実ファイルには一切触らない(ボリュームの FS・ネットワーク・ロック中・リパースポイント・ADS 欠落を再現できる)。
from __future__ import annotations

import ntpath
import time
from dataclasses import dataclass, field

from deskkit.modules.dropsort import _win32 as W


@dataclass
class FakeFile:
    path: str
    size: int
    mtime: float
    ctime: float
    atime: float
    attrs: int = 0
    streams: dict[str, bytes] = field(default_factory=dict)
    locked: bool = False
    stream_unreadable: bool = False


@dataclass
class FakeVolume:
    root: str
    fs_name: str = "NTFS"
    flags: int = W.FILE_NAMED_STREAMS
    serial: int = 1
    drive_type: int = W.DRIVE_FIXED


def _k(p: str) -> str:
    return ntpath.normcase(ntpath.normpath(p))


class FakeWin32:
    """メモリ上の偽ファイルシステム。パスは 'C:\\DL\\a.pdf' 形式。"""

    def __init__(self, downloads: str | None = "C:\\Users\\u\\DL") -> None:
        self.downloads = downloads
        self.files: dict[str, FakeFile] = {}
        self.dirs: dict[str, str] = {}
        self.volumes: dict[str, FakeVolume] = {}
        self.links: dict[str, str] = {}  # ジャンクション: パス → 実体
        self.copy_drops_streams = False  # CopyFileExW が ADS を落とす状況を再現する
        self.calls: list[tuple[str, str]] = []
        self.add_volume("C:\\", "NTFS", serial=1)
        if downloads:
            self.mkdirs(downloads)

    # ---- テスト用の操作
    def add_volume(self, root: str, fs_name: str = "NTFS", *, serial: int = 0, drive_type: int = W.DRIVE_FIXED,
                   named_streams: bool | None = None) -> None:
        ns = (fs_name.upper() == "NTFS") if named_streams is None else named_streams
        self.volumes[_k(root)] = FakeVolume(root, fs_name, W.FILE_NAMED_STREAMS if ns else 0,
                                            serial or (len(self.volumes) + 1), drive_type)
        self.dirs[_k(root)] = root

    def mkdirs(self, path: str) -> None:
        p = ntpath.normpath(path)
        parts: list[str] = []
        while True:
            parent = ntpath.dirname(p)
            parts.append(p)
            if parent == p or not parent:
                break
            p = parent
        for q in reversed(parts):
            self.dirs.setdefault(_k(q), q)

    def add_file(self, path: str, size: int = 100, *, mtime: float | None = None, ctime: float | None = None,
                 atime: float | None = None, zone: bytes | None = None, attrs: int = 0, locked: bool = False) -> FakeFile:
        now = time.time()
        self.mkdirs(ntpath.dirname(path))
        f = FakeFile(path, size, mtime if mtime is not None else now, ctime if ctime is not None else now,
                     atime if atime is not None else now, attrs, {}, locked)
        if zone is not None:
            f.streams[W.ZONE_STREAM] = zone
        self.files[_k(path)] = f
        return f

    def get(self, path: str) -> FakeFile | None:
        return self.files.get(_k(path))

    def names_in(self, folder: str) -> list[str]:
        return sorted(ntpath.basename(f.path) for f in self.files.values() if _k(ntpath.dirname(f.path)) == _k(folder))

    def _vol(self, path: str) -> FakeVolume | None:
        if path.startswith("\\\\"):
            parts = path[2:].split("\\")
            if len(parts) >= 2:
                root = "\\\\" + parts[0] + "\\" + parts[1] + "\\"
                return self.volumes.get(_k(root))
            return None
        return self.volumes.get(_k(path[:3]))

    # ---- Win32Api
    def known_folder_downloads(self) -> str | None:
        return self.downloads

    def list_dir(self, path: str) -> list[W.FileInfo]:
        kp = _k(path)
        if kp not in self.dirs:
            raise FileNotFoundError(path)
        out: list[W.FileInfo] = []
        for kd, d in self.dirs.items():
            if kd != kp and _k(ntpath.dirname(d)) == kp:
                attrs = W.FILE_ATTRIBUTE_DIRECTORY | (W.FILE_ATTRIBUTE_REPARSE_POINT if kd in self.links else 0)
                out.append(W.FileInfo(ntpath.basename(d), d, 0, 0.0, 0.0, 0.0, attrs, True))
        for f in self.files.values():
            if _k(ntpath.dirname(f.path)) == kp:
                out.append(self._info(f))
        return out

    def _info(self, f: FakeFile) -> W.FileInfo:
        return W.FileInfo(ntpath.basename(f.path), f.path, f.size, f.mtime, f.ctime, f.atime, f.attrs, False)

    def stat(self, path: str) -> W.FileInfo | None:
        f = self.files.get(_k(path))
        if f is not None:
            return self._info(f)
        d = self.dirs.get(_k(path))
        if d is not None:
            return W.FileInfo(ntpath.basename(d), d, 0, 0.0, 0.0, 0.0, W.FILE_ATTRIBUTE_DIRECTORY, True)
        return None

    def exists(self, path: str) -> bool:
        return _k(path) in self.files or _k(path) in self.dirs

    def is_dir(self, path: str) -> bool:
        return _k(path) in self.dirs

    def try_exclusive_open(self, path: str) -> int:
        f = self.files.get(_k(path))
        if f is None:
            return W.ERROR_FILE_NOT_FOUND
        return W.ERROR_SHARING_VIOLATION if f.locked else W.ERROR_SUCCESS

    def read_zone_identifier(self, path: str) -> W.StreamRead:
        f = self.files.get(_k(path))
        if f is None or W.ZONE_STREAM not in f.streams:
            return W.StreamRead(False, None)
        if f.stream_unreadable:
            return W.StreamRead(True, None)
        return W.StreamRead(True, f.streams[W.ZONE_STREAM])

    def volume_root(self, path: str) -> str | None:
        v = self._vol(path)
        return v.root if v else None

    def volume_info(self, root: str) -> W.VolumeInfo | None:
        v = self.volumes.get(_k(root))
        if v is None:
            return None
        return W.VolumeInfo(v.root, v.fs_name, v.flags, v.serial, v.drive_type)

    def final_path(self, path: str) -> str | None:
        kp = _k(path)
        for link, target in self.links.items():
            if kp == link or kp.startswith(link + "\\"):
                return ntpath.normpath(target + path[len(link):])
        if kp in self.dirs:
            return self.dirs[kp]
        if kp in self.files:
            return self.files[kp].path
        return None

    def _check_dst(self, src: str, dst: str) -> tuple[FakeFile | None, int]:
        f = self.files.get(_k(src))
        if f is None:
            return None, W.ERROR_FILE_NOT_FOUND
        if f.locked:
            return None, W.ERROR_SHARING_VIOLATION
        if _k(ntpath.dirname(dst)) not in self.dirs:
            return None, W.ERROR_PATH_NOT_FOUND
        if self.exists(dst):
            return None, W.ERROR_ALREADY_EXISTS
        return f, 0

    def MoveFileExW(self, src: str, dst: str) -> int:
        self.calls.append(("MoveFileExW", src))
        vs, vd = self._vol(src), self._vol(dst)
        if vs is None or vd is None or vs.serial != vd.serial:
            return W.ERROR_NOT_SAME_DEVICE
        f, err = self._check_dst(src, dst)
        if f is None:
            return err
        del self.files[_k(src)]
        f.path = dst
        self.files[_k(dst)] = f
        return 0

    def CopyFileExW(self, src: str, dst: str) -> int:
        self.calls.append(("CopyFileExW", src))
        f, err = self._check_dst(src, dst)
        if f is None:
            return W.ERROR_FILE_EXISTS if err == W.ERROR_ALREADY_EXISTS else err
        vd = self._vol(dst)
        keep = vd is not None and bool(vd.flags & W.FILE_NAMED_STREAMS) and not self.copy_drops_streams
        c = FakeFile(dst, f.size, f.mtime, time.time(), f.atime, f.attrs, dict(f.streams) if keep else {})
        self.files[_k(dst)] = c
        return 0

    def DeleteFileW(self, path: str) -> int:
        self.calls.append(("DeleteFileW", path))
        f = self.files.get(_k(path))
        if f is None:
            return W.ERROR_FILE_NOT_FOUND
        if f.locked:
            return W.ERROR_SHARING_VIOLATION
        del self.files[_k(path)]
        return 0

    def create_directory(self, path: str) -> int:
        if self.exists(path):
            return W.ERROR_ALREADY_EXISTS
        if _k(ntpath.dirname(ntpath.normpath(path))) not in self.dirs:
            return W.ERROR_PATH_NOT_FOUND
        self.dirs[_k(path)] = ntpath.normpath(path)
        return 0
