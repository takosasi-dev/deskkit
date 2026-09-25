# AC-4: 一時フォルダに見立てたフォルダで、8 日前のファイルは recycle に渡り、1 日前とジャンクションの先は渡らない(INV-4)。
# FR-9 の分割送り・中断・送る直前の再確認と、AC-7(見張りの通知は 24 時間に 1 回)・一時停止・起動直後の待ち。
# ごみ箱は偽物(本物の %TEMP% のファイルは送らない)。
from __future__ import annotations

import json
import os
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from deskkit.fileops import RecycleResult
from deskkit.modules.pccheckup import cleanup, fswalk, watcher
from deskkit.modules.pccheckup.checks.base import GiB
from deskkit.modules.pccheckup.fakes import FakeProbes
from deskkit.modules.pccheckup.probes import DiskInfo, count_startup

DAY = 86400


def _file(p: Path, age_days: float, size: int = 10) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    t = time.time() - age_days * DAY
    os.utime(p, (t, t))
    return p


def _junction(target: Path, link: Path) -> None:
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


class Recorder:
    def __init__(self, reasons: dict[str, str] | None = None, abort_at: int | None = None) -> None:
        self.calls: list[list[Path]] = []
        self.reasons = reasons or {}
        self.abort_at = abort_at

    def __call__(self, paths: Sequence[Path], _hwnd: int | None) -> RecycleResult:
        self.calls.append(list(paths))
        r = RecycleResult()
        for p in paths:
            if self.abort_at is not None and len(self.calls) >= self.abort_at:
                r.skipped.append((p, "aborted"))
                continue
            reason = self.reasons.get(p.name)
            if reason:
                r.skipped.append((p, reason))  # type: ignore[arg-type]
            else:
                r.sent.append(p)
        return r

    @property
    def names(self) -> set[str]:
        return {p.name for c in self.calls for p in c}


def test_ac4_old_goes_new_and_junction_target_do_not(tmp_path: Path) -> None:
    root = tmp_path / "Temp"
    _file(root / "old8.tmp", 8, 111)
    _file(root / "sub" / "deep_old.tmp", 30, 5)
    _file(root / "new1.tmp", 1)
    _file(root / "exactly7.tmp", 7 - 0.001)
    outside = tmp_path / "Outside"
    _file(outside / "precious.doc", 100)
    _junction(outside, root / "junction")
    now = time.time()
    scan = cleanup.scan_old_temp(str(root), now, lambda: False)
    rec = Recorder()
    res = cleanup.recycle_old_temp(scan, now, rec, None, lambda: False)
    assert rec.names == {"old8.tmp", "deep_old.tmp"}
    assert res.sent == 2 and res.bytes == 116 and res.skipped == 0
    assert (outside / "precious.doc").exists()


def test_is_old_boundary() -> None:
    now = 1_000_000_000.0
    assert not cleanup.is_old(now - 7 * DAY, now)
    assert cleanup.is_old(now - 7 * DAY - 1, now)


def test_recheck_before_sending(tmp_path: Path) -> None:
    root = tmp_path / "Temp"
    a = _file(root / "a.tmp", 10)
    b = _file(root / "b.tmp", 10)
    now = time.time()
    scan = cleanup.scan_old_temp(str(root), now, lambda: False)
    os.utime(a, (now, now))  # 一覧の後で更新された
    scan.files.append((str(tmp_path / "elsewhere.tmp"), 1))  # 念のため: %TEMP% の外は送らない
    _file(tmp_path / "elsewhere.tmp", 30)
    rec = Recorder()
    res = cleanup.recycle_old_temp(scan, time.time(), rec, None, lambda: False)
    assert rec.names == {b.name}
    assert res.skipped == 2 and res.reasons["not_eligible"] == 2


def test_recheck_rejects_path_through_junction(tmp_path: Path) -> None:
    root = tmp_path / "Temp"
    root.mkdir()
    outside = tmp_path / "Outside"
    f = _file(outside / "x.tmp", 30)
    _junction(outside, root / "j")
    assert not cleanup.still_eligible(str(root), str(root / "j" / f.name), time.time())
    assert cleanup.is_under(str(root), str(root / "a"))
    assert not cleanup.is_under(str(root), str(root))
    assert not cleanup.is_under(str(root), str(tmp_path / "Temp2" / "a"))


def test_chunks_in_use_and_abort(tmp_path: Path) -> None:
    root = tmp_path / "Temp"
    for i in range(45):
        _file(root / f"f{i:02d}.tmp", 9)
    now = time.time()
    scan = cleanup.scan_old_temp(str(root), now, lambda: False)
    rec = Recorder(reasons={"f03.tmp": "in_use", "f04.tmp": "failed"})
    res = cleanup.recycle_old_temp(scan, now, rec, None, lambda: False)
    assert [len(c) for c in rec.calls] == [20, 20, 5]
    assert res.sent == 43 and res.skipped == 2 and res.reasons == {"in_use": 1, "failed": 1}
    # 恒久削除の確認で「いいえ」→ 残りは送らない
    rec2 = Recorder(abort_at=2)
    res2 = cleanup.recycle_old_temp(scan, now, rec2, None, lambda: False)
    assert len(rec2.calls) == 2 and res2.aborted and res2.sent == 20 and res2.skipped == 25
    # 終了の合図は chunk の合間で見る
    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    rec3 = Recorder()
    res3 = cleanup.recycle_old_temp(scan, now, rec3, None, stop)
    assert len(rec3.calls) == 1 and res3.aborted and res3.skipped == 25


