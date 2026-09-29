# 仕様書 §8 の実測(win32_real。既定では走らない)。検体はテストの中で作る(利用者のファイルは使わない)。
#   検体 A: 白紙に近い 2,000 ページ / 検体 B: 2480×3508 の JPEG(品質 90)を1枚ずつ置いたページを約 500MB
# 実行: python -m pytest tests/modules/pagepress/test_measure_real.py -m win32_real -s
#   PAGEPRESS_MEASURE_DIR に検体と出力を置く(既定は pytest の一時フォルダ)。結果は標準出力と <DIR>\results.jsonl。
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.modules.pagepress import samples

pytestmark = pytest.mark.win32_real
REPO = Path(__file__).resolve().parents[3]
B_TARGET = 500_000_000


@pytest.fixture(scope="module")
def mdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = os.environ.get("PAGEPRESS_MEASURE_DIR")
    p = Path(d) if d else tmp_path_factory.mktemp("pagepress-measure")
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture(scope="module")
def sample_a(mdir: Path) -> Path:
    p = mdir / "A" / "A_2000.pdf"
    if not p.exists():
        samples.blank_pdf(p, 2000, width=595.28, height=841.89)
    return p


@pytest.fixture(scope="module")
def sample_b(mdir: Path) -> Path:
    p = mdir / "B" / "B_500MB.pdf"
    if not p.exists():
        from pypdf import PdfWriter

        from deskkit.modules.pagepress import images

        w = PdfWriter()
        total = 0
        while total < B_TARGET:
            data = samples.jpeg_bytes(2480, 3508, quality=90, noise=True, marker=False)
            total += len(data)
            images.add_page(w, images.Payload(data, "/DCTDecode", "/DeviceRGB", 2480, 3508, 1, None),
                            images.place((2480, 3508), None, "paper", "a4", 0))
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            w.write(f)
    return p


