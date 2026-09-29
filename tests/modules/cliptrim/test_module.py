# モジュールの流れ(同梱 ffmpeg と testsrc): 開く(FR-1・FR-2・§10)・位置とコマ送り(FR-3〜5)・区間(FR-7・FR-8)・切り方(FR-9)・
# 撮影場所などの情報の選択を覚える(回答 Q-2)・書き出しと通知(FR-12〜16)・CLI・「送る」(回答 Q-5)・usage・diagnostics・
# ログと ops.jsonl と diagnostics にファイル名が出ない(AC-13)。画面は offscreen で作って見るだけ(キー送信はしない)。
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deskkit.modules.cliptrim import sendto
from deskkit.modules.cliptrim.module import (
    MSG_CLOUD,
    MSG_END_BEFORE,
    MSG_NO_COPY,
    MSG_NO_VIDEO_STREAM,
    MSG_NOT_FOUND,
    MSG_OVERLAP,
    MSG_SUBS,
    MSG_TOO_MANY,
)

from .conftest import FakeCtx, FakeLinks, ListHandler, pump, run_ff

SECRET = "秘密の録画_ab12"


def _open(m: Any, ctx: FakeCtx, path: Path) -> None:
    assert m.open_video(path)
    pump(ctx, lambda: m.video is not None or bool(m.error), 60)
    assert m.video is not None, m.error
    pump(ctx, lambda: not m.reading and not m.frame_loading and m.around is not None, 60)


def _idle(m: Any, ctx: FakeCtx) -> None:
    pump(ctx, lambda: not m.reading and not m.frame_loading and not m.copy_pending(), 60)


def _mark(m: Any, ctx: FakeCtx, s: float, e_frame: float) -> str | None:
    m.seek(s)
    _idle(m, ctx)
    m.set_in()
    m.seek(e_frame)
    _idle(m, ctx)
    m.set_out()
    return m.add_segment()  # type: ignore[no-any-return]


@pytest.fixture
def work(samples: dict[str, Path], tmp_path: Path) -> dict[str, Path]:
    d = tmp_path / "videos"
    d.mkdir()
    out = {}
    for k, name in (("h264", f"{SECRET}.mp4"), ("multi", "multi.mkv"), ("rot", "rot.mov")):
        p = d / name
        p.write_bytes(samples[k].read_bytes())
        out[k] = p
    return out


