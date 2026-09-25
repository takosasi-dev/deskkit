# --selftest(FR-24): 一時フォルダ(データ置き場)と偽の Win32 層(メモリ上のファイル)で、完了判定・ルール評価・
# 連番・危険名・MOTW の移動拒否・別ボリューム移動の検証・undo・アーカイブ・URL を記録しないことを検査する。
# 実ファイル・実際のダウンロードフォルダ・%LOCALAPPDATA%\DeskKit には触らない。戻り値 0 = 合格 / 1 = 不合格。
from __future__ import annotations

import logging
import tempfile
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from deskkit.modules.dropsort import _win32 as W
from deskkit.modules.dropsort.config import fill_defaults, load_config
from deskkit.modules.dropsort.fakewin32 import FakeWin32
from deskkit.modules.dropsort.service import DropSortService

DL = "C:\\Users\\u\\DL"
ZONE3 = b"[ZoneTransfer]\r\nZoneId=3\r\nReferrerUrl=https://example.com/page?t=SECRET\r\nHostUrl=https://dl.example.com/f.pdf?token=SECRET\r\n"
T0 = 1_800_000_000.0
_QUIET = logging.getLogger("deskkit.dropsort.selftest")
_QUIET.addHandler(logging.NullHandler())
_QUIET.propagate = False


class FakeClock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def rule(name: str, dest: str, exts: list[str] | None = None, mode: str = "apply", **match: Any) -> dict[str, Any]:
    m: dict[str, Any] = {"ext": exts}
    m.update(match)
    return {"name": name, "mode": mode, "match": m, "dest": dest}


def make(tmp: Path, fake: FakeWin32, rules: list[dict[str, Any]], **over: Any) -> tuple[DropSortService, FakeClock]:
    sec, _ = fill_defaults({"rules": rules, **over})
    clock = FakeClock()
    return DropSortService(fake, tmp, load_config(sec), clock=clock, log=_QUIET), clock


def settle(svc: DropSortService, clock: FakeClock, rounds: int = 2) -> None:
    """完了判定の安定待ちを越えるまで、時計を進めてスキャンする。"""
    for _ in range(rounds):
        svc.run_cycle()
        clock.advance(svc.cfg.stable_seconds * 2 + 1)
    svc.run_cycle()


def started(tmp: Path, rules: list[dict[str, Any]], **over: Any) -> tuple[FakeWin32, DropSortService, FakeClock]:
    fake = FakeWin32(DL)
    fake.mkdirs("C:\\Docs\\PDF")
    svc, clock = make(tmp, fake, rules, **over)
    svc.run_cycle()  # 基準線(空)
    return fake, svc, clock


def ops(svc: DropSortService, log: str = "oplog") -> list[str]:
    j = svc.oplog if log == "oplog" else svc.dryrun
    return [str(r.get("op")) for r in j.all()]


# ------------------------------------------------------------------ 検査
def t_baseline(tmp: Path) -> None:
    fake = FakeWin32(DL)
    fake.mkdirs("C:\\Docs\\PDF")
    fake.add_file(DL + "\\old.pdf", 10, mtime=T0 - 1000)
    svc, clock = make(tmp, fake, [rule("pdf", "C:\\Docs\\PDF", [".pdf"])])
    r = svc.run_cycle()
    assert r.baseline_created == 1, r
    settle(svc, clock)
    assert fake.get(DL + "\\old.pdf") is not None, "基準線のファイルが動いた"
    assert "move" not in ops(svc), "基準線で move が記録された"
    fake.add_file(DL + "\\new.pdf", 20, mtime=clock.t)
    settle(svc, clock)
    assert fake.names_in("C:\\Docs\\PDF") == ["new.pdf"], fake.names_in("C:\\Docs\\PDF")
    ex = svc.sort_existing(False)
    assert [i["op"] for i in ex.items] == ["would_move"], ex.items
    assert fake.get(DL + "\\old.pdf") is not None
    ex = svc.sort_existing(True)
    assert fake.names_in("C:\\Docs\\PDF") == ["new.pdf", "old.pdf"]


