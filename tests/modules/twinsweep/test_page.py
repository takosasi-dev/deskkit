# 画面のテスト(offscreen): 横幅(minimumSizeHint ≤ 900)・段階的な描画・G-5 のボタン・下の帯・プレビュー・フォルダの追加。
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from deskkit.modules.twinsweep.grouping import Photo
from deskkit.modules.twinsweep.module import TwinSweepModule
from deskkit.modules.twinsweep.results import ResultModel
from tests.modules.twinsweep.conftest import FakeCtx, base_image, save_jpeg, wait_idle


def pump(qapp: Any, ctx: FakeCtx, ms: int = 300) -> None:
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        ctx.drain()
        qapp.processEvents()
        time.sleep(0.005)


def settle(env: Any, timeout: float = 10.0) -> None:
    """段階的な描画が終わるまで流す。"""
    end = time.monotonic() + timeout
    pump(env.qapp, env.ctx, 50)
    while env.page._render_timer.isActive() and time.monotonic() < end:
        pump(env.qapp, env.ctx, 50)


def fake_model(n_groups: int, img: Path) -> ResultModel:
    photos: list[Photo] = []
    exact: list[list[Photo]] = []
    for g in range(n_groups):
        a = Photo(len(photos), str(img), str(img.parent), 1000 + g, 1, f"s{g}", g * 0x1234567, 800, 600, None, 1.0)
        photos.append(a)
        b = Photo(len(photos), str(img.parent / f"missing{g}.jpg"), str(img.parent), 1000 + g, 2, f"s{g}", g * 0x1234567,
                  800, 600, None, 1.0)
        photos.append(b)
        exact.append([a, b])
    return ResultModel.build(photos, exact, "normal", True)


@pytest.fixture
def page_env(qapp: Any, make_ctx: Callable[..., FakeCtx], tmp_path: Path,
             monkeypatch: pytest.MonkeyPatch) -> Any:
    from deskkit.ui import widgets as W

    msgs: list[str] = []
    monkeypatch.setattr(W, "message", lambda _p, title, text, kind="info": msgs.append(title))
    answers = {"ok": True}
    monkeypatch.setattr(W, "confirm", lambda *_a, **_k: (answers["ok"], []))
    ctx = make_ctx({})
    m = TwinSweepModule(ctx, env={"APPDATA": str(tmp_path / "fake_appdata")}, open_recycle_bin=lambda: None)
    m.start()
    page = m.create_page()
    page.resize(1000, 800)
    page.show()
    pump(qapp, ctx, 100)

    class Env:
        pass

    e = Env()
    e.ctx, e.m, e.page, e.msgs, e.answers, e.qapp = ctx, m, page, msgs, answers, qapp  # type: ignore[attr-defined]
    yield e
    page.close()
    page.deleteLater()
    m.stop()


def test_width_and_empty_state(page_env: Any) -> None:
    assert page_env.page.minimumSizeHint().width() <= 900
    assert page_env.page.recycle_btn.isEnabled() is False
    assert "選んだ 0 枚をごみ箱へ" in page_env.page.recycle_btn.text()


