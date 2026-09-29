# まとめる・分ける・整理・軽くするを、ジョブのスレッドで最後まで動かす(AC-1〜AC-7・AC-10〜AC-12・AC-14・AC-15、FR-16・FR-17・FR-22)。
from __future__ import annotations

import hashlib
import io
import threading
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader

from deskkit.modules.pagepress import jobs as J
from deskkit.modules.pagepress import naming, ranges, sanitize
from tests.modules.pagepress import samples
from tests.modules.pagepress.conftest import FakeCtx, add_and_wait, job_done, make_module, pump, run_job


def _read(p: Path) -> PdfReader:
    return PdfReader(io.BytesIO(p.read_bytes()))


def _texts(r: PdfReader) -> list[str]:
    return [(pg.extract_text() or "").strip() for pg in r.pages]


def _outline_titles(r: PdfReader) -> list[str]:
    out: list[str] = []
    for o in r.outline:
        if not isinstance(o, list):
            out.append(str(o.title))
    return out


def _snapshot(paths: list[Path]) -> dict[str, tuple[str, int]]:
    return {str(p): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in paths}


# ------------------------------------------------------------------ まとめる
def test_merge_ac1(module: Any, ctx: FakeCtx, tmp_path: Path) -> None:
    a = samples.rich_pdf(tmp_path / "in" / "a.pdf", 3)
    b = samples.blank_pdf(tmp_path / "in" / "b.pdf", 2)
    before = _snapshot([a, b])
    add_and_wait(module, ctx, "merge", [a, b])
    assert [e.pages for e in module.lists["merge"]] == [3, 2]
    st = run_job(module, ctx, "merge")
    assert st.state == J.STATE_DONE, st.message
    row = st.rows[0]
    assert row.path == tmp_path / "in" / "a_まとめ.pdf"
    r = _read(row.path)
    assert len(r.pages) == 5
    assert _outline_titles(r) == [f"{samples.MARK}-bm1", f"{samples.MARK}-bm2"]
    root = r.trailer["/Root"]
    for k in ("/Names", "/OpenAction", "/Metadata", "/PageLabels", "/AA"):
        assert k not in root
    assert "/Info" not in r.trailer
    assert b"/EmbeddedFiles" not in row.path.read_bytes()
    assert sanitize.find_violation(r) is None
    assert _texts(r) == ["R1", "R2", "R3", "P1", "P2"]
    # 名前付きの行き先を指していたリンクは、ページの参照に直っている(§8)
    links = [x.get_object() for x in r.pages[0].get("/Annots", [])]
    dests = [lk["/Dest"] for lk in links if lk.get("/Subtype") == "/Link"]
    assert len(dests) == 1 and dests[0][0].idnum == r.pages[2].indirect_reference.idnum
    assert _snapshot([a, b]) == before  # INV-1
    assert not list((tmp_path / "in").glob("~pagepress-*.tmp"))
    assert module.pending.count() == 0
    rows = module.ops.read()
    assert rows[-1]["op"] == "merge" and rows[-1]["result"] == "ok" and rows[-1]["pages"] == 5 and rows[-1]["outputs"] == 1


def test_merge_same_file_twice_and_second_name(module: Any, ctx: FakeCtx, tmp_path: Path) -> None:
    a = samples.blank_pdf(tmp_path / "a.pdf", 2)
    add_and_wait(module, ctx, "merge", [a, a])
    assert len(module.lists["merge"]) == 2
    st = run_job(module, ctx, "merge")
    assert st.state == J.STATE_DONE
    assert len(_read(st.rows[0].path).pages) == 4
    st = run_job(module, ctx, "merge")
    assert st.rows[0].path.name == "a_まとめ (2).pdf"


def test_merge_too_many_pages_fr4(module: Any, ctx: FakeCtx, tmp_path: Path) -> None:
    a = samples.blank_pdf(tmp_path / "a.pdf", 2600, labels=False)
    add_and_wait(module, ctx, "merge", [a, a])
    assert module.start_job("merge") == J.MSG_TOO_MANY_PAGES
    assert module.job is None


