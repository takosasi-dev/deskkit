# 画像のページ化(P-8・P-9・AC-8・AC-9)と描画スレッド(P-2・INV-2・AC-13、FR-7)。
from __future__ import annotations

import io
import threading
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader

from deskkit.modules.pagepress import images
from tests.modules.pagepress import samples
from tests.modules.pagepress.conftest import FakeBackend, FakeCtx, add_and_wait, make_module, pump, run_job


def _page_jpeg(r: PdfReader, i: int = 0) -> bytes:
    xo = r.pages[i]["/Resources"]["/XObject"]
    obj = next(iter(xo.values())).get_object()
    assert obj["/Filter"] == "/DCTDecode"
    return bytes(obj.get_data())


def _render(pdf: Path) -> Any:
    import pypdfium2 as pdfium  # テストだけで使う(本体は render.py だけ)

    doc = pdfium.PdfDocument(str(pdf))
    try:
        page = doc[0]
        return page.render(scale=0.5).to_pil().convert("RGB")
    finally:
        doc.close()


def test_strip_jpeg_keeps_scan_bytes() -> None:
    src = samples.jpeg_bytes(120, 80, orientation=6, gps=True, comment=b"memo")
    segs = images.jpeg_segments(src)
    assert 0xE1 in segs and 0xFE in segs
    out = images.strip_jpeg(src)
    assert 0xE1 not in images.jpeg_segments(out) and 0xFE not in images.jpeg_segments(out)
    sos = src.index(b"\xff\xda")
    assert out.endswith(src[sos:])  # 圧縮されたデータは1バイトも変えない


def test_jpeg_orientation6_ac8(ctx: FakeCtx, tmp_path: Path) -> None:
    m = make_module(ctx, tmp_path)
    m.start()
    photo = tmp_path / "photo.jpg"
    src = samples.jpeg_bytes(400, 300, orientation=6, gps=True)  # 横長の記録・時計回りに回して見る
    photo.write_bytes(src)
    add_and_wait(m, ctx, "merge", [photo])
    st = run_job(m, ctx, "merge")
    out = st.rows[0].path
    r = PdfReader(io.BytesIO(out.read_bytes()))
    data = _page_jpeg(r)
    assert 0xE1 not in images.jpeg_segments(data)
    # APP1 を除いた部分が元と同じバイト列
    assert data == images.strip_jpeg(src)
    assert b"N\x00\x00\x00" not in data
    pg = r.pages[0]
    w, h = float(pg.mediabox.width), float(pg.mediabox.height)
    assert h > w  # A4 縦
    im = _render(out)
    assert im.height > im.width
    # 左上の赤い印は、時計回りに回すと右上に来る
    x0 = int(im.width * 0.80)
    y0 = int(im.height * 0.15)
    rr, gg, bb = im.getpixel((x0, y0))
    assert rr > 200 and gg < 80 and bb < 80
    m.stop()


@pytest.mark.parametrize("orientation", [2, 3, 4, 5, 7, 8])
def test_other_orientations_render_upright(orientation: int, tmp_path: Path) -> None:
    p = tmp_path / f"o{orientation}.jpg"
    p.write_bytes(samples.jpeg_bytes(400, 300, orientation=orientation))
    pl = images.prepare(p)
    if orientation in (3, 8):
        assert pl.orientation == orientation and pl.data == images.strip_jpeg(p.read_bytes())
    else:
        assert pl.orientation == 1  # Pillow で向きを直して作り直した
        assert 0xE1 not in images.jpeg_segments(pl.data)
    disp = pl.display_size
    assert disp == ((300, 400) if orientation in (5, 6, 7, 8) else (400, 300))


def test_placement_ac9() -> None:
    port = images.place((1000, 2000), None, "paper", "a4", 10)
    land = images.place((2000, 1000), None, "paper", "a4", 10)
    m = 10 * 72 / 25.4
    assert (port.page_w, port.page_h) == images.A4 and (land.page_w, land.page_h) == (images.A4[1], images.A4[0])
    for pl in (port, land):
        assert pl.x >= m - 1e-6 and pl.y >= m - 1e-6
        assert pl.x + pl.w <= pl.page_w - m + 1e-6 and pl.y + pl.h <= pl.page_h - m + 1e-6
        assert abs((pl.page_w - pl.w) / 2 - pl.x) < 1e-6  # 真ん中に寄せる
    small = images.place((10, 10), None, "paper", "letter", 0)
    assert small.page_w == 612 and abs(small.w - 612) < 1e-6  # 小さい画像も拡大、正方形は縦向き
    orig = images.place((960, 480), 96, "original", "a4", 10)
    assert (orig.page_w, orig.page_h) == (720.0, 360.0)
    odd = images.place((960, 480), 50, "original", "a4", 10)  # 72 未満は 96 とみなす
    assert odd.page_w == 720.0
    huge = images.place((400000, 1000), 96, "original", "a4", 10)
    assert max(huge.page_w, huge.page_h) == pytest.approx(14400.0)