def t_completion(tmp: Path) -> None:
    fake, svc, clock = started(tmp, [rule("all", "C:\\Docs\\PDF", None)], stable_seconds=5, locked_retry_max=3)
    fake.add_file(DL + "\\big.iso.crdownload", 100, mtime=clock.t)
    fake.add_file(DL + "\\a.pdf", 50, mtime=clock.t)
    fake.add_file(DL + "\\zero.txt", 0, mtime=clock.t)
    r = svc.run_cycle()
    assert r.pending == 3 and fake.names_in("C:\\Docs\\PDF") == [], r
    clock.advance(6)
    svc.run_cycle()
    assert fake.names_in("C:\\Docs\\PDF") == ["a.pdf"], fake.names_in("C:\\Docs\\PDF")  # 0 バイトは2倍待つ
    clock.advance(6)
    svc.run_cycle()
    assert "zero.txt" in fake.names_in("C:\\Docs\\PDF")
    assert fake.get(DL + "\\big.iso.crdownload") is not None, "一時ファイルが動いた"
    # 書き込み中(サイズが変わり続ける)なら動かない
    f = fake.add_file(DL + "\\grow.bin", 10, mtime=clock.t)
    for _ in range(4):
        svc.run_cycle()
        clock.advance(3)
        f.size += 10
        f.mtime = clock.t
    assert fake.get(DL + "\\grow.bin") is not None
    # 排他オープンに失敗し続けたら要確認
    fake.add_file(DL + "\\held.pdf", 10, mtime=clock.t, locked=True)
    for _ in range(5):
        svc.run_cycle()
        clock.advance(6)
    assert fake.get(DL + "\\held.pdf") is not None
    assert any(r.get("op") == "flagged" and r.get("reason") == "locked" for r in svc.oplog.all())


def t_rules(tmp: Path) -> None:
    fake, svc, clock = started(tmp, [
        rule("inet-pdf", "C:\\Docs\\PDF", [".PDF"], mode="dry-run", zone_ids=[3]),
        rule("big", "C:\\Docs\\PDF", None, mode="dry-run", size_min=1000),
        rule("rx", "C:\\Docs\\PDF", None, mode="dry-run", name_regex="^inv_"),
    ])
    fake.add_file(DL + "\\x.pdf", 10, mtime=clock.t, zone=ZONE3)
    fake.add_file(DL + "\\y.pdf", 5000, mtime=clock.t)
    fake.add_file(DL + "\\inv_1.txt", 5, mtime=clock.t)
    fake.add_file(DL + "\\other.txt", 5, mtime=clock.t)
    settle(svc, clock)
    dr = {r["src"].rsplit("\\", 1)[1]: r for r in svc.dryrun.all()}
    assert dr["x.pdf"]["rule"] == "inet-pdf" and dr["x.pdf"]["op"] == "would_move", dr
    assert dr["y.pdf"]["rule"] == "big"
    assert dr["inv_1.txt"]["rule"] == "rx"
    assert "other.txt" not in dr
    assert len(svc.dryrun.all()) == 3, "would_move が重複して記録された"
    assert fake.names_in("C:\\Docs\\PDF") == [], "dry-run で動いた"


def t_suffix(tmp: Path) -> None:
    fake, svc, clock = started(tmp, [rule("pdf", "C:\\Docs\\PDF", [".pdf", ".gz"])])
    e1 = fake.add_file("C:\\Docs\\PDF\\a.pdf", 1, mtime=T0 - 50)
    e2 = fake.add_file("C:\\Docs\\PDF\\a (1).pdf", 2, mtime=T0 - 40)
    fake.add_file(DL + "\\a.pdf", 3, mtime=clock.t)
    fake.add_file(DL + "\\b.tar.gz", 3, mtime=clock.t)
    fake.add_file("C:\\Docs\\PDF\\b.tar.gz", 3, mtime=clock.t)
    settle(svc, clock)
    names = fake.names_in("C:\\Docs\\PDF")
    assert "a (2).pdf" in names and "b.tar (1).gz" in names, names
    assert (e1.size, e1.mtime, e2.size, e2.mtime) == (1, T0 - 50, 2, T0 - 40), "既存ファイルが変わった"


def t_guard(tmp: Path) -> None:
    fake, svc, clock = started(tmp, [rule("all", "C:\\Docs\\PDF", None)])
    fake.add_file(DL + "\\report.pdf.exe", 10, mtime=clock.t)
    fake.add_file(DL + "\\photo\u202egpj.exe", 10, mtime=clock.t)
    fake.add_file(DL + "\\a.pdf     .exe", 10, mtime=clock.t)
    fake.add_file(DL + "\\archive.tar.gz", 10, mtime=clock.t)
    fake.add_file(DL + "\\setup.exe", 10, mtime=clock.t)
    fake.add_file(DL + "\\link.pdf", 10, mtime=clock.t, attrs=W.FILE_ATTRIBUTE_REPARSE_POINT)
    settle(svc, clock)
    flagged = {r["src"].rsplit("\\", 1)[1]: r["reason"] for r in svc.oplog.all() if r["op"] == "flagged"}
    assert flagged == {"report.pdf.exe": "double_extension", "photo\u202egpj.exe": "bidi_control_char",
                       "a.pdf     .exe": "spaced_extension", "link.pdf": "reparse_point"}, flagged
    assert sorted(fake.names_in("C:\\Docs\\PDF")) == ["archive.tar.gz", "setup.exe"]
    n = len(svc.oplog.all())
    settle(svc, clock)
    assert len(svc.oplog.all()) == n, "要確認が繰り返し記録された"


