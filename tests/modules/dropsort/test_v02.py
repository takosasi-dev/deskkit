# v0.2 の追加分のテスト: ルールの試し当て(D1)・ルールの実績と本番化の提案(D3)・モード連携の停止(M3)・
# スヌーズ中の自動処理の停止と再開時のフルスキャン・diagnostics()。
from __future__ import annotations

import dataclasses
from pathlib import Path

from deskkit.modules.dropsort import selftest as st
from deskkit.modules.dropsort import stats as dstats
from deskkit.modules.dropsort.config import load_config
from deskkit.modules.dropsort.fakewin32 import FakeWin32
from deskkit.modules.dropsort.module import DropSortModule
from deskkit.modules.dropsort.motw import Motw
from deskkit.modules.dropsort.oplog import JsonlLog
from deskkit.modules.dropsort.tester import make_motw, simulate

from .helpers import FakeCtx, wait_worker

DL = st.DL
PDF = "C:\\Docs\\PDF"
DAY = 86400


# ------------------------------------------------------------------ D1 試し当て
def test_simulate_first_match_and_reasons() -> None:
    cfg = load_config({"rules": [
        st.rule("inet-pdf", PDF, [".pdf"], zone_ids=[3]),
        st.rule("big", "C:\\Big", None, mode="dry-run", size_min=1000),
        st.rule("gh", "C:\\GH", None, host_domain=["github.com"]),
        st.rule("bad", "C:\\x", None, name_regex="("),
        st.rule("dated", "C:\\Sorted\\{yyyy}\\{domain}", [".zip"]),
    ]})
    r = simulate(cfg, {}, "a.pdf", 5000, Motw(True, 3, "dl.example.com"), st.T0)
    assert r.matched is not None and r.matched.name == "inet-pdf" and r.dest == PDF
    st_by = {v.name: v for v in r.verdicts}
    assert st_by["big"].status == "shadowed" and st_by["bad"].status == "disabled"
    assert st_by["gh"].status == "miss" and "dl.example.com" in st_by["gh"].text
    r = simulate(cfg, {}, "a.pdf", 5000, Motw(False), st.T0)
    assert r.matched is not None and r.matched.name == "big"
    assert "ゾーン" in {v.name: v for v in r.verdicts}["inet-pdf"].text
    assert any("試運転" in n for n in r.notes)
    r = simulate(cfg, {}, "x.zip", 1, make_motw("3", "https://user@Codeload.GitHub.com/a?token=SECRET"), st.T0)
    assert r.matched is not None and r.matched.name == "gh"  # URL を貼ってもドメインだけ使う
    r = simulate(cfg, {}, "x.zip", 1, make_motw("none", "github.com"), st.T0)
    assert r.matched is not None and r.matched.name == "dated"
    assert r.dest is not None and r.dest.startswith("C:\\Sorted\\") and r.dest.endswith("\\unknown")
    r = simulate(cfg, {}, "report.pdf.exe", 1, Motw(False), st.T0)
    assert r.blocked == "double_extension"
    r = simulate(cfg, {}, "big.iso.crdownload", 1, Motw(False), st.T0)
    assert r.blocked == "temp"
    r = simulate(cfg, {}, "note.txt", 1, Motw(False), st.T0)
    assert r.matched is None and any("当たりません" in n for n in r.notes)


def test_simulate_touches_no_files(tmp_path: Path) -> None:
    fake, svc, _clock = st.started(tmp_path, [st.rule("pdf", PDF, [".pdf"])])
    svc.validate_rules(DL)
    fake.calls.clear()
    before = (sorted(fake.files), sorted(fake.dirs))
    r = simulate(svc.cfg, svc.rule_checks, "a.pdf", 1, Motw(False), st.T0)
    assert r.matched is not None
    assert fake.calls == [] and (sorted(fake.files), sorted(fake.dirs)) == before


# ------------------------------------------------------------------ D3 実績と本番化の提案
def _write(tmp_path: Path, dry: list[tuple[float, str, str]], ops: list[tuple[float, dict[str, object]]]) -> tuple[Path, Path]:
    dj = JsonlLog(tmp_path / "dryrun.jsonl", "d")
    for t, op, rule in dry:
        dj.append({"op": op, "src": "C:\\x", "rule": rule}, t)
    oj = JsonlLog(tmp_path / "oplog.jsonl")
    for t, rec in ops:
        oj.append(rec, t)
    return oj.path, dj.path


