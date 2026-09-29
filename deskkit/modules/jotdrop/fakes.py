# テストと自己検査で使う偽の Win32Api。ファイルはメモリの中だけに持ち、開いたときの権限・共有・作り方を記録する。
# 「ほかのアプリが開いている」(locked)・読み取り専用(readonly)・ネットワークのドライブ(remote)・書き込みの失敗を差し込める。
from __future__ import annotations

import ntpath
from dataclasses import dataclass, field

from deskkit.modules.jotdrop._win32 import (
    CREATE_NEW,
    DRIVE_REMOTE,
    ERROR_ACCESS_DENIED,
    ERROR_FILE_EXISTS,
    ERROR_FILE_NOT_FOUND,
    ERROR_PATH_NOT_FOUND,
    ERROR_SHARING_VIOLATION,
    FILE_APPEND_DATA,
    FILE_READ_DATA,
    FILE_WRITE_DATA,
    GENERIC_WRITE,
    OPEN_EXISTING,
)

DRIVE_FIXED = 3


def _err(code: int) -> OSError:
    return OSError(0, "fake", None, code)


def norm(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path))


@dataclass
class OpenCall:
    path: str
    access: int
    share: int
    disposition: int


@dataclass
class FakeWin32:
    files: dict[str, bytearray] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=set)
    remote: set[str] = field(default_factory=set)       # "Z:\\" のようなドライブの根
    locked: set[str] = field(default_factory=set)       # ほかのアプリが共有なしで開いている
    readonly: set[str] = field(default_factory=set)
    fail_append: set[str] = field(default_factory=set)
    opens: list[OpenCall] = field(default_factory=list)
    handles: dict[int, tuple[str, int]] = field(default_factory=dict)
    next_handle: int = 100
    # 窓
    foreground: int = 0
    window_pids: dict[int, int] = field(default_factory=dict)
    rects: dict[int, tuple[int, int, int, int]] = field(default_factory=dict)
    set_calls: list[int] = field(default_factory=list)
    set_result: bool = True

    # ---- 準備用
    def add_dir(self, path: str) -> None:
        self.dirs.add(norm(path))

    def put(self, path: str, data: bytes) -> None:
        self.add_dir(ntpath.dirname(path))
        self.files[norm(path)] = bytearray(data)

    def get(self, path: str) -> bytes | None:
        b = self.files.get(norm(path))
        return None if b is None else bytes(b)

    # ---- Win32Api
    def create_file(self, path: str, access: int, share: int, disposition: int) -> int:
        self.opens.append(OpenCall(path, access, share, disposition))
        p = norm(path)
        parent = ntpath.dirname(p)
        if p in self.locked:
            raise _err(ERROR_SHARING_VIOLATION)
        if any(hp == p for hp, _a in self.handles.values()) and share == 0:
            raise _err(ERROR_SHARING_VIOLATION)
        exists = p in self.files
        if disposition == OPEN_EXISTING and not exists:
            raise _err(ERROR_FILE_NOT_FOUND if parent in self.dirs else ERROR_PATH_NOT_FOUND)
        if disposition == CREATE_NEW:
            if exists:
                raise _err(ERROR_FILE_EXISTS)
            if parent not in self.dirs:
                raise _err(ERROR_PATH_NOT_FOUND)
        if p in self.readonly and access & (FILE_WRITE_DATA | FILE_APPEND_DATA | GENERIC_WRITE):
            raise _err(ERROR_ACCESS_DENIED)
        if disposition == CREATE_NEW:
            self.files[p] = bytearray()
        h = self.next_handle
        self.next_handle += 1
        self.handles[h] = (p, access)
        return h

    def _h(self, handle: int) -> tuple[str, int]:
        if handle not in self.handles:
            raise _err(6)
        return self.handles[handle]

    def read_at(self, handle: int, offset: int, size: int) -> bytes:
        p, access = self._h(handle)
        if not access & FILE_READ_DATA:
            raise _err(ERROR_ACCESS_DENIED)
        return bytes(self.files[p][offset:offset + size])

    def append(self, handle: int, data: bytes) -> None:
        p, access = self._h(handle)
        if not access & (FILE_APPEND_DATA | FILE_WRITE_DATA):
            raise _err(ERROR_ACCESS_DENIED)
        if p in self.fail_append:
            raise _err(ERROR_SHARING_VIOLATION)
        self.files[p] += data  # FILE_APPEND_DATA は末尾にしか書けない

    def size(self, handle: int) -> int:
        p, _a = self._h(handle)
        return len(self.files[p])

    def truncate(self, handle: int, size: int) -> None:
        p, access = self._h(handle)
        if not access & FILE_WRITE_DATA:
            raise _err(ERROR_ACCESS_DENIED)
        del self.files[p][size:]

    def close(self, handle: int) -> None:
        self.handles.pop(handle, None)

    def is_dir(self, path: str) -> bool:
        return norm(path) in self.dirs

    def make_dir(self, path: str) -> None:
        parent = ntpath.dirname(norm(path))
        if parent not in self.dirs:
            raise _err(ERROR_PATH_NOT_FOUND)
        self.dirs.add(norm(path))

    def drive_type(self, root: str) -> int:
        return DRIVE_REMOTE if root.upper() in {r.upper() for r in self.remote} else DRIVE_FIXED

    def foreground_window(self) -> int:
        return self.foreground

    def set_foreground(self, hwnd: int) -> bool:
        self.set_calls.append(hwnd)
        if self.set_result:
            self.foreground = hwnd
        return self.set_result

    def is_window(self, hwnd: int) -> bool:
        return hwnd in self.window_pids

    def window_pid(self, hwnd: int) -> int:
        return self.window_pids.get(hwnd, 0)

    def window_rect(self, hwnd: int) -> tuple[int, int, int, int] | None:
        return self.rects.get(hwnd)