def test_merge_images_pages_ac9(ctx: FakeCtx, tmp_path: Path) -> None:
    m = make_module(ctx, tmp_path)
    m.start()
    a = tmp_path / "tall.jpg"
    a.write_bytes(samples.jpeg_bytes(300, 600))
    b = tmp_path / "wide.png"
    b.write_bytes(samples.png_bytes(600, 300))
    c = tmp_path / "gray16.png"
    c.write_bytes(samples.png_bytes(100, 100, mode="I;16"))
    add_and_wait(m, ctx, "merge", [a, b, c])
    assert m.has_images()
    st = run_job(m, ctx, "merge")
    r = PdfReader(io.BytesIO(st.rows[0].path.read_bytes()))
    sizes = [(round(float(p.mediabox.width)), round(float(p.mediabox.height))) for p in r.pages]
    assert sizes == [(595, 842), (842, 595), (595, 842)]
    wide = r.pages[1]["/Resources"]["/XObject"]["/Im0"].get_object()
    assert wide["/Filter"] == "/FlateDecode" and wide["/ColorSpace"] == "/DeviceRGB"
    pil = r.pages[1].images[0].image
    assert pil.getpixel((pil.width - 5, 5))[:3] == (255, 255, 255)  # 透明は白
    g = r.pages[2]["/Resources"]["/XObject"]["/Im0"].get_object()
    assert g["/ColorSpace"] == "/DeviceGray" and g["/BitsPerComponent"] == 8
    m.stop()


def test_merge_letter_no_margin_original(ctx: FakeCtx, tmp_path: Path) -> None:
    m = make_module(ctx, tmp_path)
    m.start()
    m.set_option("image_fit", "original")
    a = tmp_path / "a.jpg"
    a.write_bytes(samples.jpeg_bytes(960, 480))
    add_and_wait(m, ctx, "merge", [a])
    st = run_job(m, ctx, "merge")
    r = PdfReader(io.BytesIO(st.rows[0].path.read_bytes()))
    assert round(float(r.pages[0].mediabox.width)) == 720  # JFIF の 1:1 は dpi なし → 96
    m.stop()


# ------------------------------------------------------------------ 描画スレッド(AC-13)
def test_renderer_thread_only_and_drops_outside_ac13(tmp_path: Path) -> None:
    from deskkit.modules.pagepress.render import Renderer

    gate = threading.Event()
    be = FakeBackend(gate)
    delivered: list[Any] = []
    got: list[int] = []
    r = Renderer(delivered.append, backend_factory=lambda: be)
    r.open_doc(1, tmp_path / "x.pdf")
    r.want_pages(1, [0, 1, 2, 3], 160, lambda i, img: got.append(i))
    assert be.started.wait(5)  # 0 ページ目を描いている途中
    r.want_pages(1, [50, 51], 160, lambda i, img: got.append(i))  # 見えている範囲が変わった
    gate.set()
    for _ in range(200):
        if len(delivered) >= 3:
            break
        threading.Event().wait(0.02)
    for fn in list(delivered):
        fn()
    assert got == [0, 50, 51]  # 1〜3 は捨てられて描かれない
    assert be.rendered == [0, 50, 51]
    tids = {tid for _f, tid, _p in be.calls}
    assert tids == {r.thread_id} and threading.get_ident() not in tids
    r.close_doc(1)
    assert r.stop(5)
    assert ("close", r.thread_id, None) in be.calls


def test_renderer_failed_page_and_close_during_render(tmp_path: Path) -> None:
    from deskkit.modules.pagepress.render import Renderer

    be = FakeBackend(fail_pages=frozenset({1}))
    out: list[tuple[int, Any]] = []
    delivered: list[Any] = []
    r = Renderer(delivered.append, backend_factory=lambda: be)
    r.open_doc(7, tmp_path / "x.pdf")
    r.want_pages(7, [0, 1], 160, lambda i, img: out.append((i, img)))
    for _ in range(200):
        if len(delivered) >= 2:
            break
        threading.Event().wait(0.02)
    for fn in delivered:
        fn()
    assert out[0][1] is not None and out[1] == (1, None)  # 描けないページは None(「表示できません」)
    r.stop(5)


def test_renderer_load_failure(tmp_path: Path) -> None:
    from deskkit.modules.pagepress.render import STATE_LOAD_FAILED, Renderer

    def boom() -> Any:
        raise ImportError("no pdfium")

    delivered: list[Any] = []
    out: list[Any] = []
    r = Renderer(delivered.append, backend_factory=boom)
    r.open_doc(1, tmp_path / "x.pdf")
    r.want_pages(1, [0], 160, lambda i, img: out.append(img))
    for _ in range(200):
        if delivered:
            break
        threading.Event().wait(0.02)
    for fn in delivered:
        fn()
    assert out == [None] and r.state == STATE_LOAD_FAILED
    r.stop(5)


def test_real_pdfium_renders_in_thread(tmp_path: Path) -> None:
    """本物の PDFium で、描画スレッドの中で描ける(フォームの値も描く設定で開く)。"""
    from deskkit.modules.pagepress.render import Renderer

    pdf = samples.form_pdf(tmp_path / "f.pdf")
    delivered: list[Any] = []
    out: list[Any] = []
    r = Renderer(delivered.append)
    r.open_doc(1, pdf)
    r.want_pages(1, [0], 160, lambda i, img: out.append(img))
    r.request_first("k", pdf, "pdf", 96, out.append)
    for _ in range(500):
        if len(delivered) >= 2:
            break
        threading.Event().wait(0.02)
    for fn in delivered:
        fn()
    assert len(out) == 2 and all(img is not None and not img.isNull() for img in out)
    assert max(out[0].width(), out[0].height()) == 160
    assert r.pdfium_version.startswith("1")
    r.close_doc(1)
    r.stop(5)


def test_module_request_pages_window(ctx: FakeCtx, tmp_path: Path) -> None:
    m = make_module(ctx, tmp_path)
    m.start()
    pdf = samples.blank_pdf(tmp_path / "many.pdf", 100, labels=False)
    add_and_wait(m, ctx, "organize", [pdf])
    want = m.request_pages(40, 49)
    assert want[:10] == list(range(40, 50))
    assert want[10:26] == list(range(50, 66))
    assert want[26:] == list(range(39, 31, -1))
    pump(ctx, lambda: len(m.thumbs) >= len(want))
    assert m.page_thumb(40)[0]
    m.stop()
