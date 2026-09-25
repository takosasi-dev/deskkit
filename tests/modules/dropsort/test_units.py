# DropSort の部品単位のテスト(危険名・MOTW・ルール・設定・連番・ログ・ロック・状態)。
from __future__ import annotations

from pathlib import Path

import pytest

from deskkit.modules.dropsort import guard, motw
from deskkit.modules.dropsort.config import ConfigError, fill_defaults, load_config, parse_rule
from deskkit.modules.dropsort.mover import suffixed
from deskkit.modules.dropsort.oplog import JsonlLog, OpLock
from deskkit.modules.dropsort.rules import first_match, fmt_size_exact, matches, parse_size
from deskkit.modules.dropsort.state import StateStore

EXEC = frozenset({".exe", ".scr", ".js", ".lnk"})


@pytest.mark.parametrize(("name", "want"), [
    ("report.pdf.exe", "double_extension"),
    ("invoice.docx.scr", "double_extension"),
    ("photo\u202egpj.exe", "bidi_control_char"),
    ("a\u2066b.txt", "bidi_control_char"),
    ("a.pdf     .exe", "spaced_extension"),
    ("archive.tar.gz", None),
    ("report.v2.pdf", None),
    ("setup.exe", None),
    ("python-3.13.2-amd64.exe", None),
    ("node-v20.1.0.msi", None),
    ("README", None),
])
def test_guard(name: str, want: str | None) -> None:
    assert guard.check_name(name, EXEC) == want


def test_guard_reparse() -> None:
    assert guard.check("a.pdf", 0x400, EXEC) == "reparse_point"


def test_motw_parse_and_domain() -> None:
    m = motw.parse(b"[ZoneTransfer]\r\nZoneId=3\r\nHostUrl=https://user:pw@Files.Example.COM:8443/a?token=x\r\n")
    assert (m.present, m.zone_id, m.domain) == (True, 3, "files.example.com")
    assert "token" not in repr(m) and "http" not in repr(m)
    u16 = "[ZoneTransfer]\r\nZoneId=4\r\n".encode("utf-16")
    assert motw.parse(u16).zone_id == 4
    bad = motw.parse(b"\x00\x01garbage")
    assert bad.present and bad.zone_id is None
    unreadable = motw.parse(None)
    assert unreadable.present and unreadable.zone_id is None
    assert motw.domain_of("about:internet") is None


def test_rules_match_and_first() -> None:
    r0 = parse_rule(0, {"name": "a", "dest": "C:\\x", "match": {"ext": [".PDF"], "zone_ids": [3]}})
    r1 = parse_rule(1, {"name": "b", "dest": "C:\\x", "match": {"size_min": 10, "size_max": 20, "motw": False}})
    r2 = parse_rule(2, {"name": "c", "dest": "C:\\x", "match": {"name_regex": "^inv", "host_domain": "example.com"}})
    net = motw.Motw(True, 3, "dl.example.com")
    assert matches(r0, "x.pdf", 1, net)
    assert not matches(r0, "x.pdf", 1, motw.Motw(True, None))  # ZoneId 不明は zone_ids に一致しない
    assert matches(r1, "x.bin", 15, motw.NONE) and not matches(r1, "x.bin", 21, motw.NONE)
    assert matches(r2, "invoice.pdf", 1, net)
    assert not matches(r2, "invoice.pdf", 1, motw.Motw(True, 3, "badexample.com"))
    assert first_match([r0, r1, r2], "x.pdf", 15, net, set()).name == "a"  # type: ignore[union-attr]
    assert first_match([r0, r1, r2], "x.pdf", 15, net, {0}) is None


def test_rule_shape_errors_disable_only_that_rule() -> None:
    cfg = load_config({"rules": [{"name": "bad", "dest": "C:\\x", "match": {"name_regex": "("}},
                                 {"name": "ok", "dest": "C:\\x", "match": {}}]})
    assert cfg.rules[0].error and cfg.rules[1].error is None
    assert cfg.rules[1].mode == "dry-run"  # FR-8: 未指定は dry-run


def test_config_defaults_and_errors() -> None:
    sec, changed = fill_defaults({})
    assert changed and sec["stable_seconds"] == 5 and sec["watch_mode"] == "rdcw" and sec["rules"] == []
    assert ".crdownload" in sec["temp_extensions"] and sec["archive"]["mode"] == "dry-run"
    with pytest.raises(ConfigError):
        load_config({"watch_mode": "inotify"})
    with pytest.raises(ConfigError):
        load_config({"stable_seconds": "5"})
    with pytest.raises(ConfigError):
        load_config({"archive": {"dir_name": "a\\b"}})


def test_suffix_and_sizes() -> None:
    assert suffixed("a.tar.gz", 1) == "a.tar (1).gz"
    assert suffixed("README", 2) == "README (2)"
    assert suffixed("a.pdf", 0) == "a.pdf"
    assert parse_size("10MB") == 10 * 1024 * 1024 and parse_size("") is None and parse_size("2048") == 2048
    assert parse_size(fmt_size_exact(1500000) or "") == 1500000
    with pytest.raises(ValueError):
        parse_size("abc")


def test_oplog_ids_and_no_url(tmp_path: Path) -> None:
    log = JsonlLog(tmp_path / "oplog.jsonl")
    a = log.append({"op": "move", "src": "C:\\a", "dst": "C:\\b"}, 1_800_000_000)
    b = log.append({"op": "move", "src": "C:\\a", "dst": "https://evil/x"}, 1_800_000_000)
    assert a["id"].endswith("-0001") and b["id"].endswith("-0002")
    assert b["dst"] is None
    again = JsonlLog(tmp_path / "oplog.jsonl").append({"op": "undo"}, 1_800_000_000)
    assert again["id"].endswith("-0003")
    assert "http" not in (tmp_path / "oplog.jsonl").read_text(encoding="utf-8")


def test_oplock_excludes_second_holder(tmp_path: Path) -> None:
    a = OpLock(tmp_path / "op.lock")
    b = OpLock(tmp_path / "op.lock")
    assert a.try_acquire()
    assert not b.try_acquire()
    a.release()
    assert b.try_acquire()
    b.release()


def test_state_roundtrip(tmp_path: Path) -> None:
    st = StateStore(tmp_path / "state.json")
    s = st.load()
    ds = s.new_dir("C:\\DL")
    ds.baseline["a.pdf"] = {"name": "a.pdf", "size": 1, "mtime": 2.0}
    ds.set_handled("b.pdf", 3, 4.0, "flagged", reason="double_extension")
    s.undone.append("x")
    st.save(s)
    s2 = st.load()
    d2 = s2.dir("c:\\dl")
    assert d2 is not None and d2.in_baseline("A.PDF", 1, 2.0) and not d2.in_baseline("a.pdf", 1, 3.0)
    assert d2.handled_for("b.pdf", 3, 4.0)["reason"] == "double_extension"  # type: ignore[index]
    assert d2.handled_for("b.pdf", 5, 4.0) is None  # 中身が変われば判定し直す
    assert s2.undone == ["x"]
