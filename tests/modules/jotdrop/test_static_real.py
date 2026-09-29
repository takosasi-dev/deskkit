# 禁止 API が無いこと(AC-17・AC-18・V4AC-3・V4AC-4)と、@win32_real: 本物の CreateFileW で一時フォルダのファイルに書く
# (AC-1 の実機版・AC-4・AC-12 の実機版)。本物のファイルに書くのはこのファイルのテストだけで、場所は pytest の一時フォルダ。
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from deskkit.modules.jotdrop import target, writer

SRC = Path(__file__).resolve().parents[3] / "deskkit" / "modules" / "jotdrop"
WHEN = datetime(2026, 9, 26, 14, 5)


def _grep(pattern: str) -> list[str]:
    rx = re.compile(pattern)
    return [f"{p.name}:{i}: {line.strip()}" for p in SRC.rglob("*.py")
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if rx.search(line)]


def test_ac17_no_hooks_input_or_network() -> None:
    assert _grep(r"SetWindowsHookEx|GetAsyncKeyState|SendInput|keybd_event|AttachThreadInput|LockSetForegroundWindow|"
                 r"SystemParametersInfo|pynput|import keyboard|import socket|urllib|http\.client|requests|QNetwork|"
                 r"obsidian://") == []
    # V4AC-4: ホットキーは ctx.hotkeys だけ
    assert _grep(r"GetKeyState|RegisterRawInputDevices|RegisterHotKey|UnregisterHotKey") == []


def test_ac18_no_delete() -> None:
    assert _grep(r"os\.remove|os\.unlink|\.unlink\(|shutil\.rmtree|send2trash") == []


def test_inv1_append_handle_constant() -> None:
    from deskkit.modules.jotdrop import _win32

    assert _win32.APPEND_ACCESS & _win32.FILE_APPEND_DATA
    assert not _win32.APPEND_ACCESS & (_win32.FILE_WRITE_DATA | _win32.GENERIC_WRITE)
    # 追記の CreateFileW に渡すのは APPEND_ACCESS だけ(writer の中で権限を組み立てない)
    src = (SRC / "writer.py").read_text(encoding="utf-8")
    assert src.count("TRUNCATE_ACCESS") == 2  # import と取り消しの1か所だけ


# ------------------------------------------------------------------ 実機
@pytest.fixture
def real() -> Any:
    from deskkit.modules.jotdrop._win32 import RealWin32

    return RealWin32()


def _job(path: Path, text: str = "メモ", jid: str = "j") -> writer.Job:
    return writer.Job(jid, WHEN, str(path), f"- 14:05 {text}")


@pytest.mark.win32_real
def test_real_ac1_append_keeps_prefix(real: Any, tmp_path: Path) -> None:
    p = tmp_path / "a.md"
    old = b"one\ntwo\r\nlast"          # 最後に現れる改行は CRLF
    p.write_bytes(old)
    res = writer.append_line(real, _job(p), writer.Framing())
    data = p.read_bytes()
    assert res.ok and hashlib.sha256(data[:len(old)]).digest() == hashlib.sha256(old).digest()
    assert data[len(old):] == "\r\n- 14:05 メモ\r\n".encode()
    assert res.record is not None and res.record.after == len(data)
    # 新しいファイル
    n = tmp_path / "new.md"
    assert writer.append_line(real, _job(n), writer.Framing(header="# {date}")).created_file
    assert n.read_bytes() == "# 2026-09-26\n- 14:05 メモ\n".encode()


@pytest.mark.win32_real
def test_real_ac12_undo(real: Any, tmp_path: Path) -> None:
    p = tmp_path / "u.md"
    p.write_bytes(b"keep\n")
    before = hashlib.sha256(p.read_bytes()).digest()
    res = writer.append_line(real, _job(p), writer.Framing())
    assert res.record is not None
    assert writer.undo(real, res.record) == writer.UNDO_OK
    assert hashlib.sha256(p.read_bytes()).digest() == before
    res2 = writer.append_line(real, _job(p), writer.Framing())
    assert res2.record is not None
    with p.open("ab") as f:
        f.write(b"later\n")
    snap = p.read_bytes()
    assert writer.undo(real, res2.record) == writer.UNDO_CHANGED and p.read_bytes() == snap


@pytest.mark.win32_real
def test_real_readers_do_not_block_but_exclusive_does(real: Any, tmp_path: Path) -> None:
    from deskkit.modules.jotdrop._win32 import FILE_READ_DATA, FILE_SHARE_NONE, OPEN_EXISTING, READ_SHARE

    p = tmp_path / "s.md"
    p.write_bytes(b"x\n")
    # 書き込みも共有して読んでいるアプリ(照合と同じ開き方)があっても書ける
    h = real.create_file(str(p), FILE_READ_DATA, READ_SHARE, OPEN_EXISTING)
    try:
        assert writer.append_line(real, _job(p), writer.Framing(), sleep=lambda _s: False).ok
    finally:
        real.close(h)
    # 読み取りだけを共有して開いているアプリ・共有なしで開いているアプリがいる間は使用中(J-9)
    for share in (1, FILE_SHARE_NONE):
        h2 = real.create_file(str(p), FILE_READ_DATA, share, OPEN_EXISTING)
        try:
            res = writer.append_line(real, _job(p, "c"), writer.Framing(), sleep=lambda _s: False)
            assert res.reason == target.REASON_BUSY and res.retries == 4
        finally:
            real.close(h2)
    assert b"- 14:05 c" not in p.read_bytes()


@pytest.mark.win32_real
def test_real_ac4_busy_then_retry_order(real: Any, tmp_path: Path, qapp: Any) -> None:
    from deskkit.modules.jotdrop._win32 import FILE_READ_DATA, FILE_SHARE_NONE, OPEN_EXISTING
    from deskkit.modules.jotdrop.module import JotDropModule

    from .conftest import Clock, FakeCtx

    folder = tmp_path / "notes"
    folder.mkdir()
    f = folder / "2026-09-26.md"
    f.write_bytes(b"# log\n")
    ctx = FakeCtx(tmp_path / "data", {"folder": str(folder), "target_confirmed": True})
    clock = Clock(WHEN)
    m = JotDropModule(ctx, api=real, threaded=False, clock=clock.now, mono=clock.monotonic, documents=lambda: str(tmp_path),
                      startfile=lambda _p: None, make_popup=False)
    m.start()
    h = real.create_file(str(f), FILE_READ_DATA, FILE_SHARE_NONE, OPEN_EXISTING)   # 別のハンドルが共有なしで開いている
    try:
        m.on_submit("先のメモ")
        assert m.pending.items()[0].reason == target.REASON_BUSY
        clock.advance(60)
        m.on_submit("あとのメモ")
        assert m.pending.count() == 2
    finally:
        real.close(h)
    assert f.read_bytes() == b"# log\n"
    assert m.retry_pending("timer")
    assert f.read_bytes() == "# log\n- 14:05 先のメモ\n- 14:06 あとのメモ\n".encode()
    assert m.pending.count() == 0
    m.stop()


@pytest.mark.win32_real
def test_real_network_and_windows(real: Any) -> None:
    assert real.drive_type("C:\\") != 4
    assert real.is_window(real.foreground_window()) in (True, False)
    assert target.check("\\\\server\\share\\a.md", real) == target.REASON_NETWORK