def test_stats_and_promotion(tmp_path: Path) -> None:
    now = st.T0
    dry = [(now - 9 * DAY + i * 3600, "would_move", "pdf") for i in range(5)]
    dry += [(now - 2 * DAY, "would_move", "img"), (now - 1 * DAY, "would_refuse", "img")]
    dry += [(now - 30 * DAY, "would_move", "old")]
    ops: list[tuple[float, dict[str, object]]] = [
        (now - DAY, {"op": "move", "src": "a", "dst": "b", "rule": "zip"}),
        (now - DAY, {"op": "move", "src": "a", "dst": "b", "rule": "zip"}),
        (now - DAY + 10, {"op": "undo", "src": "b", "dst": "a", "rule": "zip", "undo_of": "x"}),
        (now - DAY + 20, {"op": "failed", "src": "b", "dst": "a", "rule": "zip", "reason": "undo_changed", "source": "undo"}),
        (now - DAY + 30, {"op": "refused", "src": "c", "dst": "d", "rule": "zip", "reason": "motw_unsupported_fs"}),
    ]
    op_path, dry_path = _write(tmp_path, dry, ops)
    s = dstats.compute(op_path, dry_path, now)
    assert (s["pdf"].planned, s["pdf"].refuse_planned) == (5, 0)
    assert dstats.promotion_ready(s["pdf"], now)
    assert not dstats.promotion_ready(s["pdf"], now - 3 * DAY)          # 7日に満たない
    assert not dstats.promotion_ready(s["img"], now)                     # 拒否予定がある・件数不足
    assert s["old"].planned == 0 and not dstats.promotion_ready(s["old"], now)  # 14日より前は数えない
    assert (s["zip"].moved, s["zip"].undone, s["zip"].problems) == (2, 1, 1)  # undo の失敗は問題に数えない
    assert dstats.summary(s["pdf"], False) == "試運転 14日で 5 件の予定・拒否予定 0 件"
    assert "取り消し 1 件" in dstats.summary(s["zip"], True) and "拒否/失敗 1 件" in dstats.summary(s["zip"], True)
    assert not dstats.promotion_ready(None, now)


def test_rule_stats_cached_until_logs_change(tmp_path: Path) -> None:
    m, ctx, fake, clock = _module(tmp_path, [st.rule("pdf", PDF, [".pdf"], mode="dry-run")])
    m.start()
    try:
        got: list[dict[str, dstats.RuleStats]] = []
        m.rule_stats(got.append)
        assert got == []  # 作業スレッドで集計
        wait_worker(m, ctx)
        assert len(got) == 1
        m.rule_stats(got.append)
        assert len(got) == 2 and got[1] is got[0]  # キャッシュをすぐ返す
        m._kick()
        wait_worker(m, ctx)  # 基準線
        fake.add_file(DL + "\\a.pdf", 10, mtime=clock.t)
        for _ in range(2):
            clock.advance(10)
            m._kick()
            wait_worker(m, ctx)
        m.rule_stats(got.append)
        wait_worker(m, ctx)
        assert got[-1]["pdf"].planned == 1
    finally:
        m.stop()


# ------------------------------------------------------------------ M3 モード連携・スヌーズ
def _module(tmp_path: Path, rules: list[dict[str, object]], **over: object) -> tuple[DropSortModule, FakeCtx, FakeWin32, st.FakeClock]:
    fake = FakeWin32(DL)
    fake.mkdirs(PDF)
    ctx = FakeCtx(tmp_path, {"watch_mode": "poll", "rules": rules, **over})
    ctx.modes = [("game", "ゲーム"), ("work", "作業")]
    clock = st.FakeClock()
    m = DropSortModule(ctx, api=fake, clock=clock)
    return m, ctx, fake, clock


def _scan(m: DropSortModule, ctx: FakeCtx, clock: st.FakeClock, rounds: int = 2) -> None:
    for _ in range(rounds):
        clock.advance(10)
        m._kick()
        wait_worker(m, ctx)


def test_pause_in_modes(tmp_path: Path) -> None:
    m, ctx, fake, clock = _module(tmp_path, [st.rule("pdf", PDF, [".pdf"])], pause_in_modes=["game", "gone"])
    m.start()
    try:
        assert set(ctx.handlers) >= {"modeshift.switched", "modeshift.reverted", "host.snooze_changed"}
        _scan(m, ctx, clock, 1)  # 基準線
        notes_before = len(ctx.notes)
        ctx.fire("modeshift.switched", {"mode": "work", "run_id": "r1", "failed": 0})
        assert m.auto_blocked() is None
        ctx.fire("modeshift.switched", {"mode": "game", "run_id": "r2", "failed": 0})
        assert m.auto_blocked() == "mode" and "ゲーム" in ctx.status
        fake.add_file(DL + "\\a.pdf", 10, mtime=clock.t)
        _scan(m, ctx, clock, 3)
        assert fake.names_in(PDF) == []  # 止まっている
        ctx.fire("modeshift.reverted", {"mode": "game", "run_id": "r2"})
        assert m.auto_blocked() is None
        assert m._timers["debounce"].active and m._timers["debounce"].interval == 0  # 再開でフルスキャン
        _scan(m, ctx, clock, 2)
        assert fake.names_in(PDF) == ["a.pdf"]
        # 止めた・再開したことは通知しない(整理の通知だけ)
        assert all("整理しました" in n[0] for n in ctx.notes[notes_before:]), ctx.notes[notes_before:]
        # リスト外のモードへの switched でも再開する
        ctx.fire("modeshift.switched", {"mode": "game", "run_id": "r3", "failed": 0})
        assert m.auto_blocked() == "mode"
        ctx.fire("modeshift.switched", {"mode": "work", "run_id": "r4", "failed": 0})
        assert m.auto_blocked() is None
        # 設定から外せばその場で再開する
        ctx.fire("modeshift.switched", {"mode": "game", "run_id": "r5", "failed": 0})
        sec = m.settings_dict()
        sec["pause_in_modes"] = ["gone"]
        m.update_settings(sec)
        assert m.auto_blocked() is None
        assert not ctx.errors, ctx.errors
    finally:
        m.stop()


