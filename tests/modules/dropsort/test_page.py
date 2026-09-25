# Control Center 画面の組み立てテスト(offscreen)。例外なく作れて、各セクションが中身を表示できること。
from __future__ import annotations

from pathlib import Path

from deskkit.modules.dropsort import selftest as st
from deskkit.modules.dropsort.fakewin32 import FakeWin32
from deskkit.modules.dropsort.module import DropSortModule

from .helpers import FakeCtx, wait_worker

DL = st.DL


def test_page_builds_and_refreshes(qapp: object, tmp_path: Path) -> None:
    from deskkit.modules.dropsort.page import DropSortPage, DryRunDialog, RecordsDialog, RuleEditDialog

    fake = FakeWin32(DL)
    fake.mkdirs("C:\\Docs\\PDF")
    fake.add_volume("L:\\", "exFAT", serial=7)
    fake.mkdirs("L:\\X")
    rules = [st.rule("pdf", "C:\\Docs\\PDF", [".pdf"], mode="dry-run"), st.rule("bad", "C:\\Docs", None, name_regex="("),
             st.rule("ex", "L:\\X", [".txt"]), st.rule("gone", "U:\\X", [".zip"])]
    ctx = FakeCtx(tmp_path, {"watch_mode": "poll", "rules": rules})
    clock = st.FakeClock()
    m = DropSortModule(ctx, api=fake, clock=clock)
    m.start()
    try:
        m._kick()
        wait_worker(m, ctx)
        fake.add_file(DL + "\\a.pdf", 10, mtime=clock.t, zone=st.ZONE3)
        fake.add_file(DL + "\\b.pdf.exe", 10, mtime=clock.t)
        for _ in range(2):
            clock.advance(10)
            m._kick()
            wait_worker(m, ctx)
        page = m.create_page()
        assert isinstance(page, DropSortPage)
        page.resize(1200, 900)
        page.show()
        qapp.processEvents()  # type: ignore[attr-defined]
        for key in page.sections:
            page._switch(key)
            qapp.processEvents()  # type: ignore[attr-defined]
        assert page.dry_table.rowCount() == 1
        assert page.flag_table.rowCount() == 1
        assert page.rules_box.count() == 4
        assert page.t_flagged.value.text() == "1" and page.t_dry.value.text() == "1"
        page._existing_done_dry(m.service.sort_existing(False))
        dlg = RuleEditDialog(page, rules[0], set(), "ルールを編集")
        dlg.dest.setText("\\\\server\\share")
        assert "ネットワーク" in dlg.dest_state.text()
        dlg.dest.setText("C:\\Nope")
        assert dlg.mk.isHidden() is False
        DryRunDialog(m, None)
        RecordsDialog(None, "t", [], with_time=False)
        page.refresh_all()
        m.open_page("history")  # クイックアクション・通知からのセクション指定
        assert page._current == "history" and ctx.page_shown == 1
        assert not ctx.errors, ctx.errors
        page.close()
        page.deleteLater()
        qapp.processEvents()  # type: ignore[attr-defined]
    finally:
        m.stop()


def test_page_v02_tester_stats_template_and_modes(qapp: object, tmp_path: Path) -> None:
    from deskkit.modules.dropsort.oplog import JsonlLog
    from deskkit.modules.dropsort.page import RuleEditDialog

    fake = FakeWin32(DL)
    fake.mkdirs("C:\\Docs\\PDF")
    fake.mkdirs("C:\\Sorted")
    rules = [st.rule("pdf", "C:\\Docs\\PDF", [".pdf"], mode="dry-run"),
             st.rule("dated", "C:\\Sorted\\{yyyy}\\{ext}", [".zip"])]
    ctx = FakeCtx(tmp_path, {"watch_mode": "poll", "rules": rules, "pause_in_modes": ["old-mode"]})
    ctx.modes = [("game", "ゲーム"), ("work", "作業")]
    clock = st.FakeClock()
    dj = JsonlLog(tmp_path / "dryrun.jsonl", "d")
    for i in range(5):  # 8日前から試運転の予定が5件・問題なし → 本番化の提案
        dj.append({"op": "would_move", "src": "C:\\x", "rule": "pdf"}, clock.t - 8 * 86400 + i)
    m = DropSortModule(ctx, api=fake, clock=clock)
    m.start()
    try:
        m._kick()
        wait_worker(m, ctx)
        page = m.create_page()
        page.resize(1200, 900)
        page.show()
        wait_worker(m, ctx)  # 実績の集計
        qapp.processEvents()  # type: ignore[attr-defined]
        cards = page._rule_cards
        assert "試運転 14日で 5 件の予定" in cards[0].stats_label.text()
        assert not cards[0].promote.isHidden() and cards[1].promote.isHidden()
        assert "本番 14日で 0 件を移動" in cards[1].stats_label.text()
        # ルールを試す(D1)
        page.tt_name.setText("a.pdf")
        assert "pdf" in page.tt_result.text() and "試運転" in page.tt_result.text()
        assert "C:\\Docs\\PDF\\a.pdf" in page.tt_dest.text()
        page.tt_name.setText("b.zip")
        assert "dated" in page.tt_result.text() and "C:\\Sorted\\" in page.tt_dest.text()
        assert page.tt_dest.text().endswith("\\zip\\b.zip")
        page.tt_name.setText("x.pdf.exe")
        assert page.tt_result.text() == "動かしません"
        page.tt_size.setText("abc")
        assert "サイズ" in page.tt_result.text()
        # テンプレートの検査とプレビュー(D2)
        dlg = RuleEditDialog(page, rules[1], set(), "ルールを編集")
        assert not dlg.dest_preview.isHidden() and "例: C:\\Sorted\\" in dlg.dest_preview.text()
        assert "基準フォルダ" in dlg.dest_state.text() and "OK" in dlg.dest_state.text()
        dlg.dest.setText("C:\\Sorted\\{year}")
        assert "使えません" in dlg.dest_state.text() and dlg.dest_preview.isHidden()
        dlg.dest.setText("C:\\NoBase\\{yyyy}")
        assert not dlg.mk.isHidden() and dlg._target() == "C:\\NoBase"
        # モード連携(M3): 選択肢は ctx.list_modes と、設定にある未知の名前
        labels = [cb.text() for _n, cb in page._pm_boxes]
        assert labels[0].startswith("ゲーム") and "old-mode" in labels[-1]
        page._pm_boxes[0][1].setChecked(True)
        assert ctx._section["pause_in_modes"] == ["game", "old-mode"]
        ctx.fire("modeshift.switched", {"mode": "game", "run_id": "r", "failed": 0})
        page.refresh_light()
        assert "ゲーム" in page.pill_state.text()
        ctx.snoozed = True
        ctx.fire("modeshift.reverted", {"mode": "game", "run_id": "r"})
        page.refresh_light()
        assert "スヌーズ" in page.pill_state.text()
        assert not ctx.errors, ctx.errors
        page.close()
        page.deleteLater()
        qapp.processEvents()  # type: ignore[attr-defined]
    finally:
        m.stop()
