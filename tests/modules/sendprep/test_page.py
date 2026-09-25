# 画面・エディタ・モジュールの結合(offscreen)。ページの横幅(900 以下)・ドロップ・プリセットの編集・完了のまとめ(FR-18)・
# 伏せ字エディタ(FR-10・11・13)・クリップボード(FR-19)・「送る」(FR-20・AC-11)・利用状況と診断(FR-22・23)・NFR-5。
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent, QGuiApplication, QImage
from PySide6.QtTest import QTest

from deskkit.modules.sendprep import jobs as J
from deskkit.modules.sendprep import sendto
from deskkit.modules.sendprep.ocr import Candidate, OcrUnavailableError
from deskkit.modules.sendprep.redact import MSG_OCR_UNAVAILABLE, RedactEditor

from .conftest import FakeLinks, jpeg_with_meta, png_with_meta, run_all


def _pump(qapp: Any, ms: int = 50) -> None:
    QTest.qWait(ms)


def test_page_width_and_drop(qapp: Any, make_module: Any, tmp_path: Path) -> None:
    m, ctx = make_module({"presets": [{"id": f"custom-{i}", "label": f"とても長いプリセットの名前その{i}", "limit_mb": 25}
                                      for i in range(1, 6)], "my_words": ["山田"]})
    page = m.create_page()
    assert page.minimumSizeHint().width() <= 900
    page.resize(900, 900)
    page.show()
    src = tmp_path / "x.jpg"
    src.write_bytes(jpeg_with_meta())
    (tmp_path / "memo.txt").write_text("x")
    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile(str(src)), QUrl.fromLocalFile(str(tmp_path / "memo.txt"))])
    ev = QDropEvent(QPointF(100, 100), Qt.DropAction.CopyAction, md, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    page.dropEvent(ev)
    assert len(m.jobs) == 1 and len(page._rows) == 1 and m.unsupported == 1
    assert page.notice_unsupported.text() == "対応していない形式: 1 件"
    assert "1 件" in page.go_btn.text()
    assert page.minimumSizeHint().width() <= 900
    # プリセットのチップ: 既定4つ + 追加5つ
    assert len(page.chip_group.buttons()) == 9
    page._select("discord")
    page._go()
    run_all(m, ctx)
    _pump(qapp)
    row = page._rows[m.jobs[0].id]
    assert m.jobs[0].state == J.STATE_DONE and row.edit_btn.isVisibleTo(page) and "位置情報なし" in row.status.text()
    assert page.summary.isVisibleTo(page) and page.summary_label.text() == "完了 1 件・失敗 0 件"
    assert m.config.last_preset == "discord"
    # 出力をまとめてコピー(CF_HDROP になる URL の一覧)
    page._copy_outputs()
    urls = QGuiApplication.clipboard().mimeData().urls()
    assert [Path(u.toLocalFile()) for u in urls] == [m.jobs[0].out_path]
    assert page.minimumSizeHint().width() <= 900
    page.close()


def test_preset_crud(make_module: Any) -> None:
    m, ctx = make_module()
    for i in range(5):
        assert m.add_preset(f"P{i}", 10 + i) is None
    assert m.add_preset("P6", 5) == "追加できるプリセットは 5 個までです"
    assert [p.id for p in m.config.custom_presets()] == [f"custom-{i}" for i in range(1, 6)]
    assert m.add_preset("x", 501) is not None or len(m.config.custom_presets()) == 5
    assert m.rename_preset("custom-1", "LINE") is None and m.config.preset("custom-1").label == "LINE"  # type: ignore[union-attr]
    assert m.set_preset_limit("custom-1", 0) is not None
    assert m.set_preset_limit("custom-1", 500) is None and m.config.preset("custom-1").limit_mb == 500  # type: ignore[union-attr]
    assert m.rename_preset("discord", "x") == "最初からあるプリセットは変えられません"
    m.start_processing("custom-2")
    assert m.delete_preset("custom-2") is None and m.config.preset("custom-2") is None
    assert m.delete_preset("meta") is not None
    ids = [p.id for p in m.config.presets]
    assert ids[:4] == ["meta", "discord", "mail", "small"]
    assert m.add_preset("again", 3) is None and m.config.custom_presets()[-1].id == "custom-6"
    assert m.set_my_words(["a" * 41]) is not None and m.set_my_words(["山田", "yamada"]) is None
    assert m.set_my_words([f"w{i}" for i in range(21)]) is not None


def test_settings_defaults_written(make_module: Any) -> None:
    m, ctx = make_module({"presets": "broken", "redact_style": "blur", "my_words": [1, "", "ok"]})
    sec = ctx.writes[0]
    assert [p["id"] for p in sec["presets"]] == ["meta", "discord", "mail", "small"]
    assert sec["redact_style"] == "fill" and sec["my_words"] == ["ok"] and sec["sendto_enabled"] is False
    assert sec["last_preset"] == "meta" and sec["rename_to_date"] is False


def _editor(qapp: Any, find: Any, clipboard: bool = False) -> tuple[RedactEditor, list[Any]]:
    img = Image.new("RGB", (400, 300), (240, 240, 240))
    applied: list[Any] = []
    ed = RedactEditor(None, img, accent="#F472B6", style="fill", on_apply=lambda r, s: applied.append((r, s)),
                      find=find, clipboard_mode=clipboard)
    ed.resize(800, 600)
    ed.show()
    return ed, applied


def test_editor_draw_click_and_candidates(qapp: Any) -> None:
    cands = [Candidate("email", (10, 10, 100, 20)), Candidate("phone", (10, 50, 100, 20))]
    ed, applied = _editor(qapp, lambda _img: cands)
    for _ in range(40):
        _pump(qapp, 25)
        if ed.ocr_state == "done":
            break
    assert ed.ocr_state == "done" and len(ed.canvas.candidates) == 2 and ed.canvas.count() == 0  # 自動では伏せない(S-7)
    assert not ed.apply_btn.isEnabled()
    c = ed.canvas
    # ドラッグで範囲を足す
    a = c.to_widget((200, 150, 1, 1)).topLeft().toPoint()
    b = c.to_widget((300, 250, 1, 1)).topLeft().toPoint()
    QTest.mousePress(c, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, a)
    QTest.mouseMove(c, b)
    QTest.mouseRelease(c, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, b)
    assert len(c.rects) == 1
    x, y, w, h = c.rects[0]
    assert abs(x - 200) <= 2 and abs(y - 150) <= 2 and abs(w - 100) <= 3 and abs(h - 100) <= 3
    # 候補をクリックすると伏せる範囲になる
    p = c.to_widget((50, 60, 1, 1)).topLeft().toPoint()
    QTest.mouseClick(c, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p)
    assert c.count() == 2
    # 伏せた範囲をクリックすると消える
    q = c.to_widget((250, 200, 1, 1)).topLeft().toPoint()
    QTest.mouseClick(c, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, q)
    assert len(c.rects) == 0 and c.count() == 1
    ed.adopt_btn.click()
    assert c.count() == 2
    ed.style_seg.set_value("mosaic", emit=True)
    ed.apply_btn.click()
    assert applied and applied[0][1] == "mosaic" and sorted(applied[0][0]) == sorted([cc.rect for cc in cands])
    ed.close()


def test_editor_fr13_when_ocr_unavailable(qapp: Any) -> None:
    def fail(_img: Any) -> list[Candidate]:
        raise OcrUnavailableError("no_language")

    ed, _ = _editor(qapp, fail)
    for _ in range(40):
        _pump(qapp, 25)
        if ed.ocr_state == "unavailable":
            break
    assert ed.ocr_state == "unavailable" and ed.note.text() == MSG_OCR_UNAVAILABLE and ed.note.isVisibleTo(ed)
    ed.canvas.add_rect((0, 0, 50, 50))
    assert ed.apply_btn.isEnabled()
    ed.close()


def test_open_editor_and_burn_via_module(qapp: Any, make_module: Any, tmp_path: Path) -> None:
    src = tmp_path / "s.png"
    Image.new("RGB", (200, 100), (250, 250, 250)).save(src)
    m, ctx = make_module(ocr_find=lambda _img, _w: [Candidate("email", (5, 5, 40, 10))])
    m.add_paths([src])
    m.start_processing("meta")
    run_all(m, ctx)
    job = m.jobs[0]
    m.open_editor(job)
    for _ in range(100):
        _pump(qapp, 20)
        ctx.drain()
        if job.id in m._editors:
            break
    ed = m._editors[job.id]
    for _ in range(50):
        _pump(qapp, 20)
        if ed.ocr_state == "done":
            break
    ed.canvas.adopt_all()
    ed.apply_btn.click()
    for _ in range(200):
        _pump(qapp, 20)
        ctx.drain()
        if job.id not in m._editors:
            break
    assert job.redactions == 1 and job.id not in m._editors
    rows = m.ops.read()
    assert rows[-1]["redactions"] == 1 and len(rows) == 2


def test_clipboard_flow(qapp: Any, make_module: Any) -> None:
    m, ctx = make_module()
    QGuiApplication.clipboard().setText("no image")
    assert m.open_clipboard_editor() is False and ctx.notifications[-1][1] == "クリップボードに画像がありません"
    q = QImage(120, 80, QImage.Format.Format_RGB32)
    q.fill(0xFFFFFF)
    QGuiApplication.clipboard().setImage(q)
    assert m.open_clipboard_editor() is True
    ed = m._clip_editor
    assert ed is not None and ed.clipboard_mode and ed.apply_btn.isEnabled()
    ed.canvas.add_rect((0, 0, 60, 40))
    ed.apply_btn.click()
    for _ in range(200):
        _pump(qapp, 20)
        ctx.drain()
        if m._clip_editor is None:
            break
    back = QGuiApplication.clipboard().image()
    assert back.pixelColor(10, 10).black() == 255 and back.pixelColor(100, 70).lightness() == 255
    row = m.ops.read()[-1]
    assert row["kind"] == "clipboard" and row["result"] == "ok" and row["redactions"] == 1
    assert m.usage(7)[0].per_day[-1] == 1
    assert [t.label for t in ctx.tray] == ["SendPrep を開く", "クリップボードの画像を整える"]


def test_ac11_sendto(make_module: Any, tmp_path: Path) -> None:
    links = FakeLinks()
    folder = tmp_path / "SendTo"
    m, ctx = make_module(shell_link=links, sendto_folder=folder)
    assert m.set_sendto(True) is None
    lnk = folder / sendto.LINK_NAME
    assert lnk.exists() and m.config.sendto_enabled and m.sendto_status() == sendto.STATUS_REGISTERED
    t, a = json.loads(lnk.read_text(encoding="utf-8"))
    assert a.endswith("sendprep open") and sendto.is_ours(t, a)
    assert m.set_sendto(False) is None and not lnk.exists() and not m.config.sendto_enabled
    # 自分が作っていない同名のショートカットは消さない・上書きしない
    lnk.write_text(json.dumps([r"C:\Other\other.exe", "x"]), encoding="utf-8")
    assert m.set_sendto(True) is not None and json.loads(lnk.read_text(encoding="utf-8"))[0] == r"C:\Other\other.exe"
    assert m.set_sendto(False) is None and lnk.exists()
    assert m.diagnostics()["sendto"] == sendto.STATUS_OTHER
    assert sendto.is_ours(r"C:\x\DeskKit.exe", "sendprep open") and not sendto.is_ours(r"C:\x\DeskKit.exe", "dropsort")
    assert sendto.is_ours(r"C:\py\pythonw.exe", r'"C:\r\run_deskkit.py" sendprep open')
    assert not sendto.is_ours(r"C:\py\pythonw.exe", r'"C:\r\evil.py" sendprep open')


def test_default_sendto_folder_is_isolated(tmp_path: Path) -> None:
    assert sendto.default_folder() == tmp_path / "home" / "SendTo"  # DESKKIT_HOME のときは本物の「送る」に触れない


@pytest.mark.win32_real
def test_ac11_real_shortcut(tmp_path: Path) -> None:
    from deskkit.modules.sendprep._win32 import RealShellLink

    api = RealShellLink()
    assert sendto.register(api, tmp_path) is None and sendto.status(api, tmp_path) == sendto.STATUS_REGISTERED
    other = tmp_path / "o"
    other.mkdir()
    api.create(sendto.link_path(other), r"C:\Windows\notepad.exe", "sendprep open", "C:\\", r"C:\Windows\notepad.exe", "x")
    assert sendto.unregister(api, other) == "not_ours" and sendto.link_path(other).exists()
    assert sendto.unregister(api, tmp_path) == "removed" and not sendto.link_path(tmp_path).exists()


def test_diagnostics_and_usage_shape(make_module: Any) -> None:
    m, ctx = make_module()
    d = m.diagnostics()
    assert {"ffmpeg", "h264_encoder", "ocr", "sendto", "presets"} <= set(d) and d["presets"] == 4
    assert all(isinstance(v, (str, int, bool)) for v in d.values())
    us = m.usage(30)
    assert [s.key for s in us] == ["prepared", "redacted"] and us[0].primary and len(us[0].per_day) == 30


def test_nfr5_create_does_not_import_heavy_libs(tmp_path: Path) -> None:
    code = (
        "import sys, logging\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "from deskkit.modules.sendprep import create\n"
        "class C:\n"
        "    name='sendprep'; data_dir=Path(sys.argv[1]); log=logging.getLogger('x')\n"
        "    def settings_dict(self): return {}\n"
        "    def write_settings(self, s, restart=False): pass\n"
        "    def add_tray_action(self, *a, **k): pass\n"
        "    def set_tray_status(self, t): pass\n"
        "    def show_page(self): pass\n"
        "m = create(C()); m.start()\n"
        "bad = [n for n in ('PIL', 'numpy', 'winrt', 'pi_heif', 'psutil') if n in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    r = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, timeout=60,
                       cwd=str(Path(__file__).resolve().parents[3]))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ""


def test_png_meta_row_text(make_module: Any, tmp_path: Path) -> None:
    src = tmp_path / "a.png"
    src.write_bytes(png_with_meta())
    m, ctx = make_module()
    m.add_paths([src])
    m.start_processing("meta")
    run_all(m, ctx)
    assert m.jobs[0].result_text().startswith("位置情報なし・")
    # 終わった後に積むと、新しい一覧として始まる
    m.add_paths([src])
    assert len(m.jobs) == 1 and m.jobs[0].state == J.STATE_PENDING and m.summary is None
    _ = QPoint


def test_selftest_passes(capsys: pytest.CaptureFixture[str]) -> None:
    from deskkit.modules.sendprep.selftest import run

    assert run() == 0
    assert "合格" in capsys.readouterr().out