def test_manual_scan_runs_while_paused_by_mode(tmp_path: Path) -> None:
    m, ctx, fake, clock = _module(tmp_path, [st.rule("pdf", PDF, [".pdf"])], pause_in_modes=["game"])
    m.start()
    try:
        _scan(m, ctx, clock, 1)
        ctx.fire("modeshift.switched", {"mode": "game", "run_id": "r", "failed": 0})
        fake.add_file(DL + "\\a.pdf", 10, mtime=clock.t)
        for _ in range(2):
            clock.advance(10)
            m.request_scan(0, manual=True)  # 利用者が押した「今すぐスキャン」は止めない(契約 §1)
            m._kick()
            wait_worker(m, ctx)
        assert fake.names_in(PDF) == ["a.pdf"]
    finally:
        m.stop()


def test_snooze_skips_auto_and_rescans_on_resume(tmp_path: Path) -> None:
    m, ctx, fake, clock = _module(tmp_path, [st.rule("pdf", PDF, [".pdf"])])
    m.start()
    try:
        _scan(m, ctx, clock, 1)
        ctx.snoozed = True
        ctx.fire("host.snooze_changed", {"snoozed": True, "until": None})
        assert m.auto_blocked() == "snooze" and "スヌーズ" in ctx.status
        fake.add_file(DL + "\\a.pdf", 10, mtime=clock.t)
        _scan(m, ctx, clock, 3)
        assert fake.names_in(PDF) == []
        ctx.snoozed = False
        m._timers["debounce"].stop()
        ctx.fire("host.snooze_changed", {"snoozed": False, "until": None})
        assert m._timers["debounce"].active  # 再開でフルスキャン
        _scan(m, ctx, clock, 2)
        assert fake.names_in(PDF) == ["a.pdf"]
        assert not ctx.errors, ctx.errors
    finally:
        m.stop()


def test_ctx_without_v02_api_still_works(tmp_path: Path) -> None:
    class OldCtx(FakeCtx):
        is_snoozed = None  # type: ignore[assignment]
        list_modes = None  # type: ignore[assignment]

    fake = FakeWin32(DL)
    ctx = OldCtx(tmp_path, {"watch_mode": "poll"})
    m = DropSortModule(ctx, api=fake)
    assert not m.snoozed() and m.list_modes() == [] and m.auto_blocked() is None


# ------------------------------------------------------------------ diagnostics
def test_diagnostics_has_no_paths_or_names(tmp_path: Path) -> None:
    m, ctx, fake, clock = _module(tmp_path, [st.rule("pdf", PDF, [".pdf"]), st.rule("t", "C:\\S\\{yyyy}", [".zip"],
                                                                                     mode="dry-run")],
                                  pause_in_modes=["game"])
    m.start()
    try:
        _scan(m, ctx, clock, 1)
        fake.add_file(DL + "\\secret-name.pdf.exe", 10, mtime=clock.t,
                      zone=b"[ZoneTransfer]\r\nZoneId=3\r\nHostUrl=https://host.example/x\r\n")
        _scan(m, ctx, clock, 2)
        ctx.fire("modeshift.switched", {"mode": "game", "run_id": "r", "failed": 0})
        d = m.diagnostics()
        assert d["rules_mode"] == "mixed" and d["rules_total"] == 2 and d["rules_invalid"] == 1
        assert d["rules_template"] == 1 and d["paused"] is True and d["paused_reason"] == "mode"
        assert d["flagged"] == 1 and d["watch"] == "poll" and d["downloads_resolved"] is True
        assert all(isinstance(v, str | int | bool) for v in d.values())
        text = repr(d)
        for bad in (DL, "secret", "http", "host.example", "game", "C:\\", "pdf.exe"):
            assert bad not in text, bad
    finally:
        m.stop()


def test_cancel_flag_reset_on_restart(tmp_path: Path) -> None:
    m, ctx, fake, clock = _module(tmp_path, [st.rule("pdf", PDF, [".pdf"])])
    m.start()
    m.stop()
    m.start()
    try:
        assert not m.service.cancel.is_set()
        m.service.set_config(dataclasses.replace(m.service.cfg, stable_seconds=0))
        _scan(m, ctx, clock, 1)
        fake.add_file(DL + "\\a.pdf", 10, mtime=clock.t)
        _scan(m, ctx, clock, 1)
        assert fake.names_in(PDF) == ["a.pdf"]
    finally:
        m.stop()
