# 移動先テンプレート(D2)のテスト: 形の検査・展開と無害化・基準フォルダの下だけにフォルダを作ること・
# 試運転では作らないこと・リンクで外へ出ないこと・undo でフォルダを消さないこと・URL を記録しないこと。
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from deskkit.modules.dropsort import selftest as st
from deskkit.modules.dropsort import template as tpl
from deskkit.modules.dropsort.config import load_config, parse_rule

DL = st.DL
BASE = "C:\\Sorted"


def _ymd(t: float) -> tuple[str, str, str]:
    d = dt.datetime.fromtimestamp(t)
    return f"{d.year:04d}", f"{d.month:02d}", f"{d.day:02d}"


@pytest.mark.parametrize(("dest", "ok"), [
    ("D:\\整理\\{yyyy}\\{mm}", True),
    ("D:\\整理\\{ext}\\{yyyy}-{mm}-{dd}", True),
    ("D:\\{domain}", True),
    ("D:\\整理\\img_{YYYY}", True),          # 大文字小文字は問わない
    ("D:\\整理\\{year}", False),              # 知らないプレースホルダ
    ("D:\\整理\\{yyyy", False),               # 括弧の対応
    ("{yyyy}\\x", False),                      # 基準フォルダが無い
    ("整理\\{yyyy}", False),                   # 相対パス
    ("D:\\整理\\{yyyy}\\..\\x", False),        # ..
    ("D:\\整理\\{yyyy}\\a|b", False),          # 使えない文字
    ("D:\\整理\\{yyyy}\\CON", False),          # 予約名
    ("D:\\整理\\{yyyy}\\\\{mm}", False),       # 空の階層
])
def test_validate(dest: str, ok: bool) -> None:
    assert (tpl.validate(dest) is None) == ok, tpl.validate(dest)
    r = parse_rule(0, {"name": "t", "dest": dest, "match": {}})
    assert (r.error is None) == ok
    if ok:
        assert r.is_template and not tpl.has_placeholder(r.base_dest)


def test_split_and_base() -> None:
    assert tpl.split("D:\\整理\\{yyyy}\\{mm}") == ("D:\\整理", "{yyyy}\\{mm}")
    assert tpl.split("D:\\整理\\img_{yyyy}") == ("D:\\整理", "img_{yyyy}")
    assert tpl.split("D:\\{ext}") == ("D:\\", "{ext}")
    assert parse_rule(0, {"name": "p", "dest": "D:\\x", "match": {}}).base_dest == "D:\\x"


def test_expand_and_sanitize() -> None:
    t = st.T0
    y, m, d = _ymd(t)
    v = tpl.TemplateVars(t, "report.PDF", "dl.example.com")
    assert tpl.expand("D:\\S\\{yyyy}\\{mm}\\{dd}", v) == f"D:\\S\\{y}\\{m}\\{d}"
    assert tpl.expand("D:\\S\\{ext}\\{domain}", v) == "D:\\S\\pdf\\dl.example.com"
    none = tpl.TemplateVars(t, "README", None)
    assert tpl.expand("D:\\S\\{ext}\\{domain}", none) == f"D:\\S\\{tpl.NO_EXT}\\{tpl.UNKNOWN_DOMAIN}"
    assert tpl.expand("D:\\S\\{ext}", tpl.TemplateVars(t, "x.con", None)) == "D:\\S\\_con"  # 予約名
    assert tpl.expand("D:\\S\\{domain}", tpl.TemplateVars(t, "x", "..")) == "D:\\S\\_"
    assert tpl.expand("D:\\S\\{domain}", tpl.TemplateVars(t, "x", "a/b\\c:d")) == "D:\\S\\a_b_c_d"  # 区切りを作らない
    assert tpl.expand("D:\\S\\{yyyy}", v, "E:\\Real") == f"E:\\Real\\{y}"
    assert tpl.sanitize_segment("abc. ") == "abc" and tpl.sanitize_segment("nul.txt") == "_nul.txt"
    assert tpl.sanitize_segment("  ") == "_" and len(tpl.sanitize_segment("a" * 500)) == tpl.MAX_SEGMENT
    assert tpl.example("D:\\S\\{ext}\\{domain}", when=t) == "D:\\S\\pdf\\example.com"


