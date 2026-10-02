# v0.4.1 B-4: 起動の証明書(B2)の期限の判定に、本物の日付ではなくモジュールの時計を使うこと。
# 偽の時計を期限(2026-10-19)の前から後へ進めると、同じ読み取り値でも文が「期限が来ます」→「期限が過ぎました」に変わる。
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from deskkit.modules.pccheckup.checks import boot
from deskkit.modules.pccheckup.fakes import FAKE_TODAY, FakeProbes, sb_raw
from deskkit.modules.pccheckup.probes import RealProbes, RegRead


class FakeClock:
    def __init__(self, when: datetime) -> None:
        self.t = when.timestamp()

    def __call__(self) -> float:
        return self.t

    def set(self, when: datetime) -> None:
        self.t = when.timestamp()


NOT_STARTED = sb_raw(status=RegRead("ok", "NotStarted"), error=RegRead("ok", 0))


def _b2(m: Any) -> Any:
    return next(f for f in m.state.results["boot"] if f.check_id == "B2")


def test_real_probes_today_follows_given_clock() -> None:
    clock = FakeClock(datetime(2026, 10, 18, 23, 59, 30))
    p = RealProbes(clock)
    assert p.today() == date(2026, 10, 18)
    clock.set(datetime(2026, 10, 19, 0, 0, 30))
    assert p.today() == date(2026, 10, 19)


def test_module_passes_its_clock_to_default_probes(make_module: Any) -> None:
    from deskkit.modules.pccheckup.module import PcCheckupModule

    m, ctx, _ = make_module()
    clock = FakeClock(datetime(2031, 3, 4, 12, 0))
    m2 = PcCheckupModule(ctx, threaded=False, now=clock)  # 既定の probes_factory(RealProbes(now))
    assert m2._probes_factory().today() == date(2031, 3, 4)
    clock.set(datetime(2031, 3, 5, 0, 1))
    assert m2._probes_factory().today() == date(2031, 3, 5)


def test_b2_text_and_reason_change_with_module_clock(make_module: Any) -> None:
    clock = FakeClock(datetime(2026, 10, 18, 23, 59))
    m, _ctx, _ = make_module(FakeProbes(sb=NOT_STARTED, now=clock), now=clock)
    m.run(["boot"])
    f = _b2(m)
    assert f.detail == boot.NOT_STARTED_BEFORE and m.diagnostics()["sb_b2"] == "cert_not_started"
    clock.set(datetime(2026, 10, 19, 0, 1))  # 日付が変わって期限を過ぎる
    m.run(["boot"])
    assert _b2(m).detail == boot.NOT_STARTED_AFTER


def test_fake_probes_default_today_is_fixed() -> None:
    assert FakeProbes().today() == FAKE_TODAY < boot.PCA2011_EXPIRY