def t_motw_refuse(tmp: Path) -> None:
    fake = FakeWin32(DL)
    fake.add_volume("L:\\", "exFAT", serial=77, drive_type=W.DRIVE_REMOVABLE)
    fake.mkdirs("L:\\Sorted")
    svc, clock = make(tmp, fake, [rule("ex", "L:\\Sorted", [".pdf", ".txt"])])
    svc.run_cycle()
    fake.add_file(DL + "\\m.pdf", 10, mtime=clock.t, zone=ZONE3)
    fake.add_file(DL + "\\plain.txt", 10, mtime=clock.t)
    settle(svc, clock)
    assert fake.get(DL + "\\m.pdf") is not None, "MOTW 付きが exFAT へ動いた"
    ref = [r for r in svc.oplog.all() if r["op"] == "refused"]
    assert len(ref) == 1 and ref[0]["reason"] == "motw_unsupported_fs" and ref[0]["dst_fs"] == "exFAT", ref
    assert fake.names_in("L:\\Sorted") == ["plain.txt"], "MOTW なしは exFAT へ移せるはず"
    assert ("DeleteFileW", DL + "\\plain.txt") in fake.calls


def t_network(tmp: Path) -> None:
    fake = FakeWin32(DL)
    fake.add_volume("Z:\\", "NTFS", serial=9, drive_type=W.DRIVE_REMOTE)
    fake.mkdirs("Z:\\share")
    svc, clock = make(tmp, fake, [rule("unc", "\\\\server\\share\\x", [".pdf"]), rule("z", "Z:\\share", [".pdf"])])
    probs = svc.validate_rules(DL)
    assert len(probs) == 2 and all(svc.rule_checks[i].code == "network_dest" for i in (0, 1)), probs
    svc.run_cycle()
    fake.add_file(DL + "\\n.pdf", 10, mtime=clock.t)
    settle(svc, clock)
    assert fake.get(DL + "\\n.pdf") is not None


def t_cross_volume(tmp: Path) -> None:
    fake = FakeWin32(DL)
    fake.add_volume("E:\\", "NTFS", serial=5)
    fake.mkdirs("E:\\Docs")
    svc, clock = make(tmp, fake, [rule("e", "E:\\Docs", [".pdf"])])
    svc.run_cycle()
    fake.add_file(DL + "\\ok.pdf", 10, mtime=clock.t, zone=ZONE3)
    settle(svc, clock)
    moved = fake.get("E:\\Docs\\ok.pdf")
    assert moved is not None and moved.streams.get(W.ZONE_STREAM) == ZONE3 and fake.get(DL + "\\ok.pdf") is None
    fake.copy_drops_streams = True
    fake.add_file(DL + "\\bad.pdf", 10, mtime=clock.t, zone=ZONE3)
    settle(svc, clock)
    assert fake.get(DL + "\\bad.pdf") is not None, "検証に失敗したのに元が消えた"
    assert fake.get("E:\\Docs\\_dropsort_failed\\bad.pdf") is not None
    f = [r for r in svc.oplog.all() if r["op"] == "failed"]
    assert f and f[-1]["reason"] == "verify_mismatch", f
    dels = [c for c in fake.calls if c[0] == "DeleteFileW"]
    assert dels == [("DeleteFileW", DL + "\\ok.pdf")], dels


