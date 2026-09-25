# 実機の Win32 に触れるテスト。クリップボードに触れるものは @pytest.mark.win32_real(既定では実行しない)。
# 補助関数 put_real は除外形式付き・所有者なしの内容を置く(AC-5 / AC-6 / AC-22 の実機確認用)。終了時に元のテキストへ戻す。
# 印の無いテストはクリップボード・入力に触れない(DPAPI と構造体の大きさ、読み取り専用の API だけ)。
from __future__ import annotations

import ctypes
import logging
import secrets
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from deskkit.modules.clipshelf import _win32
from deskkit.modules.clipshelf._win32 import CF_UNICODETEXT, FMT_CAN_INCLUDE_HISTORY, FMT_EXCLUDE_MONITOR, RealWin32


# ------------------------------------------------------------------ クリップボードに触れない実機テスト
def test_dpapi_roundtrip_and_no_plaintext() -> None:
    api = RealWin32()
    data = ("CANARY-" + secrets.token_hex(8)).encode("utf-8")
    blob = api.protect(data)
    assert data not in blob
    assert api.unprotect(blob) == data
    with pytest.raises(_win32.CryptoError):
        api.unprotect(b"not a dpapi blob")


def test_struct_sizes_match_sdk() -> None:
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(_win32._INPUT) == 40
        assert ctypes.sizeof(_win32._PROCESSENTRY32W) == 568
    assert ctypes.sizeof(_win32._KEYBDINPUT) in (16, 24)


def test_read_only_apis() -> None:
    api = RealWin32()
    assert isinstance(api.sequence_number(), int)
    names = api.running_exe_names()
    assert names and all(n == n.lower() for n in names)
    assert api.current_pid() > 0
    assert api.format_name(CF_UNICODETEXT) == "CF_UNICODETEXT"


# ------------------------------------------------------------------ 実機のクリップボード(既定では実行しない)
def put_real(api: RealWin32, text: str, formats: dict[str, bytes] | None = None) -> None:
    """所有者なし(OpenClipboard(NULL))で text と任意の登録形式を置く。"""
    for _ in range(20):
        if api.open_clipboard(0):
            break
    else:
        raise RuntimeError("OpenClipboard failed")
    try:
        api.empty_clipboard()
        api.set_unicode_text(text)
        for name, value in (formats or {}).items():
            api.set_data_bytes(api.register_format(name), value)
    finally:
        api.close_clipboard()


@pytest.fixture
def real_clipboard() -> Iterator[RealWin32]:
    api = RealWin32()
    prev: str | None = None
    if api.open_clipboard(0):
        try:
            prev = api.get_unicode_text(10_000_000) if api.is_format_available(CF_UNICODETEXT) else None
        finally:
            api.close_clipboard()
    yield api
    if api.open_clipboard(0):  # 元のテキストへ戻す(テキスト以外の形式は戻せない)
        try:
            api.empty_clipboard()
            if prev is not None:
                api.set_unicode_text(prev)
        finally:
            api.close_clipboard()


def _monitor(api: RealWin32, tmp_path: Path, **over: Any) -> Any:
    from deskkit.modules.clipshelf import config as cfgmod
    from deskkit.modules.clipshelf.fakes import FakeCipher
    from deskkit.modules.clipshelf.monitor import ClipMonitor
    from deskkit.modules.clipshelf.ops import OpsLog
    from deskkit.modules.clipshelf.store import Store

    sec, _ = cfgmod.merge_defaults({"mode": "record", "unknown_owner_policy": "record", **over})
    cfg = cfgmod.parse(sec)
    store = Store(tmp_path / "c.db", FakeCipher())
    store.open()
    mon = ClipMonitor(api, owner_hwnd=lambda: 0, config=lambda: cfg, store=lambda: store, paused=lambda: False,
                      log=logging.getLogger("deskkit.clipshelf.test.real"), ops=OpsLog(tmp_path / "ops.jsonl"),
                      now=lambda: datetime.now().astimezone())
    mon.mark_startup()
    return mon, store


@pytest.mark.win32_real
@pytest.mark.parametrize(("fmt", "reason"), [(FMT_EXCLUDE_MONITOR, "exclude_format"), (FMT_CAN_INCLUDE_HISTORY, "history_disallowed")])
def test_real_exclusion_formats(real_clipboard: RealWin32, tmp_path: Path, fmt: str, reason: str) -> None:
    mon, store = _monitor(real_clipboard, tmp_path)
    put_real(real_clipboard, "CLIPSHELF-CANARY-" + secrets.token_hex(6), {fmt: b"\x00\x00\x00\x00"})
    d = mon.process()
    assert d is not None and d.reason == reason and store.row_count() == 0
    store.close()


@pytest.mark.win32_real
def test_real_startup_content_not_recorded(real_clipboard: RealWin32, tmp_path: Path) -> None:
    put_real(real_clipboard, "CLIPSHELF-CANARY-" + secrets.token_hex(6))
    mon, store = _monitor(real_clipboard, tmp_path)
    assert mon.process() is None and store.row_count() == 0  # D-15 / AC-22
    store.close()
