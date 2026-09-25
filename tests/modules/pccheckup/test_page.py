# 画面: 3つのボタンと「まとめて診断」(FR-1)・診断中の無効化と進み具合(FR-2)・悪い順と問題なしの折りたたみ(FR-3)・
# 字形と文字での status 表示(P-1)・結果のコピー(FR-6)・横幅(minimumSizeHint が 900 以下)・見張りのスイッチ。
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QApplication, QLabel

from deskkit.modules.pccheckup.checks.base import STATUS_TEXT, GiB
from deskkit.modules.pccheckup.fakes import FakeProbes
from deskkit.modules.pccheckup.page import BigButton, FindingCard, PcCheckupPage, StatusBadge
from deskkit.modules.pccheckup.probes import CpuSample, DiskInfo, MemInfo, ProcUsage
from deskkit.modules.pccheckup.runner import BY_CATEGORY

MAX_W = 900


def _widths(page: PcCheckupPage) -> tuple[int, int]:
    page.resize(1000, 800)
    page.show()
    QApplication.processEvents()
    inner = page.widget()
    return page.minimumSizeHint().width(), inner.minimumSizeHint().width()


def _bad_probes() -> FakeProbes:
    return FakeProbes(cpu_value=CpuSample(97.0, tuple(ProcUsage(f"very-long-process-name-{i}.exe", 10.0) for i in range(5))),
                      mem=MemInfo(85.0, 16 * GiB, (ProcUsage("chrome.exe", 3 * GiB),)),
                      sysdrive=DiskInfo("C:\\", 100 * GiB, 1 * GiB), reboot=True, raise_on={"startup"})


def test_page_builds_idle_and_width(qapp: Any, make_module: Any) -> None:
    m, _ctx, _ = make_module()
    page = m.create_page()
    assert isinstance(page, PcCheckupPage)
    assert set(page.cat_btns) == {"perf", "net", "storage"}
    assert page.all_btn.text().endswith("まとめて診断")
    assert page.empty.isVisibleTo(page) and not page.progress_card.isVisibleTo(page)
    outer, inner = _widths(page)
    assert outer <= MAX_W and inner <= MAX_W, (outer, inner)
    page.close()


def test_page_results_sorted_collapsed_and_width(qapp: Any, make_module: Any) -> None:
    m, _ctx, _ = make_module(_bad_probes())
    page = m.create_page()
    m.run(list(BY_CATEGORY))
    QApplication.processEvents()
    sec = page.sections["perf"]
    order = [sec.main.itemAt(i).widget().finding.status for i in range(sec.main.count())]  # type: ignore[union-attr]
    assert order == sorted(order, key=lambda s: ["bad", "warn", "unknown", "info", "good"].index(s))
    assert order[0] == "bad" and "good" not in order
    assert sec.good_toggle.isVisibleTo(page) and not sec.good_box.isVisibleTo(page)
    assert "問題なしの項目(" in sec.good_toggle.text()
    sec.good_toggle.click()
    assert sec.good_box.isVisibleTo(page)
    badges = page.findChildren(StatusBadge)
    texts = {b.text.text() for b in badges}
    assert {STATUS_TEXT["bad"], STATUS_TEXT["warn"], STATUS_TEXT["unknown"]} <= texts
    labels = " ".join(lb.text() for lb in page.findChildren(QLabel))
    assert "very-long-process-name-0.exe" in labels
    outer, inner = _widths(page)
    assert outer <= MAX_W and inner <= MAX_W, (outer, inner)
    page.close()


def test_buttons_disabled_while_running(qapp: Any, make_module: Any) -> None:
    m, _ctx, _ = make_module()
    page = m.create_page()
    seen: list[tuple[bool, bool, bool]] = []

    def spy() -> None:
        seen.append((page.cat_btns["perf"].isEnabled(), page.all_btn.isEnabled(), page.progress_card.isVisibleTo(page)))

    m.notifier.finding.connect(spy)
    m.run(["perf"])
    assert seen and all(s == (False, False, True) for s in seen)
    assert page.cat_btns["perf"].isEnabled() and page.all_btn.isEnabled()
    assert not page.progress_card.isVisibleTo(page)
    page.close()


def test_big_button_click_runs(qapp: Any, make_module: Any) -> None:
    m, _ctx, _ = make_module()
    page = m.create_page()
    btn = page.cat_btns["net"]
    assert isinstance(btn, BigButton)
    btn.clicked.emit()
    assert m.state.categories == ["net"] and m.state.done == {"net"}
    page.close()


def test_copy_button_puts_text(qapp: Any, make_module: Any) -> None:
    m, _ctx, _ = make_module(_bad_probes())
    page = m.create_page()
    m.run(["perf"])
    assert page.copy_btn.isEnabled()
    page.copy_btn.click()
    text = QApplication.clipboard().text()
    assert "PcCheckup の診断結果" in text and "very-long-process-name-0.exe" in text
    page.close()


def test_worse_mark_shown(qapp: Any, make_module: Any) -> None:
    fp = FakeProbes()
    m, _ctx, _ = make_module(fp)
    m.run(["perf"])
    fp.reboot = True
    page = m.create_page()
    m.run(["perf"])
    labels = " ".join(lb.text() for lb in page.findChildren(QLabel))
    assert "前回より悪化" in labels
    page.close()


def test_page_recreated_restores_results(qapp: Any, make_module: Any) -> None:
    m, _ctx, _ = make_module(_bad_probes())
    m.run(["perf"])
    page = m.create_page()
    assert len(page.sections["perf"].cards) == 7
    assert page.history_rows() >= 1
    page.close()


def test_watch_toggle_saves(qapp: Any, make_module: Any) -> None:
    m, ctx, _ = make_module()
    page = m.create_page()
    page.watch_toggle.click()
    assert ctx.settings()["watch_disk"] is True
    assert page.watch_pill.isVisibleTo(page)
    page.close()


def test_action_buttons_open(qapp: Any, make_module: Any) -> None:
    m, _ctx, _ = make_module(FakeProbes(reboot=True, sense=False))
    page = m.create_page()
    m.run(["storage"])
    card = page.sections["storage"].cards["S6"]
    assert isinstance(card, FindingCard)
    page.on_action(card.finding.actions[0], page.copy_btn)
    assert m.opened == ["ms-settings:storagesense"]
    page.close()