def test_open_navigate_mark_and_export(make_module: Any, work: dict[str, Path], logs: ListHandler, tmp_path: Path) -> None:
    m, ctx = make_module()
    src = work["h264"]
    _open(m, ctx, src)
    assert m.duration == pytest.approx(120.0, abs=0.1) and m.frame_png is not None and m.frame_png.startswith(b"\x89PNG")
    assert m.video.container == (".mp4", "mp4")
    pump(ctx, lambda: all(t is not None for t in m.thumbs), 60)
    assert len(m.thumbs) == 24 and all(t.png for t in m.thumbs), [(t.index, t.target) for t in m.thumbs if not t.png]
    # コマ送り・秒・切れ目(FR-3・FR-4)
    m.seek(61.5)
    _idle(m, ctx)
    assert m.pos == pytest.approx(61.5, abs=1e-6)
    m.step_frame(1)
    _idle(m, ctx)
    assert m.pos == pytest.approx(61.5 + 1 / 30, abs=1e-6)
    m.step_frame(-1)
    m.step_frame(-1)
    _idle(m, ctx)
    assert m.pos == pytest.approx(61.5 - 1 / 30, abs=1e-6)
    m.step_key(-1)
    _idle(m, ctx)
    assert m.pos == pytest.approx(60.0, abs=1e-6)
    m.step_key(-1)  # 切れ目ちょうど: 1コマ前で読み直して前の切れ目へ
    _idle(m, ctx)
    assert m.pos == pytest.approx(58.0, abs=1e-6)
    m.step_key(1)
    _idle(m, ctx)
    assert m.pos == pytest.approx(60.0, abs=1e-6)
    m.step_frame(-1)  # 切れ目の1コマ前は前の GOP
    _idle(m, ctx)
    assert m.pos == pytest.approx(60.0 - 1 / 30, abs=1e-6)
    m.step_seconds(5)
    _idle(m, ctx)
    assert m.pos == pytest.approx(65.0 - 1 / 30, abs=1e-3)
    # 区間(FR-7): 終わりのコマを含む
    assert _mark(m, ctx, 61.5, 71.5 - 1 / 30) is None
    seg = m.segments[0]
    assert seg.start == pytest.approx(61.5, abs=1e-6) and seg.end == pytest.approx(71.5, abs=1e-6)
    _idle(m, ctx)
    assert seg.copy is not None and seg.copy.actual == pytest.approx(60.0, abs=1e-6) and seg.shift == pytest.approx(1.5)
    assert _mark(m, ctx, 65.0, 66.0) == MSG_OVERLAP
    m.seek(90.0)
    _idle(m, ctx)
    m.set_in()
    m.seek(80.0)
    _idle(m, ctx)
    m.set_out()
    assert m.add_segment() == MSG_END_BEFORE
    assert "見積もり" in m.estimate_text()
    # 書き出し(FR-12・FR-16)
    assert m.export() is None
    assert m.handle_cli(["open", str(src)]) == (1, "busy")
    pump(ctx, lambda: m.last_result is not None, 120)
    res = m.last_result
    assert res.result == "ok" and res.outputs[0].path == src.parent / f"{SECRET}_clip.mp4"
    assert ctx.notifications and ctx.notifications[-1][1] == "切り出しが終わりました(1 本)"
    rows = m.ops.read()
    assert rows[-1]["result"] == "ok" and rows[-1]["segments"] == 1 and rows[-1]["shift_max_s"] == pytest.approx(1.5)
    usage = {u.key: u for u in m.usage(7)}
    assert usage["clips"].primary and usage["clips"].per_day[-1] == 1 and usage["copy"].per_day[-1] == 1
    diag = m.diagnostics()
    assert diag["last_result"] == "ok" and diag["segments"] == 1 and diag["ffmpeg"] == "extracted"
    # AC-13: ログ・ops.jsonl・diagnostics に名前が出ない
    texts = logs.text() + (Path(ctx.data_dir) / "ops.jsonl").read_text(encoding="utf-8") + repr(diag)
    assert "秘密の録画" not in texts and "ab12" not in texts
    assert not ctx.errors


def test_limits_and_notes(make_module: Any, work: dict[str, Path]) -> None:
    m, ctx = make_module({"mode": "copy"})
    _open(m, ctx, work["multi"])
    assert ("info", MSG_SUBS) in m.mode_notes()  # AC-7
    m.segments = []
    from deskkit.modules.cliptrim.module import Segment

    m.segments = [Segment(i * 0.5, i * 0.5 + 0.2) for i in range(20)]
    m.mark_in, m.mark_out = 11.0, 11.5
    assert m.add_segment() == MSG_TOO_MANY


def test_strip_metadata_choice_is_remembered(make_module: Any, work: dict[str, Path]) -> None:
    m, ctx = make_module()
    assert m.config.strip_metadata is True  # 最初は「消す」
    assert ctx.writes and ctx.writes[0]["strip_metadata"] is True and "keep_metadata" not in ctx.writes[0]
    assert m.set_strip_metadata(False) is None
    assert ctx.settings()["strip_metadata"] is False
    m2, _ = make_module(ctx.settings())
    assert m2.config.strip_metadata is False


def test_open_errors(make_module: Any, work: dict[str, Path], tmp_path: Path, ffmpeg_exe: Path) -> None:
    m, ctx = make_module()
    assert not m.open_video(tmp_path / "nothing.mp4") and m.error == MSG_NOT_FOUND
    cloud, cctx = make_module(attributes=lambda _p: 0x00400000)
    assert not cloud.open_video(work["h264"]) and cloud.error == MSG_CLOUD
    wav = tmp_path / "videos" / "sound.m4a"
    run_ff(ffmpeg_exe, "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=2", "-c:a", "aac", "file:" + str(wav))
    assert m.open_video(wav)
    pump(ctx, lambda: bool(m.error), 60)
    assert m.error == MSG_NO_VIDEO_STREAM and m.video is None