def test_groups_render_in_batches(page_env: Any, tmp_path: Path) -> None:
    img = save_jpeg(base_image(1, (400, 300)), tmp_path / "a.jpg")
    m, page = page_env.m, page_env.page
    m.model = fake_model(130, img)
    m.signals.results.emit()
    m.signals.selection.emit()
    settle(page_env)
    assert page._drawn_groups == 50
    assert page.more_btn.isVisible()
    assert "残り 80 グループ" in page.more_btn.text()
    assert page.minimumSizeHint().width() <= 900
    page._load_more()
    settle(page_env)
    assert page._drawn_groups == 100
    # 下の帯: 130 枚を選択中
    assert "選んだ 130 枚をごみ箱へ" in page.recycle_btn.text()
    assert page.recycle_btn.isEnabled()
    # サムネイルはメモリの LRU だけ(読めない写真は None)
    assert len(m.thumbs()) >= 1
    # G-5: グループの最後の「残す」の「ごみ箱へ」は押せない
    card = page._cards[0]
    keep_tile = next(t for t in card.tiles if m.model.is_keep(t.photo.pid))
    trash_tile = next(t for t in card.tiles if not m.model.is_keep(t.photo.pid))
    assert not keep_tile.toggle.trash_btn.isEnabled()
    assert trash_tile.toggle.trash_btn.isChecked()
    trash_tile.toggle.keep_btn.click()
    pump(page_env.qapp, page_env.ctx, 50)
    assert keep_tile.toggle.trash_btn.isEnabled()
    assert "選んだ 129 枚をごみ箱へ" in page.recycle_btn.text()
    keep_tile.toggle.trash_btn.click()
    trash_tile.toggle.trash_btn.click()  # もう片方が最後の「残す」なので切り替わらない
    pump(page_env.qapp, page_env.ctx, 50)
    assert m.model.invariant_ok()
    # FR-15: 送ったあとの描き直しでも、描いた数は保つ
    m.model.remove_photos([p.pid for p in m.model.selected()[:10]])
    m.signals.results.emit()
    settle(page_env)
    assert page._drawn_groups == 100


def test_preview_dialog(page_env: Any, tmp_path: Path) -> None:
    from deskkit.modules.twinsweep.page import PreviewDialog

    img = save_jpeg(base_image(2, (800, 600)), tmp_path / "b.jpg")
    m, page = page_env.m, page_env.page
    m.model = fake_model(1, img)
    m.signals.results.emit()
    pump(page_env.qapp, page_env.ctx, 300)
    photo = m.model.groups[0].photos[0]
    dlg = PreviewDialog(page, photo)
    dlg.show()
    pump(page_env.qapp, page_env.ctx, 800)
    assert dlg.image.pixmap() is not None and not dlg.image.pixmap().isNull()
    scr = page.screen().availableGeometry()
    assert dlg.width() <= scr.width() * 0.8 + 1
    keep = m.model.is_keep(photo.pid)
    other = next(p for p in m.model.groups[0].photos if p is not photo)
    if keep:
        assert not dlg.toggle.trash_btn.isEnabled()
        m.set_keep(other.pid, True)
        pump(page_env.qapp, page_env.ctx, 50)
        assert dlg.toggle.trash_btn.isEnabled()
        dlg.toggle.trash_btn.click()
        assert not m.model.is_keep(photo.pid)
    dlg.close()


def test_add_folder_paths(page_env: Any, tmp_path: Path) -> None:
    page, m = page_env.page, page_env.m
    bad = tmp_path / "fake_appdata" / "x"
    bad.mkdir(parents=True)
    assert page.try_add_folder(str(bad)) is False
    assert page_env.msgs[-1] == "このフォルダは調べられません"
    good = tmp_path / "photos"
    good.mkdir()
    assert page.try_add_folder(str(good)) is True
    assert page.folder_list.count() == 1
    assert page.try_add_folder(str(good)) is False  # 重複
    page_env.answers["ok"] = False
    assert page.try_add_folder(tmp_path.anchor) is False  # ドライブの直下: 確認で「いいえ」
    assert m.folders() == [str(good)]


def test_scan_from_page_shows_progress_and_results(page_env: Any, tmp_path: Path) -> None:
    page, m, ctx = page_env.page, page_env.m, page_env.ctx
    photos = tmp_path / "photos"
    img = base_image(7, (800, 600))
    save_jpeg(img, photos / "a.jpg")
    save_jpeg(img.resize((400, 300)), photos / "b.jpg")
    assert page.try_add_folder(str(photos))
    page.scan_btn.click()
    assert page.progress_card.isVisibleTo(page)
    wait_idle(ctx, m, qapp=page_env.qapp)
    pump(page_env.qapp, ctx, 400)
    assert not page.progress_card.isVisibleTo(page)
    assert m.model is not None and len(m.model.groups) == 1
    assert page._drawn_groups == 1
    assert page.t_files.value.text() == "2"
    assert page.minimumSizeHint().width() <= 900