def test_merge_field_warning_ac14(module: Any, ctx: FakeCtx, tmp_path: Path) -> None:
    a = samples.form_pdf(tmp_path / "a.pdf", "name", "A")
    b = samples.form_pdf(tmp_path / "b.pdf", "name", "B")
    c = samples.form_pdf(tmp_path / "c.pdf", "other", "C", xfa=True, signed=True)
    add_and_wait(module, ctx, "merge", [a, b])
    from deskkit.modules.pagepress.module import MSG_FIELDS_SAME, MSG_SIG, MSG_XFA

    assert module.warnings("merge") == [MSG_FIELDS_SAME]
    # 「やめる」: 画面は start_job を呼ばない → 何も書かれない
    assert not list(tmp_path.glob("*_まとめ*.pdf"))
    module.clear("merge")
    add_and_wait(module, ctx, "merge", [c])
    assert module.warnings("merge") == [MSG_XFA, MSG_SIG]
    st = run_job(module, ctx, "merge")
    r = _read(st.rows[0].path)
    assert "/XFA" not in r.trailer["/Root"]["/AcroForm"]
    assert sanitize.find_violation(r) is None  # 入力欄の /AA も外れている
    assert "name" not in (r.get_fields() or {}) and "other" in (r.get_fields() or {})


def test_merge_bad_input_fails_whole_fr22(module: Any, ctx: FakeCtx, tmp_path: Path) -> None:
    a = samples.blank_pdf(tmp_path / "a.pdf", 1)
    b = samples.blank_pdf(tmp_path / "b.pdf", 1)
    add_and_wait(module, ctx, "merge", [a, b])
    b.write_bytes(b"%PDF-1.4 broken")  # 確かめたあとで壊れた(§10)
    st = run_job(module, ctx, "merge")
    assert st.state == J.STATE_FAILED and st.failed_index == 1
    assert not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob("*_まとめ*"))


# ------------------------------------------------------------------ 暗号化・壊れ(FR-2・AC-4)
def test_encrypted_and_broken_rejected_ac4(module: Any, ctx: FakeCtx, tmp_path: Path) -> None:
    e1 = samples.encrypted_pdf(tmp_path / "user.pdf", user="pw")
    e2 = samples.encrypted_pdf(tmp_path / "owner_only.pdf", user="")
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf at all")
    img = tmp_path / "bad.jpg"
    img.write_bytes(b"\xff\xd8garbage")
    gif = tmp_path / "x.gif"
    gif.write_bytes(b"GIF89a")
    add_and_wait(module, ctx, "merge", [e1, e2, bad, img, gif])
    assert module.lists["merge"] == []
    notes = module.notes["merge"]
    from deskkit.modules.pagepress import reader

    msgs = [r.message for r in notes.rejected]
    assert msgs.count(reader.MSG_ENCRYPTED) == 2
    assert reader.MSG_UNREADABLE_PDF in msgs and reader.MSG_BAD_IMAGE in msgs
    assert notes.unsupported == 1


def test_limits_200_items(module: Any, ctx: FakeCtx, tmp_path: Path) -> None:
    a = samples.blank_pdf(tmp_path / "a.pdf", 1)
    add_and_wait(module, ctx, "merge", [a] * 205)
    assert len(module.lists["merge"]) == 200
    assert [r.message for r in module.notes["merge"].rejected] == [J.MSG_OVER_COUNT] * 5