def t_undo(tmp: Path) -> None:
    fake, svc, clock = started(tmp, [rule("pdf", "C:\\Docs\\PDF", [".pdf"])])
    for n in ("1", "2", "3"):
        fake.add_file(DL + f"\\{n}.pdf", 10, mtime=clock.t, zone=ZONE3)
        settle(svc, clock)
    assert fake.names_in("C:\\Docs\\PDF") == ["1.pdf", "2.pdf", "3.pdf"]
    u = svc.undo(2)
    assert len(u.restored) == 2 and fake.names_in(DL) == ["2.pdf", "3.pdf"], (u, fake.names_in(DL))
    assert fake.get(DL + "\\3.pdf").streams.get(W.ZONE_STREAM) == ZONE3  # type: ignore[union-attr]
    settle(svc, clock)
    assert fake.names_in(DL) == ["2.pdf", "3.pdf"], "戻したファイルが再び振り分けられた"
    u = svc.undo(2)
    assert len(u.restored) == 1 and fake.names_in(DL) == ["1.pdf", "2.pdf", "3.pdf"]
    assert sum(1 for r in svc.oplog.all() if r["op"] == "undo" and r.get("undo_of")) == 3
    assert svc.undo(1).nothing
    # 同名があれば連番で戻す / 変更されていれば戻さない
    fake.add_file(DL + "\\4.pdf", 10, mtime=clock.t)
    settle(svc, clock)
    fake.add_file(DL + "\\4.pdf", 99, mtime=clock.t)
    u = svc.undo(1)
    assert u.restored and u.restored[0]["dst"].endswith("4 (1).pdf"), u
    fake.add_file(DL + "\\5.pdf", 10, mtime=clock.t)
    settle(svc, clock)
    fake.get("C:\\Docs\\PDF\\5.pdf").size = 11  # type: ignore[union-attr]
    u = svc.undo(1)
    assert not u.restored and u.problems[0]["reason"] == "undo_changed", u


def t_archive(tmp: Path) -> None:
    fake, svc, clock = started(tmp, [], archive={"enabled": True, "mode": "dry-run", "idle_days": 30,
                                                  "dir_name": "_archive", "use_atime": False})
    old = T0 - 90 * 86400
    fake.add_file(DL + "\\stale.zip", 10, mtime=old, ctime=old)
    svc.run_cycle()
    clock.advance(31 * 86400)  # 初回観測から 31 日
    fake.add_file(DL + "\\fresh.zip", 10, mtime=clock.t)
    r = svc.archive_now(False)
    wa = [i for i in r.items if i["op"] == "would_archive"]
    assert len(wa) == 1 and wa[0]["name"] == "stale.zip" and "\\_archive\\" in wa[0]["dst"], r.items
    r = svc.archive_now(True)
    assert fake.names_in(DL) == ["fresh.zip"] and any(n for n in fake.dirs if n.endswith("_archive")), fake.names_in(DL)


def t_no_url(tmp: Path) -> None:
    fake, svc, clock = started(tmp, [rule("pdf", "C:\\Docs\\PDF", [".pdf"], host_domain=["example.com"]),
                                     rule("d", "C:\\Docs\\PDF", [".txt"], mode="dry-run")])
    fake.add_file(DL + "\\h.pdf", 10, mtime=clock.t, zone=ZONE3)
    fake.add_file(DL + "\\h.txt", 10, mtime=clock.t, zone=ZONE3)
    settle(svc, clock)
    assert fake.get("C:\\Docs\\PDF\\h.pdf") is not None, "host_domain の後方一致が効いていない"
    for p in (svc.oplog.path, svc.dryrun.path, svc.store.path):
        text = p.read_text(encoding="utf-8")
        assert "http" not in text and "SECRET" not in text, p
    rec = [r for r in svc.oplog.all() if r["op"] == "move"][0]
    assert rec.get("domain") == "dl.example.com" and rec.get("zone_id") == 3, rec


TESTS: list[tuple[str, Callable[[Path], None]]] = [
    ("基準線と sort-existing", t_baseline),
    ("完了判定(一時名・安定・0バイト・使用中)", t_completion),
    ("ルール評価(先勝ち・dry-run)", t_rules),
    ("名前衝突の連番", t_suffix),
    ("危険名の検出", t_guard),
    ("MOTW 付きの exFAT 行きを拒否", t_motw_refuse),
    ("ネットワーク宛ルールの無効化", t_network),
    ("別ボリューム移動の検証", t_cross_volume),
    ("undo", t_undo),
    ("アーカイブ", t_archive),
    ("URL を記録しない", t_no_url),
]


def run(verbose: bool = True) -> int:
    failed = 0
    for name, fn in TESTS:
        with tempfile.TemporaryDirectory(prefix="dropsort-selftest-") as d:
            try:
                fn(Path(d))
                ok, detail = True, ""
            except Exception as e:  # noqa: BLE001 - 失敗を集計して表示する
                ok, detail = False, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"
        failed += 0 if ok else 1
        if verbose:
            print(f"[{'OK' if ok else 'NG'}] {name}" + (f"\n    {detail}" if detail else ""))
    if verbose:
        print(f"DropSort selftest: {len(TESTS) - failed}/{len(TESTS)} 合格")
    return 0 if failed == 0 else 1


__all__ = ["run", "FakeClock", "make", "rule", "settle", "started", "DL", "ZONE3", "T0"]
