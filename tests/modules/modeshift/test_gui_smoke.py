# GUI の煙テスト: 偽の ctx と偽の OS 実装で、画面・プレビュー窓・編集フォームを組み立てて例外が出ないことを確かめる。
# offscreen で動かす(実機の電源・音量・アプリには触らない)。
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from deskkit.modules.modeshift.editor import ActionDialog, ProcessPicker, default_action  # noqa: E402
from deskkit.modules.modeshift.fakes import FakeCtx, fake_system, populate, sample_section  # noqa: E402
from deskkit.modules.modeshift.model import ACTION_TYPES  # noqa: E402
from deskkit.modules.modeshift.module import ModeShiftModule  # noqa: E402
from deskkit.modules.modeshift.preview import ModePicker, PreviewWindow  # noqa: E402
from deskkit.ui import theme  # noqa: E402


@pytest.fixture(scope="module")
def app() -> QApplication:
    a = QApplication.instance() or QApplication([])
    theme.apply(a)  # type: ignore[arg-type]
    return a  # type: ignore[return-value]


def _module(tmp_path: Path) -> tuple[ModeShiftModule, FakeCtx]:
    sysm = fake_system()
    populate(sysm)
    ctx = FakeCtx(tmp_path / "data", sample_section(tmp_path), games=frozenset({"game.exe"}))
    mod = ModeShiftModule(ctx, backends=sysm.backends())
    mod.start()
    assert mod.service is not None
    mod.service.worker.threaded = False
    return mod, ctx


def test_page_builds_and_refreshes(app: QApplication, tmp_path: Path) -> None:
    mod, ctx = _module(tmp_path)
    page = mod.create_page()
    page.resize(1200, 900)
    page.show()
    app.processEvents()
    # 未確認モードをトレイ入口で → プレビューが出る → 実行 → 結果表示
    mod.switch("game", source="tray")
    app.processEvents()
    assert mod._preview is not None
    win: PreviewWindow = mod._preview
    win._execute()
    app.processEvents()
    assert win.plan.finished
    assert mod.service is not None and mod.service.config.mode("game").confirmed  # type: ignore[union-attr]
    page._load_history()
    assert page.hist.rowCount() > 0
    # 編集画面: 行の選択・保存
    page.editor.modes.setCurrentRow(1)
    page.editor._set_field("label", "勉強モード")
    page.editor.save()
    assert ctx.settings_dict()["modes"][1]["label"] == "勉強モード"
    # undo のプレビュー
    mod.undo(dry_run=True, source="gui")
    app.processEvents()
    assert mod._preview is not None and mod._preview.plan.kind == "undo"
    mod._preview.close()
    page.close()
    mod.stop()


def test_action_dialogs_and_pickers(app: QApplication, tmp_path: Path) -> None:
    mod, _ctx = _module(tmp_path)
    for t in ACTION_TYPES:
        dlg = ActionDialog(None, mod, default_action(t))
        dlg._validate()
        dlg.result_action()
        dlg.close()
    ProcessPicker(None, mod, exclude_games=True, audio_first=True).close()
    picker = ModePicker(mod)
    picker.show_animated()
    app.processEvents()
    picker.close()
    mod.stop()
