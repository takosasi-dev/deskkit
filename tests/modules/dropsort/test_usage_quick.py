# 利用状況(Module.usage)とクイックアクションのテスト。
from __future__ import annotations

import dataclasses
import time
from pathlib import Path

from deskkit.modules.dropsort import selftest as st
from deskkit.modules.dropsort.fakewin32 import FakeWin32
from deskkit.modules.dropsort.module import DropSortModule

from .helpers import FakeCtx

DL = st.DL
PDF = "C:\\Docs\\PDF"


def _module(tmp_path: Path, rules: list[dict[str, object]]) -> tuple[DropSortModule, FakeCtx, FakeWin32]:
    fake = FakeWin32(DL)
    fake.mkdirs(PDF)
    ctx = FakeCtx(tmp_path, {"watch_mode": "poll", "rules": rules})
    return DropSortModule(ctx, api=fake, clock=time.time), ctx, fake  # usage は実時計の「今日」で数える


def test_usage_series_and_r3_hint(tmp_path: Path) -> None:
    m, _ctx, fake = _module(tmp_path, [st.rule("pdf", PDF, [".pdf"]), st.rule("txt", PDF, [".txt"], mode="dry-run")])
    svc = m.service
    svc.set_config(dataclasses.replace(svc.cfg, stable_seconds=0))
    svc.run_cycle()  # 基準線
    now = time.time()
    for n in ("a", "b", "c"):
        fake.add_file(DL + f"\\{n}.pdf", 10, mtime=now)
    fake.add_file(DL + "\\x.pdf.exe", 10, mtime=now)
    fake.add_file(DL + "\\n.txt", 10, mtime=now)
    svc.run_cycle()
    assert fake.names_in(PDF) == ["a.pdf", "b.pdf", "c.pdf"]
    svc.undo(1)
    series = {s.key: s for s in m.usage(7)}
    assert set(series) == {"auto_moves", "undos", "flagged_refused", "would_move"}
    assert all(len(s.per_day) == 7 for s in series.values())
    assert series["auto_moves"].primary and series["auto_moves"].per_day[-1] == 3
    assert series["undos"].per_day[-1] == 1 and series["undos"].good_when == "low"
    hint = series["undos"].hint or ""
    assert "R-3" in hint and "1/3" in hint and "33.3%" in hint
    assert series["flagged_refused"].per_day[-1] == 1 and series["flagged_refused"].good_when == "neutral"
    assert series["would_move"].per_day[-1] == 1
    text = repr(list(series.values()))
    assert "a.pdf" not in text and DL not in text  # パス・ファイル名を入れない


def test_usage_empty(tmp_path: Path) -> None:
    m, _ctx, _fake = _module(tmp_path, [])
    s = {x.key: x for x in m.usage(30)}
    assert s["auto_moves"].per_day == [0] * 30 and "自動移動なし" in (s["undos"].hint or "")


def test_quick_actions_open_page(tmp_path: Path) -> None:
    m, ctx, _fake = _module(tmp_path, [])
    m.start()
    try:
        acts = {q[0]: q[1] for q in ctx.quick}
        assert "既存ファイルを整理(試運転)" in acts and "アーカイブを今すぐ評価(試運転)" in acts
        tray = {t.label for t in ctx.tray}
        assert not (set(acts) & tray)  # トレイにある操作は重ねない
        acts["要確認のファイルを見る"]()
        assert ctx.page_shown == 1 and m.take_page_request() == ("flagged", None)
        assert m.take_page_request() is None
        acts["既存ファイルを整理(試運転)"]()
        assert m.take_page_request() == ("existing", "existing_dry")
        assert not ctx.errors, ctx.errors
    finally:
        m.stop()
