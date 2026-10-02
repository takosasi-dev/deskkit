# v0.4.1 B-1: DropSort のログ(host のログファイルに出る行)に、パス・ファイル名・ルール名を書かないこと。件数と理由コードだけ。
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from deskkit.modules.dropsort import selftest as st
from deskkit.modules.dropsort import service as service_mod
from deskkit.modules.dropsort.config import fill_defaults, load_config
from deskkit.modules.dropsort.fakewin32 import FakeWin32
from deskkit.modules.dropsort.service import DropSortService, err_text

DL = st.DL
BASE = "C:\\Sorted"
SECRET_RULE = "ひみつのルール名"


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _logger(name: str) -> tuple[logging.Logger, _ListHandler]:
    log = logging.getLogger(f"deskkit.dropsort.test_v041.{name}")
    log.setLevel(logging.DEBUG)
    log.propagate = False
    h = _ListHandler()
    log.handlers = [h]
    return log, h


def _assert_clean(lines: list[str], *words: str) -> None:
    assert lines, "ログが1行も出ていない(検査になっていない)"
    for line in lines:
        for w in ("C:\\", "\\\\", "Downloads", ".pdf", SECRET_RULE, *words):
            assert w not in line, (w, line)


def _svc(tmp: Path, fake: FakeWin32, rules: list[dict[str, object]], log: logging.Logger) -> tuple[DropSortService, st.FakeClock]:
    sec, _ = fill_defaults({"rules": rules})
    clock = st.FakeClock()
    return DropSortService(fake, tmp, load_config(sec), clock=clock, log=log), clock


def test_baseline_move_and_template_logs_have_no_paths(tmp_path: Path) -> None:
    log, h = _logger("move")
    fake = FakeWin32(DL)
    fake.mkdirs(BASE)
    fake.mkdirs("C:\\Docs\\PDF")
    fake.add_file(DL + "\\old-secret.pdf", 10, mtime=st.T0 - 1000)
    svc, clock = _svc(tmp_path, fake, [st.rule(SECRET_RULE, BASE + "\\{yyyy}\\{mm}", [".pdf"])], log)
    svc.run_cycle()  # 基準線(1 件)
    assert any(line == "基準線を記録しました: 1 件" for line in h.lines), h.lines
    fake.add_file(DL + "\\new-secret.pdf", 10, mtime=clock.t)
    st.settle(svc, clock)
    assert "move" in st.ops(svc)
    assert any("移動先のフォルダを作成しました" in line for line in h.lines), h.lines
    assert any(line == "move: 1 件" for line in h.lines), h.lines
    _assert_clean(h.lines, "secret", "Sorted", "Docs")


def test_refused_log_has_reason_only(tmp_path: Path) -> None:
    log, h = _logger("refused")
    fake = FakeWin32(DL)
    fake.mkdirs("C:\\Docs\\PDF")
    svc, clock = _svc(tmp_path, fake, [st.rule(SECRET_RULE, "C:\\Docs\\PDF", [".pdf"])], log)
    svc.run_cycle()
    # 移動先に同じ名前が連番の上限まである → refused(suffix_exhausted)
    fake.add_file("C:\\Docs\\PDF\\x-secret.pdf", 1, mtime=clock.t)
    for n in range(1, svc.cfg.max_suffix + 1):
        fake.add_file(f"C:\\Docs\\PDF\\x-secret ({n}).pdf", 1, mtime=clock.t)
    fake.add_file(DL + "\\x-secret.pdf", 10, mtime=clock.t)
    st.settle(svc, clock)
    assert "refused" in st.ops(svc)
    assert any(line == "refused: 1 件 (suffix_exhausted)" for line in h.lines), h.lines
    _assert_clean(h.lines, "secret", "Docs")


def test_scan_error_logs_type_and_code_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log, h = _logger("scan")
    fake = FakeWin32(DL)
    svc, _clock = _svc(tmp_path, fake, [], log)

    def boom(_api: object, path: str) -> object:
        raise FileNotFoundError(2, "見つかりません", path)

    monkeypatch.setattr(service_mod, "scan_dir", boom)
    r = svc.run_cycle()
    assert not r.ok
    assert h.lines == ["ダウンロードフォルダを列挙できません: FileNotFoundError(errno=2)"], h.lines


def test_err_text_drops_message_and_filename() -> None:
    e = OSError(5, "アクセス拒否", "C:\\Users\\someone\\secret.txt")
    e.winerror = 5  # type: ignore[attr-defined]
    assert err_text(e) == "OSError(errno=5 winerror=5)"
    assert err_text(ValueError("C:\\secret")) == "ValueError"
