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
