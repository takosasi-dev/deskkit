# モジュールの流れ: AC-2(例外のチェックだけ unknown)・中止(FR-2)・履歴(P-5・§9)・悪化の印(FR-7)・
# 開く操作の制限(FR-10)・クイックアクション(FR-13)・usage(FR-14)・diagnostics(FR-15)・設定の既定値(§9)。
from __future__ import annotations

import json
import logging
from typing import Any

from deskkit.modules.pccheckup.checks.base import Action, Cancel, GiB
from deskkit.modules.pccheckup.fakes import FakeProbes
from deskkit.modules.pccheckup.probes import CpuSample, DiskInfo, MemInfo, ProcUsage
from deskkit.modules.pccheckup.runner import BY_CATEGORY, run_category

LOG = logging.getLogger("deskkit.pccheckup.test.module")


def statuses(m: Any, cat: str) -> dict[str, str]:
    return {f.check_id: f.status for f in m.state.results[cat]}


def test_settings_defaults_written_back(make_module: Any) -> None:
    m, ctx, _ = make_module()
    assert m.watch_disk is False
    assert ctx.writes and ctx.writes[0]["watch_disk"] is False
    m2, ctx2, _ = make_module(section={"watch_disk": True})
    assert m2.watch_disk is True and not ctx2.writes


def test_ac2_one_probe_raises_others_continue(make_module: Any) -> None:
    fp = FakeProbes(raise_on={"startup", "recycle", "adapters"})
    m, _ctx, _ = make_module(fp)
    assert m.run(list(BY_CATEGORY))
    st = {**statuses(m, "perf"), **statuses(m, "net"), **statuses(m, "storage")}
    assert len(st) == 18
    assert {k for k, v in st.items() if v == "unknown"} == {"P5", "S2", "N2"}
    assert sorted(m.state.failed) == ["N2", "P5", "S2"]
    f = next(f for f in m.state.results["perf"] if f.check_id == "P5")
    assert f.title == "スタートアップのアプリ" and "読めませんでした" in f.detail


def test_winrt_failure_marks_n1_n3_n4_unknown(make_module: Any) -> None:
    m, _ctx, _ = make_module(FakeProbes(raise_on={"connectivity"}))
    m.run(["net"])
    assert statuses(m, "net") == {"N1": "unknown", "N2": "good", "N3": "unknown", "N4": "unknown", "N5": "good"}


def test_cancel_skips_following_checks() -> None:
    cancel = Cancel()

    class Probe(FakeProbes):
        def memory(self) -> MemInfo:
            cancel.set()  # P2 を調べている最中に「中止」
            return super().memory()

    res = run_category("perf", Probe(), cancel, LOG)
    assert [f.check_id for f in res.findings] == ["P1", "P2"]
    assert res.cancelled


def test_cancel_in_multi_run_marks_rest_cancelled(make_module: Any) -> None:
    holder: dict[str, Any] = {}

    class Probe(FakeProbes):
        def uptime_seconds(self) -> float:
            holder["m"].cancel()
            return super().uptime_seconds()

    fp = Probe()
    m, ctx, _ = make_module(fp)
    holder["m"] = m
    m.run(list(BY_CATEGORY))
    assert not m.state.running
    assert m.state.cancelled == {"perf", "net", "storage"}
    assert len(m.state.results["perf"]) == 4
    assert m.state.results["net"] == [] and m.state.results["storage"] == []
    assert m.history.entries() == []  # 中止したカテゴリは履歴に残さない


def test_running_blocks_second_run(make_module: Any) -> None:
    m, _ctx, _ = make_module()
    m.state.running = True
    assert m.run(["perf"]) is False
    m.state.running = False
    assert m.run(["nope"]) is False