def test_apply_creates_only_under_base_and_undo_keeps_folders(tmp_path: Path) -> None:
    fake, svc, clock = st.started(tmp_path, [st.rule("t", BASE + "\\{yyyy}\\{mm}\\{domain}", [".pdf"])])
    fake.mkdirs(BASE)
    arrived = st.T0 - 3 * 86400
    fake.add_file(DL + "\\a.pdf", 10, mtime=clock.t, ctime=arrived, zone=st.ZONE3)
    fake.add_file(DL + "\\b.pdf", 10, mtime=clock.t, ctime=arrived)
    st.settle(svc, clock)
    y, m, _ = _ymd(arrived)
    assert fake.names_in(f"{BASE}\\{y}\\{m}\\dl.example.com") == ["a.pdf"]
    assert fake.names_in(f"{BASE}\\{y}\\{m}\\{tpl.UNKNOWN_DOMAIN}") == ["b.pdf"]
    for p in (svc.oplog.path, svc.dryrun.path, svc.store.path):
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        assert "http" not in text and "SECRET" not in text
    u = svc.undo(2)
    assert len(u.restored) == 2 and fake.names_in(DL) == ["a.pdf", "b.pdf"]
    assert fake.is_dir(f"{BASE}\\{y}\\{m}\\dl.example.com")  # 作ったフォルダは消さない


def test_base_missing_disables_rule_and_creates_nothing(tmp_path: Path) -> None:
    fake, svc, clock = st.started(tmp_path, [st.rule("t", "C:\\NoSuch\\{yyyy}", [".pdf"])])
    fake.add_file(DL + "\\a.pdf", 10, mtime=clock.t)
    st.settle(svc, clock)
    assert svc.rule_checks[0].code == "dest_missing"
    assert fake.get(DL + "\\a.pdf") is not None and not fake.is_dir("C:\\NoSuch")


def test_dry_run_plans_expanded_path_without_creating(tmp_path: Path) -> None:
    fake, svc, clock = st.started(tmp_path, [st.rule("t", BASE + "\\{ext}", [".pdf"], mode="dry-run")])
    fake.mkdirs(BASE)
    fake.add_file(DL + "\\a.pdf", 10, mtime=clock.t)
    st.settle(svc, clock)
    recs = svc.dryrun.all()
    assert [r["op"] for r in recs] == ["would_move"] and recs[0]["dst"] == BASE + "\\pdf\\a.pdf", recs
    assert not fake.is_dir(BASE + "\\pdf")
    ex = svc.sort_existing(False)
    assert not fake.is_dir(BASE + "\\pdf") and ex.items == []


def test_link_inside_base_is_refused(tmp_path: Path) -> None:
    fake, svc, clock = st.started(tmp_path, [st.rule("t", BASE + "\\{ext}\\{yyyy}", [".pdf"])])
    fake.mkdirs(BASE + "\\pdf")
    fake.mkdirs("C:\\Elsewhere")
    fake.links[(BASE + "\\pdf").lower()] = "C:\\Elsewhere"
    fake.add_file(DL + "\\a.pdf", 10, mtime=clock.t)
    st.settle(svc, clock)
    assert fake.get(DL + "\\a.pdf") is not None
    ref = [r for r in svc.oplog.all() if r["op"] == "refused"]
    assert ref and ref[0]["reason"] == "dest_escape", svc.oplog.all()
    assert not any(k.startswith("c:\\elsewhere\\") for k in fake.dirs)


def test_expanded_path_must_not_be_downloads(tmp_path: Path) -> None:
    # 基準フォルダはダウンロードの親、展開先がダウンロードフォルダ自身になる悪い例
    fake, svc, clock = st.started(tmp_path, [st.rule("t", "C:\\Users\\u\\{domain}", [".pdf"])])
    fake.add_file(DL + "\\a.pdf", 10, mtime=clock.t,
                  zone=b"[ZoneTransfer]\r\nZoneId=3\r\nHostUrl=https://dl/x\r\n")
    st.settle(svc, clock)
    assert fake.get(DL + "\\a.pdf") is not None
    ref = [r for r in svc.oplog.all() if r["op"] == "refused"]
    assert ref and ref[0]["reason"] == "dest_is_downloads", svc.oplog.all()


def test_pause_in_modes_config() -> None:
    cfg = load_config({"pause_in_modes": ["game", "game", " focus "]})
    assert cfg.pause_in_modes == ("game", "focus")
    with pytest.raises(Exception):  # noqa: B017 - ConfigError
        load_config({"pause_in_modes": "game"})