def _record(mdir: Path, row: dict[str, Any]) -> None:
    print(json.dumps(row, ensure_ascii=False))
    with open(mdir / "results.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _child(mdir: Path, op: str, inputs: list[Path], tag: str) -> dict[str, Any]:
    out = subprocess.run([sys.executable, "-m", "tests.modules.pagepress.measure_ops", op, str(mdir / "ops" / tag),
                          *map(str, inputs)], capture_output=True, text=True, timeout=3600, cwd=str(REPO),
                         encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert out.returncode == 0, out.stderr[-2000:]
    row = json.loads(out.stdout.strip().splitlines()[-1])
    row["tag"] = tag
    _record(mdir, row)
    return row


def test_probe_a_b(mdir: Path, sample_a: Path, sample_b: Path) -> None:
    _child(mdir, "probe", [sample_a], "probe_A")
    _child(mdir, "probe", [sample_b], "probe_B")


def test_merge_a_b(mdir: Path, sample_a: Path, sample_b: Path) -> None:
    row = _child(mdir, "merge", [sample_a, sample_b], "merge_A+B")
    assert row["state"] == "done", row


def test_split_single(mdir: Path, sample_a: Path, sample_b: Path) -> None:
    _child(mdir, "split", [sample_a], "split_single_A")
    _child(mdir, "split", [sample_b], "split_single_B")


def test_compress_levels(mdir: Path, sample_a: Path, sample_b: Path) -> None:
    _child(mdir, "compress_normal", [sample_a], "compress_normal_A")
    for lv in ("light", "normal", "strong"):
        _child(mdir, f"compress_{lv}", [sample_b], f"compress_{lv}_B")


def test_readability_crops(mdir: Path) -> None:
    """文字を撮った 2480×3508 の JPEG 1 ページを3段階で軽くし、100% 表示(96dpi)の一部を PNG に保存する(Q-1 の材料)。"""
    import io

    import pypdfium2 as pdfium  # テストだけ(本体は render.py だけ)
    from PIL import Image, ImageDraw, ImageFont
    from pypdf import PdfReader, PdfWriter

    from deskkit.modules.pagepress import compress, images, sanitize
    from deskkit.modules.pagepress.build import Control

    im = Image.new("RGB", (2480, 3508), (250, 250, 247))
    d = ImageDraw.Draw(im)
    font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "YuGothM.ttc"
    for i, size in enumerate((36, 42, 50, 60)):
        try:
            f: Any = ImageFont.truetype(str(font_path), size)
        except OSError:
            f = ImageFont.load_default()
        y = 200 + i * 700
        for k in range(8):
            d.text((180, y + k * (size + 20)), f"{size // 4.17:.0f}pt 相当: 契約書の本文 第{k + 1}条 甲は乙に対し、金 1,234,567 円を支払う。",
                   fill=(20, 20, 20), font=f)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    data = buf.getvalue()
    w = PdfWriter()
    images.add_page(w, images.Payload(data, "/DCTDecode", "/DeviceRGB", 2480, 3508, 1, None),
                    images.place((2480, 3508), None, "paper", "a4", 0))
    src = mdir / "readability_src.pdf"
    with open(src, "wb") as fo:
        w.write(fo)
    sizes = {"src": src.stat().st_size}
    import threading

    for lv in ("light", "normal", "strong"):
        wr = PdfWriter()
        wr.append(PdfReader(str(src)))
        compress.shrink_images(wr, lv, Control(threading.Event()))
        compress.finish(wr)
        sanitize.clean(wr)
        out = mdir / f"readability_{lv}.pdf"
        with open(out, "wb") as fo:
            wr.write(fo)
        sizes[lv] = out.stat().st_size
        doc = pdfium.PdfDocument(str(out))
        page = doc[0]
        pil = page.render(scale=96 / 72).to_pil()
        pil.crop((40, 40, min(pil.width, 760), min(pil.height, 1100))).save(mdir / f"readability_{lv}_100pct.png")
        big = page.render(scale=192 / 72).to_pil()  # 200%(拡大して読むとき)
        big.crop((80, 80, 900, 700)).save(mdir / f"readability_{lv}_200pct.png")
        page.close()
        doc.close()
    _record(mdir, {"tag": "readability", "sizes": sizes})


def test_organize_visible_thumbs_fr12(mdir: Path, sample_a: Path, qapp: Any, tmp_path: Path) -> None:
    """検体 A を整理で開いてから、見えている分のサムネイルが揃うまで(FR-12・AC-21)と、GUI スレッドの最大の止まり。"""
    from PySide6.QtCore import QElapsedTimer, QObject, Qt, QTimer, Signal

    from deskkit.modules.pagepress.module import PagePressModule
    from tests.modules.pagepress.conftest import FakeCtx, FakeLink

    class Bridge(QObject):
        run = Signal(object)

    bridge = Bridge()
    bridge.run.connect(lambda fn: fn(), Qt.ConnectionType.QueuedConnection)

    class QtCtx(FakeCtx):
        def call_soon(self, fn: Any) -> None:  # 本物の host と同じく、どのスレッドからでも GUI スレッドへ送る
            bridge.run.emit(fn)

    ctx = QtCtx(tmp_path / "home" / "pagepress")
    m = PagePressModule(ctx, fallback_dir=lambda: tmp_path, shell_link=FakeLink(), sendto_folder=tmp_path / "S")
    m.start()
    page = m.create_page()
    page.resize(1100, 900)
    page.show()
    page.switch("organize", save=False)
    qapp.processEvents()
    gaps: list[int] = []
    clock = QElapsedTimer()
    clock.start()
    last = [clock.elapsed()]

    def beat() -> None:
        now = clock.elapsed()
        gaps.append(now - last[0])
        last[0] = now

    hb = QTimer()
    hb.setInterval(10)
    hb.timeout.connect(beat)
    hb.start()
    t0 = time.perf_counter()
    m.add_paths("organize", [sample_a])
    opened_at = None
    done_at = None
    while time.perf_counter() - t0 < 30:
        qapp.processEvents()
        if opened_at is None and m.organize_entry is not None:
            opened_at = time.perf_counter()
        if opened_at is not None:
            f, last_row = page.t_organize.view.visible_rows()
            if last_row >= f and all(m.page_thumb(m.organize_state.items[r][0])[0] for r in range(f, last_row + 1)):
                done_at = time.perf_counter()
                visible = last_row - f + 1
                break
        time.sleep(0.002)
    hb.stop()
    assert opened_at is not None and done_at is not None
    row = {"tag": "organize_A_visible", "probe_open_s": round(opened_at - t0, 2),
           "visible_after_open_s": round(done_at - opened_at, 2), "total_s": round(done_at - t0, 2),
           "visible_pages": visible, "max_gui_gap_ms": max(gaps) if gaps else 0}
    _record(mdir, row)
    m.stop()
    page.deleteLater()
    assert row["visible_after_open_s"] <= 2.0
