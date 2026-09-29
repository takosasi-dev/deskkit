# モジュールの口(トレイ・CLI・設定・利用状況・診断・送る)と、書かない物(INV-5・INV-6・AC-19・AC-20)、grep 系の AC(AC-16〜AC-18)。
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from deskkit.modules.pagepress import sendto
from tests.modules.pagepress import samples
from tests.modules.pagepress.conftest import FakeCtx, FakeLink, add_and_wait, make_module, pump, run_job

PKG = Path(__file__).resolve().parents[3] / "deskkit" / "modules" / "pagepress"


def test_create_start_tray_and_defaults(ctx: FakeCtx, tmp_path: Path) -> None:
    m = make_module(ctx, tmp_path)
    assert ctx.writes and ctx.writes[-1]["compress_level"] == "normal"  # 足りないキーを書き戻す
    m.start()
    assert [label for label, _cb in ctx.tray] == ["PDF をまとめる・分ける"]
    ctx.tray[0][1]()
    assert ctx.shown == 1 and ctx.status == "待機中"
    m.stop()


def test_import_is_light() -> None:
    import subprocess
    import sys

    code = ("import sys; import deskkit.modules.pagepress as p; from deskkit.modules.pagepress import module; "
            "print(int('pypdf' in sys.modules), int('pypdfium2' in sys.modules), int('PIL' in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                         cwd=str(PKG.parents[2]))
    assert out.stdout.strip() == "0 0 0", out.stderr


def test_handle_cli_open(ctx: FakeCtx, tmp_path: Path) -> None:
    m = make_module(ctx, tmp_path)
    m.start()
    a = samples.blank_pdf(tmp_path / "a.pdf", 1)
    code, msg = m.handle_cli(["open", str(a), str(a)])
    assert (code, msg) == (0, "queued 2") and ctx.shown == 1
    pump(ctx, lambda: m.probing["merge"] == 0)
    assert len(m.lists["merge"]) == 2
    code, msg = m.handle_cli(["open", *([str(a)] * 250)])
    assert code == 0 and msg == "queued 198"
    assert m.handle_cli(["status"]) == (2, "unsupported")
    m.stop()


def test_settings_normalized_write_back(tmp_path: Path) -> None:
    ctx = FakeCtx(tmp_path / "home" / "pagepress", {"paper": "a3", "margin_mm": 7, "last_tab": "organize"})
    m = make_module(ctx, tmp_path)
    assert m.config.paper == "a4" and m.config.margin_mm == 10 and m.config.last_tab == "organize"
    assert ctx.writes[-1]["paper"] == "a4"
    assert m.set_option("margin_mm", 0) is None and m.config.margin_mm == 0
    assert m.set_option("margin_mm", 3) is None and m.config.margin_mm == 10  # 合わない値は既定へ


def test_sendto_register_and_unregister(ctx: FakeCtx, tmp_path: Path) -> None:
    link = FakeLink()
    m = make_module(ctx, tmp_path, shell_link=link)
    folder = tmp_path / "SendTo"
    assert m.sendto_status() == sendto.STATUS_ABSENT
    assert m.set_sendto(True) is None
    lp = folder / sendto.LINK_NAME
    assert lp.exists() and m.config.sendto_enabled
    target, args = link.links[str(lp)]
    assert "pagepress open" in args and sendto.is_ours(target, args)
    assert m.sendto_status() == sendto.STATUS_REGISTERED
    assert m.set_sendto(False) is None and not lp.exists() and not m.config.sendto_enabled
    # 同じ名前の他人のショートカットは消さない・上書きしない
    lp.write_bytes(b"other")
    link.links[str(lp)] = ("C:\\Other\\app.exe", "x")
    assert m.set_sendto(True) is not None
    assert m.set_sendto(False) is None and lp.exists()


def test_sendto_launch_spec_source() -> None:
    spec = sendto.launch_spec()
    assert spec.args.endswith("pagepress open") and "run_deskkit.py" in spec.args
    assert sendto.is_ours("C:\\x\\DeskKit.exe", "pagepress open")
    assert not sendto.is_ours("C:\\x\\DeskKit.exe", "sendprep open")


def test_usage_and_diagnostics(module: Any, ctx: FakeCtx, tmp_path: Path) -> None:
    a = samples.blank_pdf(tmp_path / "a.pdf", 10)
    add_and_wait(module, ctx, "split", [a])
    module.set_option("split_mode", "every")
    module.set_option("split_every", 5)
    run_job(module, ctx, "split")
    src = samples.image_pdf(tmp_path / "scan.pdf", 1, w=1600, h=2200)
    add_and_wait(module, ctx, "compress", [src])
    run_job(module, ctx, "compress")
    us = {u.key: u for u in module.usage(7)}
    assert us["made"].per_day[-1] == 3 and us["made"].primary
    assert us["compressed"].per_day[-1] == 1
    d = module.diagnostics()
    assert d["jobs"] == 2 and d["jobs_done"] == 2 and d["pending"] == 0
    assert str(d["pypdf"]).startswith("6.")
    assert set(d) >= {"pdfium", "renderer", "thumbs", "pypdf_warnings", "sendto"}


# ------------------------------------------------------------------ 書かない物(AC-19・AC-20)
def test_no_marks_in_logs_ops_diag_ac19(ctx: FakeCtx, tmp_path: Path) -> None:
    mark = samples.MARK
    cap: list[str] = []

    class H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            cap.append(record.getMessage())
            if record.exc_info:
                cap.append(logging.Formatter().formatException(record.exc_info))

    h = H(logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(h)
    lg = logging.getLogger("deskkit.pagepress")
    old = lg.level
    lg.setLevel(logging.DEBUG)
    try:
        m = make_module(ctx, tmp_path)
        m.start()
        d = tmp_path / f"{mark}_dir"
        rich = samples.rich_pdf(d / f"{mark}_本文.pdf", 4, mark=mark)
        form = samples.form_pdf(d / f"{mark}_form.pdf", f"{mark}_field", f"{mark}_value")
        photo = d / f"{mark}_photo.jpg"
        photo.write_bytes(samples.jpeg_bytes(200, 300, orientation=6, gps=True))
        enc = samples.encrypted_pdf(d / f"{mark}_locked.pdf", user="pw")
        bad = d / f"{mark}_bad.pdf"
        bad.write_bytes(b"%PDF-1.4 broken")
        add_and_wait(m, ctx, "merge", [rich, form, photo, enc, bad])
        run_job(m, ctx, "merge")
        add_and_wait(m, ctx, "split", [rich])
        m.set_option("split_mode", "single")
        run_job(m, ctx, "split")
        add_and_wait(m, ctx, "organize", [rich])
        m.organize_state.rotate([0], 90)
        m.request_pages(0, 3)
        run_job(m, ctx, "organize")
        add_and_wait(m, ctx, "compress", [rich, form])
        run_job(m, ctx, "compress")
        m.row_thumb(m.lists["compress"][0])
        pump(ctx, lambda: len(m.thumbs) >= 1)
        diag = json.dumps(m.diagnostics(), ensure_ascii=False)
        usage = repr(m.usage(7))
        ops = (ctx.data_dir / "ops.jsonl").read_text(encoding="utf-8")
        m.stop()
    finally:
        root.removeHandler(h)
        lg.setLevel(old)
    blob = "\n".join(cap) + diag + usage + ops
    assert mark not in blob
    assert str(tmp_path) not in blob
    assert "@" not in ops
    for p in (ctx.data_dir / "ops.jsonl",):
        for line in p.read_text(encoding="utf-8").splitlines():
            assert set(json.loads(line)) == {"ts", "op", "result", "inputs", "images", "pages", "outputs", "in_bytes",
                                             "out_bytes", "level", "ms", "code"}


def test_pypdf_logger_isolated() -> None:
    from deskkit.modules.pagepress import reader

    reader.quiet_pypdf_logging()
    lg = logging.getLogger("pypdf")
    assert lg.propagate is False
    before = reader.pypdf_warning_count()
    lg.warning("secret %s", "C:\\Users\\x\\ZZMARK.pdf")
    assert reader.pypdf_warning_count() == before + 1


def test_no_new_files_outside_data_while_organizing_ac20(ctx: FakeCtx, tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    pdf = samples.blank_pdf(src_dir / "a.pdf", 30)
    temp = Path(os.environ.get("TEMP", str(tmp_path)))

    def listing(p: Path) -> set[str]:
        try:
            return {str(x) for x in p.rglob("*")} if p.exists() else set()
        except OSError:
            return set()

    watched = [tmp_path]
    before = {str(w): listing(w) for w in watched}
    temp_before = {x.name for x in temp.iterdir()} if temp.exists() else set()
    m = make_module(ctx, tmp_path)
    m.start()
    add_and_wait(m, ctx, "organize", [pdf])
    m.request_pages(0, 29)
    pump(ctx, lambda: len(m.thumbs) >= 30)
    m.organize_state.rotate([0, 1], 90)
    m.close_organize()
    m.stop()
    for w in watched:
        new = listing(w) - before[str(w)]
        assert all(str(ctx.data_dir) in x for x in new), new
    temp_after = {x.name for x in temp.iterdir()} if temp.exists() else set()
    assert not {n for n in temp_after - temp_before if "pagepress" in n.lower() or n.lower().endswith((".png", ".bmp"))}


# ------------------------------------------------------------------ grep 系(AC-16〜AC-18)
def _sources() -> list[Path]:
    return sorted(PKG.rglob("*.py"))


def test_no_network_ac16() -> None:
    pat = re.compile(r"import socket|urllib|http\.client|requests|QNetwork")
    hits = [f"{p.name}:{i}" for p in _sources() for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if pat.search(line)]
    assert hits == []


def test_no_pymupdf_and_pdfium_only_in_render_ac17() -> None:
    pat = re.compile(r"fitz|pymupdf|PyMuPDF")
    assert [p.name for p in _sources() if pat.search(p.read_text(encoding="utf-8"))] == []
    assert [p.name for p in _sources() if "pypdfium2" in p.read_text(encoding="utf-8")] == ["render.py"]


def test_delete_only_in_remove_own_ac18() -> None:
    pat = re.compile(r"os\.remove|os\.unlink|\.unlink\(|shutil\.rmtree|send2trash|recycle\(|clone_from")
    hits = [(p.name, i, line.strip()) for p in _sources()
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if pat.search(line)]
    assert [(n, line) for n, _i, line in hits] == [("naming.py", "os.remove(p)")]