def test_limit_total_bytes(module: Any, ctx: FakeCtx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a = samples.blank_pdf(tmp_path / "a.pdf", 1)
    monkeypatch.setattr(J, "MAX_TOTAL_BYTES", a.stat().st_size * 2 + 1)
    add_and_wait(module, ctx, "merge", [a, a, a])
    assert len(module.lists["merge"]) == 2
    assert module.notes["merge"].rejected[0].message == J.MSG_OVER_BYTES


def test_folder_drop_natural_order_fr1(module: Any, ctx: FakeCtx, tmp_path: Path) -> None:
    d = tmp_path / "scan"
    for n in (10, 2, 1):
        samples.blank_pdf(d / f"p{n}.pdf", 1)
    (d / "memo.txt").write_text("x", encoding="utf-8")
    sub = d / "sub"
    samples.blank_pdf(sub / "deep.pdf", 1)
    add_and_wait(module, ctx, "merge", [d])
    assert [e.path.name for e in module.lists["merge"]] == ["p1.pdf", "p2.pdf", "p10.pdf"]
    assert module.notes["merge"].unsupported == 1


# ------------------------------------------------------------------ 分ける(AC-5・AC-6・FR-22)
def test_split_ranges_ac5(module: Any, ctx: FakeCtx, tmp_path: Path) -> None:
    a = samples.blank_pdf(tmp_path / "doc.pdf", 10)
    small = samples.blank_pdf(tmp_path / "small.pdf", 4)
    add_and_wait(module, ctx, "split", [a, small])
    spans = ranges.parse("1-3, 5, 8-", 10)
    assert module.split_count(spans) == 3
    st = run_job(module, ctx, "split", spans=spans)
    ok = [r for r in st.rows if r.ok]
    bad = [r for r in st.rows if not r.ok]
    assert len(ok) == 1 and len(bad) == 1 and bad[0].text == ranges.MSG_OVER_ALL
    folder = ok[0].folder
    assert folder is not None and folder.name == "doc_分割"
    files = sorted(p.name for p in folder.iterdir())
    assert files == ["doc_1-3.pdf", "doc_5.pdf", "doc_8-10.pdf"]
    assert [len(_read(folder / f).pages) for f in files] == [3, 1, 3]
    assert _texts(_read(folder / "doc_8-10.pdf")) == ["P8", "P9", "P10"]
    assert not (tmp_path / "small_分割").exists()  # 失敗した入力のフォルダは残さない
    assert st.state == J.STATE_DONE
    assert module.ops.read()[-1]["code"] == "range_out"


def test_split_single_and_every_ac6(module: Any, ctx: FakeCtx, tmp_path: Path) -> None:
    a = samples.blank_pdf(tmp_path / "ten.pdf", 10)
    add_and_wait(module, ctx, "split", [a])
    module.set_option("split_mode", "single")
    assert module.split_count(None) == 10
    st = run_job(module, ctx, "split")
    assert len(list(st.rows[0].folder.iterdir())) == 10
    module.set_option("split_mode", "every")
    module.set_option("split_every", 3)
    assert module.split_count(None) == 4
    st = run_job(module, ctx, "split")
    folder = st.rows[0].folder
    assert folder.name == "ten_分割 (2)"
    names = sorted((p.name for p in folder.iterdir()), key=lambda s: int(s.split("_")[1].split("-")[0].split(".")[0]))
    assert names == ["ten_1-3.pdf", "ten_4-6.pdf", "ten_7-9.pdf", "ten_10.pdf"]
    assert [len(_read(folder / n).pages) for n in names] == [3, 3, 3, 1]


# ------------------------------------------------------------------ 整理(AC-7・AC-2)
def test_organize_ac7(module: Any, ctx: FakeCtx, tmp_path: Path) -> None:
    from pypdf import PdfWriter

    src = samples.blank_pdf(tmp_path / "four.pdf", 4)
    w = PdfWriter(clone_from=str(src))  # テストの検体づくりだけ(本体は clone_from を使わない)
    w.pages[0].rotate(90)
    w.write(str(src))
    add_and_wait(module, ctx, "organize", [src])
    st_ = module.organize_state
    assert st_ is not None and len(st_.items) == 4
    st_.remove([1])            # 2 ページ目を抜く → [1,3,4]
    st_.rotate([0], 90)        # 1 ページ目を右に 1 回
    st_.move([1], 0)           # 3 を先頭へ → [3,1,4]
    assert st_.items == [(2, 0), (0, 90), (3, 0)]
    assert module.organize_dirty()
    st = run_job(module, ctx, "organize")
    assert st.state == J.STATE_DONE, st.message
    r = _read(st.rows[0].path)
    orig = _read(src)
    assert len(r.pages) == 3
    assert r.pages[1].rotation == (orig.pages[0].rotation + 90) % 360 == 180
    assert _texts(r) == ["P3", "P1", "P4"]
    for out_i, src_i in ((0, 2), (1, 0), (2, 3)):
        assert r.pages[out_i].get_contents().get_data() == orig.pages[src_i].get_contents().get_data()
    assert not module.organize_dirty()
    assert st.rows[0].path.name == "four_整理.pdf"


def test_organize_edit_during_save_stays_dirty(ctx: FakeCtx, tmp_path: Path) -> None:
    m = make_module(ctx, tmp_path)
    m.start()
    gate = threading.Event()
    entered = threading.Event()

    def on_write() -> None:
        entered.set()
        gate.wait(10)

    m.env.on_write = on_write
    add_and_wait(m, ctx, "organize", [samples.blank_pdf(tmp_path / "a.pdf", 3)])
    m.organize_state.rotate([0], 90)
    assert m.start_job("organize") is None
    assert entered.wait(10)
    m.organize_state.rotate([1], 90)  # 保存の途中で変えた
    gate.set()
    pump(ctx, lambda: job_done(m))
    assert m.job.state == J.STATE_DONE and m.organize_dirty()
    m.organize_state.undo()
    assert not m.organize_dirty()  # 保存した並びに戻れば変更なし
    m.stop()


def test_organize_keeps_only_uri_ac2(module: Any, ctx: FakeCtx, tmp_path: Path) -> None:
    src = samples.annots_pdf(tmp_path / "links.pdf", 4)
    add_and_wait(module, ctx, "organize", [src])
    st = run_job(module, ctx, "organize")
    r = _read(st.rows[0].path)
    annots = [a.get_object() for a in r.pages[0].get("/Annots", [])]
    kinds = [(a.get("/Subtype"), (a.get("/A") or {}).get("/S")) for a in annots]
    assert ("/Link", "/URI") in kinds
    assert all(k[1] != "/JavaScript" and k[1] != "/Launch" and k[0] != "/FileAttachment" for k in kinds)
    assert "/AA" not in r.pages[0]
    # 2 ページ目を抜くと、2 ページ目へのリンクも外れる(§8)
    module.organize_state.remove([1])
    st = run_job(module, ctx, "organize")
    r = _read(st.rows[0].path)
    annots = [a.get_object() for a in r.pages[0].get("/Annots", [])]
    assert [(a.get("/A") or {}).get("/S") for a in annots] == ["/URI"]


# ------------------------------------------------------------------ 検査(AC-3)と中止(AC-12)
def test_verify_failure_removes_tmp_ac3(ctx: FakeCtx, tmp_path: Path) -> None:
    m = make_module(ctx, tmp_path)
    m.start()
    m.env.clean = lambda w: None  # わざと掃除しない
    a = samples.rich_pdf(tmp_path / "a.pdf", 2)
    add_and_wait(m, ctx, "merge", [a])
    st = run_job(m, ctx, "merge")
    assert st.state == J.STATE_FAILED and st.message == J.MSG_VERIFY
    assert not list(tmp_path.glob("~pagepress-*.tmp")) and not list(tmp_path.glob("a_まとめ*"))
    assert m.pending.count() == 0
    assert m.ops.read()[-1]["result"] == "verify_failed"
    m.stop()


def test_cancel_during_write_ac12(ctx: FakeCtx, tmp_path: Path) -> None:
    m = make_module(ctx, tmp_path)
    m.start()
    gate = threading.Event()
    entered = threading.Event()

    def on_write() -> None:
        entered.set()
        gate.wait(10)

    m.env.on_write = on_write
    a = samples.blank_pdf(tmp_path / "a.pdf", 20)
    add_and_wait(m, ctx, "merge", [a])
    assert m.start_job("merge") is None
    assert entered.wait(10)
    assert list(tmp_path.glob("~pagepress-*.tmp"))  # 書いている途中
    assert m.pending.count() == 1
    assert m.start_job("split") == J.MSG_BUSY  # FR-13
    m.cancel_job()
    assert ctx.timers and ctx.timers[-1].ms == 1000
    ctx.timers[-1].cb()  # 1 秒たっても戻らない
    assert m.job_text() == J.MSG_CANCELLING
    gate.set()
    pump(ctx, lambda: job_done(m))
    assert m.job.state == J.STATE_CANCELLED and m.job_text() == J.MSG_CANCELLED
    assert not list(tmp_path.glob("~pagepress-*.tmp")) and not list(tmp_path.glob("a_まとめ*"))
    assert m.pending.count() == 0
    m.stop()


def test_start_sweeps_pending_ac12(ctx: FakeCtx, tmp_path: Path) -> None:
    own = tmp_path / naming.tmp_name()
    own.write_bytes(b"x")
    user = tmp_path / "user.pdf"
    user.write_bytes(b"x")
    naming.Pending(ctx.data_dir / "pending.json").add(own)
    naming.Pending(ctx.data_dir / "pending.json").add(user)
    m = make_module(ctx, tmp_path)
    m.start()
    assert not own.exists() and user.exists()
    m.stop()


def test_stop_during_job_cancels(ctx: FakeCtx, tmp_path: Path) -> None:
    m = make_module(ctx, tmp_path)
    m.start()
    gate = threading.Event()
    entered = threading.Event()

    def on_write() -> None:
        entered.set()
        gate.wait(0.5)

    m.env.on_write = on_write
    a = samples.blank_pdf(tmp_path / "a.pdf", 5)
    add_and_wait(m, ctx, "merge", [a])
    m.start_job("merge")
    assert entered.wait(10)
    m.stop()
    assert not list(tmp_path.glob("~pagepress-*.tmp")) and not list(tmp_path.glob("a_まとめ*"))


# ------------------------------------------------------------------ 空き容量・書けない・同じ実体(FR-16・FR-17・§10)
def test_disk_full_fr16(ctx: FakeCtx, tmp_path: Path) -> None:
    m = make_module(ctx, tmp_path, disk_free=lambda p: 10 * 1024 * 1024)
    m.start()
    a = samples.blank_pdf(tmp_path / "a.pdf", 2)
    add_and_wait(m, ctx, "merge", [a])
    st = run_job(m, ctx, "merge")
    assert st.state == J.STATE_FAILED and st.message.startswith("空き容量が足りません(あと ")
    assert m.ops.read()[-1]["code"] == "disk_full"
    assert not list(tmp_path.glob("~pagepress-*.tmp"))
    m.stop()


def test_denied_folder_falls_back(module: Any, ctx: FakeCtx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a = samples.blank_pdf(tmp_path / "ro" / "a.pdf", 1)
    real = J._make_tmp

    def fake(folder: Path) -> Path:
        if folder == tmp_path / "ro":
            raise PermissionError(13, "denied")
        return real(folder)

    monkeypatch.setattr(J, "_make_tmp", fake)
    add_and_wait(module, ctx, "merge", [a])
    st = run_job(module, ctx, "merge")
    assert st.state == J.STATE_DONE and st.rows[0].fallback
    assert st.rows[0].path.parent == tmp_path / "Documents" / "PagePress"


def test_same_entity_refused_fr17(module: Any, ctx: FakeCtx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a = samples.blank_pdf(tmp_path / "a.pdf", 1)
    add_and_wait(module, ctx, "merge", [a])
    monkeypatch.setattr(J.naming, "same_file", lambda x, y: True)
    st = run_job(module, ctx, "merge")
    assert st.state == J.STATE_FAILED and st.message == J.MSG_SAME
    assert a.exists() and not list(tmp_path.glob("~pagepress-*.tmp"))


def test_inputs_unchanged_all_ops_ac15(module: Any, ctx: FakeCtx, tmp_path: Path) -> None:
    pdf = samples.rich_pdf(tmp_path / "r.pdf", 4)
    img = tmp_path / "photo.jpg"
    img.write_bytes(samples.jpeg_bytes(300, 200, orientation=6, gps=True))
    imgpdf = samples.image_pdf(tmp_path / "scan.pdf", 1, w=1200, h=1700)
    paths = [pdf, img, imgpdf]
    before = _snapshot(paths)
    add_and_wait(module, ctx, "merge", [pdf, img])
    run_job(module, ctx, "merge")
    add_and_wait(module, ctx, "split", [pdf])
    module.set_option("split_mode", "single")
    run_job(module, ctx, "split")
    add_and_wait(module, ctx, "organize", [pdf])
    module.organize_state.rotate([0], 270)
    run_job(module, ctx, "organize")
    add_and_wait(module, ctx, "compress", [imgpdf])
    run_job(module, ctx, "compress")
    assert _snapshot(paths) == before


# ------------------------------------------------------------------ 軽くする(AC-10・AC-11)
def test_compress_ac10(module: Any, ctx: FakeCtx, tmp_path: Path) -> None:
    src = samples.image_pdf(tmp_path / "scan.pdf", 5)
    add_and_wait(module, ctx, "compress", [src])
    st = run_job(module, ctx, "compress")
    row = st.rows[0]
    assert row.ok and not row.warn, row.text
    assert row.path.name == "scan_軽量.pdf"
    assert row.size < src.stat().st_size * 0.5
    r = _read(row.path)
    for pg in r.pages:
        for im in pg.images:
            assert max(im.image.size) <= 1754
    assert "→" in row.text and "% 減" in row.text
    assert module.ops.read()[-1]["level"] == "normal"


def test_compress_not_smaller_ac11(module: Any, ctx: FakeCtx, tmp_path: Path) -> None:
    src = samples.blank_pdf(tmp_path / "text.pdf", 1, labels=False)
    add_and_wait(module, ctx, "compress", [src])
    st = run_job(module, ctx, "compress")
    row = st.rows[0]
    assert not row.ok and row.text == J.MSG_NOT_SMALLER
    assert not list(tmp_path.glob("text_軽量*")) and not list(tmp_path.glob("~pagepress-*"))
    assert module.ops.read()[-1]["result"] == "not_smaller"


def test_compress_levels_sizes(module: Any, ctx: FakeCtx, tmp_path: Path) -> None:
    src = samples.image_pdf(tmp_path / "b.pdf", 1)
    sizes = {}
    for lv in ("light", "normal", "strong"):
        module.set_option("compress_level", lv)
        add_and_wait(module, ctx, "compress", [src])
        st = run_job(module, ctx, "compress")
        sizes[lv] = st.rows[0].size
        module.clear("compress")
    assert sizes["light"] > sizes["normal"] > sizes["strong"]


def test_compress_skips_masked_and_small(tmp_path: Path) -> None:
    from pypdf import PdfWriter

    from deskkit.modules.pagepress import compress
    from deskkit.modules.pagepress.build import Control

    src = samples.image_pdf(tmp_path / "b.pdf", 1, w=1000, h=1400)
    w = PdfWriter()
    w.append(PdfReader(str(src)))
    (idnum, _), = compress.image_targets(w).items()
    obj = w.get_object(idnum)
    from pypdf.generic import BooleanObject, NameObject

    obj[NameObject("/Interpolate")] = BooleanObject(True)
    obj[NameObject("/SMask")] = obj.indirect_reference  # マスクがある画像は触らない
    stats = compress.shrink_images(w, "strong", Control(threading.Event()))
    assert stats.replaced == 0 and stats.candidates == 0


def test_split_folder_cleanup_on_cancel(ctx: FakeCtx, tmp_path: Path) -> None:
    m = make_module(ctx, tmp_path)
    m.start()
    calls = [0]
    gate = threading.Event()

    def on_write() -> None:
        calls[0] += 1
        if calls[0] > 50:
            gate.set()
            m.job.cancel.set()

    m.env.on_write = on_write
    a = samples.blank_pdf(tmp_path / "a.pdf", 10)
    add_and_wait(m, ctx, "split", [a])
    m.set_option("split_mode", "single")
    st = run_job(m, ctx, "split")
    assert st.state == J.STATE_CANCELLED
    assert not (tmp_path / "a_分割").exists()
    assert not list(tmp_path.rglob("~pagepress-*.tmp"))
    m.stop()