def test_avi_forces_precise(make_module: Any, ffmpeg_exe: Path, tmp_path: Path) -> None:
    from .conftest import make_video

    src = make_video(ffmpeg_exe, tmp_path / "old.avi", seconds=4, codec="mpeg4", size="160x120", gop=25, audio=0,
                     fmt="avi")
    m, ctx = make_module({"mode": "copy"})
    _open(m, ctx, src)
    assert not m.copy_ok() and m.effective_mode() == "precise"
    assert ("warn", MSG_NO_COPY) in m.mode_notes()


def test_cli_open_and_discard_confirm(make_module: Any, work: dict[str, Path]) -> None:
    asked: list[str] = []
    m, ctx = make_module(confirm=lambda _t, text: asked.append(text) is not None and False)
    assert m.handle_cli(["open"]) == (2, "no path")
    assert m.handle_cli(["open", str(work["h264"]), str(work["rot"])]) == (0, "opened 1")
    assert ctx.shown == 1
    pump(ctx, lambda: m.video is not None, 60)
    assert m.video.path == work["h264"]
    _idle(m, ctx)
    m.mark_in, m.mark_out = 1.0, 2.0
    assert m.add_segment() is None
    assert not m.open_video(work["rot"])  # 「今の区間を捨てて開きますか」で「いいえ」
    assert asked and m.video.path == work["h264"]
    assert m.handle_cli(["close"]) == (2, "unsupported")


def test_sendto_register_only_own(make_module: Any, tmp_path: Path) -> None:
    links = FakeLinks()
    folder = tmp_path / "SendTo"
    m, ctx = make_module(shell_link=links, sendto_folder=folder)
    assert m.sendto_status() == sendto.STATUS_ABSENT
    assert m.set_sendto(True) is None
    assert m.sendto_status() == sendto.STATUS_REGISTERED and ctx.settings()["sendto_enabled"] is True
    target, args = links.read(folder / sendto.LINK_NAME)  # type: ignore[misc]
    assert "cliptrim open" in args
    assert m.set_sendto(False) is None and not (folder / sendto.LINK_NAME).exists()
    # 同じ名前の他人のショートカットは消さない・上書きしない
    links.create(folder / sendto.LINK_NAME, "C:/other.exe", "x", "", "", "")
    assert m.set_sendto(True) is not None
    assert m.set_sendto(False) is None and (folder / sendto.LINK_NAME).exists()


def test_launch_spec_source_run() -> None:
    spec = sendto.launch_spec()
    assert spec.args.endswith("cliptrim open") and "run_deskkit.py" in spec.args
    assert sendto.is_ours(spec.target, spec.args)
    assert sendto.is_ours("C:/x/DeskKit.exe", "cliptrim open")
    assert not sendto.is_ours("C:/x/DeskKit.exe", "sendprep open")


def test_page_builds_and_shows_rows(make_module: Any, work: dict[str, Path], qapp: Any) -> None:
    m, ctx = make_module()
    page = m.create_page()
    assert not page.editor.isVisibleTo(page) and page.empty_card.isVisibleTo(page)
    _open(m, ctx, work["h264"])
    assert page.editor.isVisibleTo(page) and page.meta_seg.value() == "strip"
    assert "120" not in page.meta_label.text() and "2:00.0" in page.meta_label.text()
    pump(ctx, lambda: all(t is not None for t in m.thumbs), 60)
    assert page.filmstrip.pixmaps and all(p is not None and not p.isNull() for p in page.filmstrip.pixmaps)
    assert page.frame_view._pm is not None and not page.frame_view._pm.isNull()
    assert _mark(m, ctx, 61.5, 71.5 - 1 / 30) is None
    _idle(m, ctx)
    from PySide6.QtWidgets import QLabel

    texts = [w.text() for w in page.seg_card.findChildren(QLabel)]
    assert any("開始が 1.5 秒早まります" in t for t in texts), texts
    page.meta_seg.set_value("keep", emit=True)
    assert m.config.strip_metadata is False and ctx.settings()["strip_metadata"] is False
    page.deleteLater()