def test_history_format_and_three_lines_for_all(make_module: Any) -> None:
    m, ctx, _ = make_module()
    m.run(list(BY_CATEGORY))
    rows = [json.loads(x) for x in (ctx.data_dir / "history.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["category"] for r in rows] == ["perf", "net", "storage"]
    for r in rows:
        assert set(r) == {"ts", "category", "results", "ms"}
        assert isinstance(r["ms"], int)
        assert all(v in ("good", "warn", "bad", "info", "unknown") for v in r["results"].values())
    assert set(rows[0]["results"]) == {"P1", "P2", "P3", "P4", "P5", "P6", "P7"}


def test_history_keeps_200(make_module: Any) -> None:
    m, ctx, _ = make_module()
    for _ in range(203):
        m.history.append("net", {"N1": "good"}, 1)
    assert len((ctx.data_dir / "history.jsonl").read_text(encoding="utf-8").splitlines()) == 200


def test_fr7_worse_than_last_time(make_module: Any) -> None:
    fp = FakeProbes()
    m, _ctx, _ = make_module(fp)
    m.run(["perf"])
    assert m.state.worse == set()
    fp.mem = MemInfo(95.0, 16 * GiB, ())
    fp.cpu_value = CpuSample(70.0, ())
    fp.raise_on = {"uptime"}
    m.run(["perf"])
    assert m.state.worse == {"P1", "P2"}  # unknown になった P4 は比べない
    # 同じ悪さのままなら印は付かない。bad → warn は良くなったので付かない
    fp.mem = MemInfo(85.0, 16 * GiB, ())
    m.run(["perf"])
    assert m.state.worse == set()


def test_copy_text_has_process_names(make_module: Any) -> None:
    fp = FakeProbes(cpu_value=CpuSample(95.0, (ProcUsage("render.exe", 80.0),)))
    m, _ctx, _ = make_module(fp)
    m.run(["perf"])
    text = m.copy_text()
    assert "render.exe" in text and "[対処が必要]" in text and "■ 重い" in text
    assert text.index("[対処が必要]") < text.index("[問題なし]")  # 悪い順


def test_open_action_allowlist(make_module: Any, tmp_path: Any) -> None:
    m, _ctx, _ = make_module()
    assert m.open_action(Action("", "uri", "ms-settings:startupapps", "x"))
    assert m.open_action(Action("", "uri", "shell:RecycleBinFolder", "x"))
    assert m.open_action(Action("", "taskmgr", "ignored.exe", "x"))
    assert m.open_action(Action("", "folder", str(tmp_path), "x"))
    assert not m.open_action(Action("", "uri", "https://example.com", "x"))
    assert not m.open_action(Action("", "uri", "ms-settings:privacy", "x"))
    assert not m.open_action(Action("", "folder", str(tmp_path / "missing"), "x"))
    assert not m.open_action(Action("", None, "cmd.exe", "x"))
    assert m.opened == ["ms-settings:startupapps", "shell:RecycleBinFolder", "taskmgr.exe", str(tmp_path)]
    assert m.open_action(Action("", "category", "storage", "x"))
    assert "storage" in m.state.results


def test_quick_actions_open_page_and_run(make_module: Any) -> None:
    m, ctx, _ = make_module()
    labels = [q.label for q in ctx.quick]
    assert labels == ["PC が重い原因を調べる", "ネットの不調を調べる", "容量を調べる"]
    ctx.quick[1].callback()
    assert ctx.shown == 1 and m.state.categories == ["net"]
    assert ctx.quick[0].enabled() is True


def test_usage_counts(make_module: Any) -> None:
    m, ctx, _ = make_module()
    m.run(list(BY_CATEGORY))
    m.ops.recycle_temp(3, 1, 100)
    u = m.usage(7)
    assert u[0].key == "diagnoses" and u[0].primary and u[0].per_day[-1] == 3 and len(u[0].per_day) == 7
    assert "R-2" in (u[0].hint or "")
    assert u[1].per_day[-1] == 1


def test_diagnostics(make_module: Any) -> None:
    m, _ctx, _ = make_module(FakeProbes(raise_on={"proxy"}, sysdrive=DiskInfo("C:\\", 100 * GiB, 1 * GiB)))
    d0 = m.diagnostics()
    assert d0 == {"watch_disk": False, "running": False, "last_category": "none"}
    m.run(["perf", "net"])
    d = m.diagnostics()
    assert d["last_category"] == "perf,net"
    assert d["last_bad"] == 1 and d["last_unknown"] == 1 and d["unknown_checks"] == "N5"
    assert all(isinstance(v, (str, int, bool)) for v in d.values())


def test_status_text(make_module: Any) -> None:
    m, ctx, _ = make_module(FakeProbes(reboot=True))
    assert ctx.status == "待機中"
    m.run(["perf"])
    assert ctx.status == "前回の診断: 注意 1"


def test_set_watch_saves(make_module: Any) -> None:
    m, ctx, _ = make_module()
    assert m.set_watch(True) is None
    assert ctx.settings()["watch_disk"] is True and m.watch_disk
    assert ctx.timers  # 見張りのタイマーを張った
    ctx.fail_write = True
    assert m.set_watch(False) is not None
    assert m.watch_disk is True
