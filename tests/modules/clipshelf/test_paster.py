# AC-15: 偽の foreground 情報で game_processes 該当・全画面・昇格・昇格不明・フォーカス変化の各ケースで
# SendInput が 0 回、ops.jsonl に対応する skipped_* が出ること。dry_run と送信可の場合も確認する。
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from deskkit.foreground import ForegroundInfo
from deskkit.modules.clipshelf.fakes import FakeWin32
from deskkit.modules.clipshelf.ops import OpsLog
from deskkit.modules.clipshelf.paster import Paster

TARGET = 0x7001
OK_FG = ForegroundInfo(TARGET, 10, "notepad.exe", False, False, False)


def _paster(tmp_path: Path, fg: ForegroundInfo) -> tuple[Paster, FakeWin32, OpsLog]:
    api = FakeWin32()
    ops = OpsLog(tmp_path / "ops.jsonl")
    return Paster(api, lambda: fg, ops, logging.getLogger("deskkit.clipshelf.test.paster")), api, ops


@pytest.mark.parametrize(
    ("fg", "expected"),
    [
        (ForegroundInfo(TARGET, 10, "game.exe", True, False, False), "skipped_game"),
        (ForegroundInfo(TARGET, 10, "player.exe", False, True, False), "skipped_fullscreen"),
        (ForegroundInfo(TARGET, 10, "player.exe", False, None, False), "skipped_fullscreen"),
        (ForegroundInfo(TARGET, 10, "admin.exe", False, False, True), "skipped_elevated"),
        (ForegroundInfo(TARGET, 10, "admin.exe", False, False, None), "skipped_elevated"),
        (ForegroundInfo(0x9999, 11, "other.exe", False, False, False), "skipped_focus_changed"),
        (ForegroundInfo(None, None, None, False, False, False), "skipped_focus_changed"),
        (ForegroundInfo(TARGET, 10, "windowsterminal.exe", False, False, False), "skipped_denied"),
    ],
)
def test_auto_paste_skips(tmp_path: Path, fg: ForegroundInfo, expected: str) -> None:
    p, api, ops = _paster(tmp_path, fg)
    assert p.auto_paste("on", TARGET, frozenset({"windowsterminal.exe"})) == expected
    assert api.send_input_calls == 0
    assert ops.read()[-1]["result"] == expected and ops.read()[-1]["op"] == "auto_paste"


def test_auto_paste_sends_when_safe(tmp_path: Path) -> None:
    p, api, ops = _paster(tmp_path, OK_FG)
    assert p.auto_paste("on", TARGET, frozenset()) == "sent"
    assert api.send_input_calls == 1
    assert ops.read()[-1]["result"] == "sent"


def test_dry_run_never_sends(tmp_path: Path) -> None:
    p, api, ops = _paster(tmp_path, OK_FG)
    assert p.auto_paste("dry_run", TARGET, frozenset()) == "dry_run"
    p2, api2, _ = _paster(tmp_path, ForegroundInfo(TARGET, 10, "game.exe", True, False, False))
    p2.auto_paste("dry_run", TARGET, frozenset())
    assert api.send_input_calls == 0 and api2.send_input_calls == 0
    recs = ops.read()
    assert recs[0] == {"ts": recs[0]["ts"], "op": "auto_paste", "result": "dry_run", "would": "sent"}
    assert recs[1]["would"] == "skipped_game"


def test_off_does_nothing(tmp_path: Path) -> None:
    p, api, ops = _paster(tmp_path, OK_FG)
    assert p.auto_paste("off", TARGET, frozenset()) is None
    assert api.send_input_calls == 0 and ops.read() == []


def test_restore(tmp_path: Path) -> None:
    p, api, _ = _paster(tmp_path, OK_FG)
    assert not p.restore(0)
    assert not p.restore(0x1234)  # もう無いウィンドウ
    api.windows[0x1234] = 1
    assert p.restore(0x1234) and api.fg_hwnd == 0x1234
