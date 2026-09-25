# 保持上限のスピンボックスの誤操作対策(v0.2): 値の変更は約700ms 落ち着いてから判定し、既存の履歴が消えるときは
# 件数を示して確認する。取消なら元の値に戻し、何も消さない・保存しない。
from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtTest import QTest

from deskkit.modules.clipshelf import page as page_mod


def _record(m: Any, api: Any, clock: Any, n: int) -> None:
    for i in range(n):
        clock.advance(minutes=1)
        api.put(f"r{i}")
        m.monitor.process()


def _page(m: Any) -> Any:
    p = m.create_page()
    p.resize(1000, 800)
    return p


def test_step_does_not_trim_immediately(module_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    m, ctx, api, clock = module_factory({"mode": "record", "retention": {"max_items": 10, "max_days": 0}})
    _record(m, api, clock, 8)
    asked: list[str] = []
    monkeypatch.setattr(page_mod.W, "confirm", lambda _p, _t, text, **k: (asked.append(text), (False, []))[1])
    p = _page(m)
    writes = len(ctx.writes)
    for v in (9, 8, 7, 6, 5):  # ホイールで数段回した
        p.sp_items.setValue(v)
    assert p._ret_timer.isActive()
    assert len(m.store.history()) == 8 and len(ctx.writes) == writes and not asked  # まだ何も起きない
    QTest.qWait(page_mod.RETENTION_DEBOUNCE_MS + 250)
    assert len(asked) == 1 and "3 件" in asked[0]  # 1回だけ、消える件数つきで確認
    # 取消: 元の値に戻り、何も消えず、保存もしない
    assert p.sp_items.value() == 10 and m.config.max_items == 10
    assert len(m.store.history()) == 8 and len(ctx.writes) == writes
    assert not any(o["op"] == "retention_trim" for o in m.ops.read())
    p.close()


def test_confirm_ok_saves_and_trims(module_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    m, ctx, api, clock = module_factory({"mode": "record", "retention": {"max_items": 10, "max_days": 0}})
    _record(m, api, clock, 8)
    monkeypatch.setattr(page_mod.W, "confirm", lambda *a, **k: (True, []))
    p = _page(m)
    p.sp_items.setValue(5)
    p._apply_retention()
    assert not p._ret_timer.isActive()
    assert m.config.max_items == 5 and ctx.writes[-1][0]["retention"] == {"max_items": 5, "max_days": 0}
    assert len(m.store.history()) == 5
    assert [o["deleted"] for o in m.ops.read() if o["op"] == "retention_trim"] == [3]
    p.close()


def test_raising_limit_saves_without_confirm(module_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    m, ctx, api, clock = module_factory({"mode": "record", "retention": {"max_items": 10, "max_days": 30}})
    _record(m, api, clock, 3)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("消える履歴が無いときは確認しない")

    monkeypatch.setattr(page_mod.W, "confirm", boom)
    p = _page(m)
    p.sp_items.setValue(50)
    p.sp_days.setValue(1)  # 記録は数分前なので 1 日でも消えない
    p._apply_retention()
    assert m.config.max_items == 50 and m.config.max_days == 1 and len(m.store.history()) == 3
    p.close()


def test_days_limit_counts_old_items(module_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    m, ctx, api, clock = module_factory({"mode": "record", "retention": {"max_items": 0, "max_days": 30}})
    _record(m, api, clock, 2)
    clock.advance(days=10)
    _record(m, api, clock, 1)
    assert m.retention_preview(0, 5) == 2 and m.retention_preview(0, 30) == 0
    asked: list[str] = []
    monkeypatch.setattr(page_mod.W, "confirm", lambda _p, _t, text, **k: (asked.append(text), (True, []))[1])
    p = _page(m)
    p.sp_days.setValue(5)
    p._apply_retention()
    assert "2 件" in asked[0] and len(m.store.history()) == 1
    p.close()