def test_module_cleanup_flow_writes_counts_only(make_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "Temp"
    _file(root / "old.tmp", 9, 2048)
    _file(root / "new.tmp", 0)
    monkeypatch.setenv("TEMP", str(root))
    rec = Recorder()
    m, ctx, _ = make_module(recycle_fn=rec)
    got: list[Any] = []
    m.scan_temp(got.append)
    assert got[0].count == 1 and got[0].bytes == 2048
    done: list[Any] = []
    m.recycle_temp(got[0], 1234, done.append)
    assert done[0].sent == 1 and done[0].bytes == 2048
    row = json.loads((ctx.data_dir / "ops.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert set(row) == {"ts", "op", "sent", "skipped", "bytes"} and row["op"] == "recycle_temp"


def test_scan_missing_temp_returns_none(make_module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMP", str(tmp_path / "nope"))
    monkeypatch.setenv("TMP", str(tmp_path / "nope2"))
    m, _ctx, _ = make_module()
    got: list[Any] = []
    m.scan_temp(got.append)
    assert got == [None]


# ------------------------------------------------------------------ fswalk
def test_fswalk_skips_reparse_and_cloud_only(tmp_path: Path) -> None:
    root = tmp_path / "R"
    _file(root / "a.bin", 0, 100)
    _file(root / "d" / "b.bin", 0, 50)
    _file(tmp_path / "O" / "c.bin", 0, 1000)
    _junction(tmp_path / "O", root / "j")
    st = fswalk.dir_size(str(root), lambda: False)
    assert (st.bytes, st.files, st.partial) == (150, 2, False)
    st2 = fswalk.dir_size(str(root), lambda: True)
    assert st2.partial
    st3 = fswalk.dir_size(str(tmp_path / "missing"), lambda: False)
    assert st3.denied and st3.bytes == 0


def test_count_startup_even_enabled_odd_disabled() -> None:
    info = count_startup([
        (["A", "B", "C"], {"a": b"\x02\x00", "b": b"\x03\x00", "x": b"\x03"}),  # C は値なし → 有効
        (["D"], {"d": b"\x06"}),
        (["E"], {"e": b"\x07"}),
    ])
    assert (info.enabled, info.disabled) == (3, 2)


# ------------------------------------------------------------------ 見張り(AC-7)
def _watch_module(make_module: Any, free: int) -> tuple[Any, Any, list[float]]:
    clock = [1_000_000.0]
    fp = FakeProbes(sysdrive=DiskInfo("C:\\", 100 * GiB, free))
    m, ctx, _ = make_module(fp, section={"watch_disk": True}, now=lambda: clock[0])
    return m, ctx, clock


def test_ac7_notify_once_per_24h(make_module: Any) -> None:
    m, ctx, clock = _watch_module(make_module, 12 * GiB)  # 12% だが 15GB 未満 → warn
    assert m.check_disk_now() == "warn"
    assert len(ctx.notifications) == 1
    title, text, level, on_click = ctx.notifications[0]
    assert level == "warn" and "C:" in text
    clock[0] += 6 * 3600
    assert m.check_disk_now() is None
    clock[0] += 17 * 3600
    assert m.check_disk_now() is None  # まだ 24 時間たっていない
    assert len(ctx.notifications) == 1
    clock[0] += 3600
    assert m.check_disk_now() == "warn"
    assert len(ctx.notifications) == 2
    # FR-12: 通知のクリックで画面を開き、容量の診断を始める
    on_click()
    assert ctx.shown == 1 and m.state.categories == ["storage"]


def test_watch_status_change_notifies_and_state_persists(make_module: Any) -> None:
    m, ctx, clock = _watch_module(make_module, 12 * GiB)
    m.check_disk_now()
    m.watcher._read = lambda: DiskInfo("C:\\", 100 * GiB, 1 * GiB)
    assert m.check_disk_now() == "bad"  # status が変われば 24 時間以内でも出す
    st = json.loads((ctx.data_dir / "watch.json").read_text(encoding="utf-8"))
    assert set(st) == {"last_check", "notified"} and set(st["notified"]) == {"warn", "bad"}


def test_watch_good_and_snoozed(make_module: Any) -> None:
    m, ctx, _ = _watch_module(make_module, 80 * GiB)
    assert m.check_disk_now() is None and not ctx.notifications
    ctx.snoozed = True
    m.watcher._read = lambda: (_ for _ in ()).throw(AssertionError("一時停止中は読まない"))
    assert m.check_disk_now() is None


def test_watch_timers(make_module: Any) -> None:
    m, ctx, clock = _watch_module(make_module, 80 * GiB)
    ms, _cb, single = ctx.timers[0]
    assert single and ms == watcher.FIRST_MIN_DELAY_S * 1000
    m.check_disk_now()
    clock[0] += 3600
    assert m.watcher.first_delay_s() == pytest.approx(5 * 3600)
    clock[0] += 10 * 3600
    assert m.watcher.first_delay_s() == watcher.FIRST_MIN_DELAY_S
    m.watcher._on_first()
    assert ctx.timers[-1][0] == watcher.INTERVAL_S * 1000 and not ctx.timers[-1][2]


def test_watch_off_by_default_no_timer(make_module: Any) -> None:
    _m, ctx, _ = make_module()
    assert ctx.timers == []
