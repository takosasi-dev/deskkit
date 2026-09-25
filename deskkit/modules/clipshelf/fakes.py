# テストと自己検査用の偽物(偽 Win32Api・偽暗号器・偽の時計)。実機のクリップボード・入力には一切触れない。
# FakeWin32 は CF_UNICODETEXT の取得回数・SendInput 回数などを数え、INV-3 / INV-6 の検査に使う。
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from deskkit.modules.clipshelf._win32 import CF_UNICODETEXT, STANDARD_FORMAT_NAMES


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.t = start or datetime(2026, 10, 1, 10, 0, 0).astimezone()

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t = self.t + timedelta(**kw)


class FakeCipher:
    """平文が暗号文に現れない可逆変換(テスト用。実運用では使わない)。"""

    def protect(self, data: bytes) -> bytes:
        return b"FAKE" + bytes(b ^ 0xA5 for b in reversed(data))

    def unprotect(self, data: bytes) -> bytes:
        if not data.startswith(b"FAKE"):
            from deskkit.modules.clipshelf._win32 import CryptoError

            raise CryptoError("fake unprotect", 13)
        return bytes(b ^ 0xA5 for b in reversed(data[4:]))


@dataclass
class FakeWin32:
    clip: dict[int, bytes] = field(default_factory=dict)
    owner_hwnd: int = 0
    seq: int = 1
    busy_opens: int = 0           # 次の OpenClipboard を何回失敗させるか
    windows: dict[int, int] = field(default_factory=dict)   # hwnd → pid
    exes: dict[int, str] = field(default_factory=dict)      # pid → exe フルパス
    fg_hwnd: int = 0
    set_fg_ok: bool = True
    registered: dict[str, int] = field(default_factory=dict)
    running: list[str] = field(default_factory=lambda: ["explorer.exe", "notepad.exe"])
    # 呼び出し回数
    text_reads: int = 0
    send_input_calls: int = 0
    set_fg_calls: int = 0
    opens: int = 0
    is_open: bool = False
    _owner_candidate: int = 0

    # ---------------------------------------------------------- テスト補助
    def fmt_id(self, name: str) -> int:
        for fid, n in STANDARD_FORMAT_NAMES.items():
            if n == name:
                return fid
        return self.register_format(name)

    def put(self, text: str | None, *, formats: dict[str, bytes] | None = None, owner_exe: str | None = "C:\\Apps\\editor.exe",
            owner_hwnd: int | None = None) -> None:
        """外部アプリがコピーした状態を作る。owner_exe=None は所有者不明(hwnd なし)。"""
        self.clip = {}
        if text is not None:
            self.clip[CF_UNICODETEXT] = text.encode("utf-16-le") + b"\x00\x00"
        for name, value in (formats or {}).items():
            self.clip[self.fmt_id(name)] = value
        if owner_exe is None:
            self.owner_hwnd = 0
        else:
            hwnd = owner_hwnd or 0x5000 + len(self.windows) + 1
            pid = 4000 + len(self.windows) + 1
            self.windows[hwnd] = pid
            self.exes[pid] = owner_exe
            self.owner_hwnd = hwnd
        self.seq += 1

    def format_names_now(self) -> set[str]:
        return {self.format_name(f) for f in self.clip}

    def text_now(self) -> str | None:
        raw = self.clip.get(CF_UNICODETEXT)
        return raw.decode("utf-16-le").split("\x00", 1)[0] if raw is not None else None

    # ---------------------------------------------------------- Win32Api
    def open_clipboard(self, hwnd: int) -> bool:
        self.opens += 1
        if self.busy_opens > 0:
            self.busy_opens -= 1
            return False
        self.is_open = True
        self._owner_candidate = hwnd
        return True

    def close_clipboard(self) -> None:
        self.is_open = False

    def empty_clipboard(self) -> bool:
        assert self.is_open
        self.clip = {}
        self.owner_hwnd = self._owner_candidate
        self.seq += 1
        return True

    def enum_formats(self) -> list[int]:
        assert self.is_open
        return list(self.clip.keys())

    def format_name(self, fmt: int) -> str:
        if fmt in STANDARD_FORMAT_NAMES:
            return STANDARD_FORMAT_NAMES[fmt]
        for n, fid in self.registered.items():
            if fid == fmt:
                return n
        return f"#{fmt}"

    def register_format(self, name: str) -> int:
        if name not in self.registered:
            self.registered[name] = 0xC000 + len(self.registered) + 1
        return self.registered[name]

    def is_format_available(self, fmt: int) -> bool:
        return fmt in self.clip

    def get_data_bytes(self, fmt: int) -> bytes | None:
        assert self.is_open
        if fmt == CF_UNICODETEXT:
            raise AssertionError("本文形式を get_data_bytes で読んではならない")
        return self.clip.get(fmt)

    def get_unicode_text(self, limit: int) -> str | None:
        assert self.is_open
        self.text_reads += 1
        t = self.text_now()
        return None if t is None else t[: limit + 1]

    def set_data_bytes(self, fmt: int, data: bytes) -> bool:
        assert self.is_open
        self.clip[fmt] = bytes(data)
        return True

    def set_unicode_text(self, text: str) -> bool:
        assert self.is_open
        self.clip[CF_UNICODETEXT] = text.encode("utf-16-le") + b"\x00\x00"
        return True

    def clipboard_owner(self) -> int:
        return self.owner_hwnd

    def sequence_number(self) -> int:
        return self.seq

    def window_pid(self, hwnd: int) -> int:
        return self.windows.get(hwnd, 0)

    def process_image_path(self, pid: int) -> str | None:
        return self.exes.get(pid)

    def current_pid(self) -> int:
        return 1234

    def running_exe_names(self) -> list[str]:
        return sorted(self.running)

    def is_window(self, hwnd: int) -> bool:
        return hwnd in self.windows or hwnd == self.fg_hwnd

    def set_foreground_window(self, hwnd: int) -> bool:
        self.set_fg_calls += 1
        if self.set_fg_ok:
            self.fg_hwnd = hwnd
        return self.set_fg_ok

    def window_rect(self, hwnd: int) -> tuple[int, int, int, int] | None:
        return (0, 0, 1280, 720) if hwnd else None

    def send_ctrl_v(self) -> int:
        self.send_input_calls += 1
        return 4

    def protect(self, data: bytes) -> bytes:
        return FakeCipher().protect(data)

    def unprotect(self, data: bytes) -> bytes:
        return FakeCipher().unprotect(data)
