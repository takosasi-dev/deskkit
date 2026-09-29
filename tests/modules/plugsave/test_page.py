# 画面のテスト(offscreen)。いろいろな状態で作り直せること・初回の計画・結果の表・候補のダイアログ。
from __future__ import annotations

from typing import Any

from deskkit.modules.plugsave.module import Candidate

from .conftest import Env, ready_module, write


def test_page_empty_and_full(env: Env, qapp: Any) -> None:
    from deskkit.modules.plugsave.page import PlugSavePage

    m = env.make()
    m.start()
    p = PlugSavePage(m)
    assert not p.now_btn.isEnabled()
    assert not p.first_card.isVisibleTo(p)
    p.deleteLater()


def test_page_first_run_and_result(env: Env, qapp: Any) -> None:
    from deskkit.modules.plugsave.page import PlugSavePage

    e = env
    write(e.src / "a.txt", b"a")
    write(e.src / "~$b.docx", b"b")
    m, did = ready_module(e, first_done=False)
    p = PlugSavePage(m)
    assert p.first_card.isVisibleTo(p)
    p._preview(did)
    assert did in m.previews
    p._start_first(did)
    assert m.last is not None and m.last.result == "ok"
    assert p.table.rowCount() == 1
    assert "新しくコピー: 1 件" in p.res_summary.text()
    assert not p.first_card.isVisibleTo(p)
    assert p.t_last.value.text() == "今日"


def test_page_progress_and_waiting(env: Env, qapp: Any) -> None:
    from deskkit.modules.plugsave.page import PlugSavePage

    e = env
    write(e.src / "a.txt", b"a")
    m, did = ready_module(e)
    p = PlugSavePage(m)
    m._begin_wait("Q", did)
    assert p.prog_card.isVisibleTo(p) and p.cancel_btn.isVisibleTo(p)
    p._cancel()
    assert m.pending is None and not p.prog_card.isVisibleTo(p)
    held: list[Any] = []
    orig = m._spawn
    m._spawn = lambda fn, name: held.append(fn) if name == "plugsave-run" else orig(fn, name)
    m.start_backup(did, trigger="manual")
    m._on_progress(m.run.token, "copy", 1, 4, 10, 40)
    assert "コピーしています 1 / 4" in p.stage_label.text()
    assert m.status_text() == "バックアップ中 25%"
    held[0]()
    assert m.run is None


def test_page_no_space_buttons(env: Env, qapp: Any) -> None:
    from deskkit.modules.plugsave.page import PlugSavePage

    e = env
    write(e.src / "a.bin", b"a" * 100)
    m, did = ready_module(e)
    e.api.drives["Q"].free = 10
    m.start_backup(did, trigger="manual")
    assert m.last.result == "no_space"
    p = PlugSavePage(m)
    assert any("あと" in n.text for n in m.notices)
    assert p.notice_card.isVisibleTo(p)


def test_candidates_dialog(env: Env, qapp: Any) -> None:
    from deskkit.modules.plugsave.page import PlugSavePage

    m = env.make()
    m.start()
    p = PlugSavePage(m)
    dlg = p.candidates_dialog([Candidate("Q", "USB", "exFAT", 64 * 1024**3, 30 * 1024**3, "new", 2),
                               Candidate("R", "", "NTFS", 1, 1, "broken", 3)])
    assert dlg.body.count() == 2
    dlg.deleteLater()
    empty = p.candidates_dialog([])
    assert empty.body.count() == 1
    empty.deleteLater()
