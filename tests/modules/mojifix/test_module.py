# モジュールの流れ(偽 ctx): 振り分け・CLI・50 件の上限・候補を選ぶまで書けない(AC-2)・FR-11/12 の問いと続き・
# zip と名前の濁点の流れ・停止・利用状況と診断・ログと ops.jsonl に名前や中身が出ない(AC-19)。
from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Any

from deskkit.modules.mojifix import textfix as TF
from deskkit.modules.mojifix import zipnames as Z
from deskkit.modules.mojifix.module import MSG_QUEUE_FULL, kind_of

from .conftest import ADDRESS, FakeCtx, ListHandler, raw_zip, settle

SECRET = "ひみつの名前-SECRET"


def _src(tmp: Path, name: str, data: bytes) -> Path:
    p = tmp / name
    p.write_bytes(data)
    return p


def test_settings_defaults_written(make_module: Any) -> None:
    m, ctx = make_module({"text_output": "bogus", "zip_max_total_gb": 999})
    assert m.config.text_output == "excel" and m.config.zip_max_total_gb == 20
    assert ctx.writes and ctx.writes[-1]["newline"] == "keep"
    assert ctx.tray and ctx.tray[0][0] == "MojiFix を開く"


def test_kind_of(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    assert kind_of(tmp_path / "d") == "rename"
    assert kind_of(tmp_path / "a.ZIP") == "zip"
    assert kind_of(tmp_path / "a.csv") == "text"


def test_cli_dispatch(make_module: Any, tmp_path: Path) -> None:
    m, ctx = make_module()
    a = _src(tmp_path, "a.csv", ADDRESS.encode("cp932"))
    z = _src(tmp_path, "z.zip", raw_zip([(b"a.txt", b"x")]))
    d = tmp_path / "folder"
    d.mkdir()
    areas: list[str] = []
    m.signals.area.connect(areas.append)
    code, msg = m.handle_cli(["open", str(a), str(z), str(d), str(tmp_path / "missing.txt")])
    assert (code, msg) == (0, "queued 3") and ctx.shown == 1
    settle(m, ctx)
    assert m.text_queue == [a] and m.zip_path == z and m.rn_folder == d
    assert areas == ["rename"]
    assert m.sniff is not None and m.zip_scan is not None  # 処理中に積まれた物は、あとで読み込む
    assert m.handle_cli(["other"]) == (2, "unsupported")


def test_queue_limit(make_module: Any, tmp_path: Path) -> None:
    m, ctx = make_module()
    files = [_src(tmp_path, f"{i}.csv", b"a,b\r\n") for i in range(55)]
    n = m.add_paths(files)
    settle(m, ctx)
    assert n == 50 and len(m.text_queue) == 50 and m.queue_full
    assert MSG_QUEUE_FULL == "一度に積めるのは 50 件までです"
    m.remove_text(files[3])
    assert len(m.text_queue) == 49 and not m.queue_full


def test_ac2_export_needs_a_click(make_module: Any, tmp_path: Path) -> None:
    m, ctx = make_module()
    src = _src(tmp_path, "売上.csv", ADDRESS.encode("cp932"))
    m.add_paths([src])
    settle(m, ctx)
    assert m.sniff is not None and m.sniff.candidates[0].key == "sjis"
    assert not m.can_export() and not m.export()
    m.choose("sjis")
    assert m.can_export()
    assert m.export()
    settle(m, ctx)
    assert m.text_out is not None and m.text_out.path.name == "売上_文字直し.csv"
    assert m.text_notice is not None and m.text_notice.text == "書き出しました: 売上_文字直し.csv(Excel で開ける形・3 行)"


def test_ascii_is_preselected(make_module: Any, tmp_path: Path) -> None:
    m, ctx = make_module()
    m.add_paths([_src(tmp_path, "a.csv", b"a,b\r\n1,2\r\n")])
    settle(m, ctx)
    assert m.chosen == "utf8" and m.text_notice is not None and "英数字だけ" in m.text_notice.text
    assert m.can_export()


def test_empty_and_binary_refused(make_module: Any, tmp_path: Path) -> None:
    m, ctx = make_module()
    m.add_paths([_src(tmp_path, "e.txt", b"")])
    settle(m, ctx)
    assert m.text_notice is not None and m.text_notice.text == "空のファイルです" and not m.can_export()
    m.select_text(_src(tmp_path, "x.dat", b"PK\x03\x04" + bytes(30)))
    settle(m, ctx)
    assert m.text_notice is not None and m.text_notice.text == "文字のファイルではないようです" and m.text_is_zip
    reasons = [r["reason"] for r in m.ops.read()]
    assert reasons == ["empty", "binary"]


def test_undecodable_flow(make_module: Any, tmp_path: Path) -> None:
    m, ctx = make_module({"text_output": "utf8"})
    body = "一,UTF-8\n".encode() + "二,Shift_JIS\n".encode("cp932")
    src = _src(tmp_path, "mix.txt", body)
    m.add_paths([src])
    settle(m, ctx)
    m.choose("utf8")
    m.export()
    settle(m, ctx)
    assert m.text_ask == "undecodable" and m.text_check is not None and m.text_check.first_undecodable_line == 2
    assert not list(tmp_path.glob("*_文字直し*"))
    m.refuse_export()
    assert m.chosen is None and m.text_ask is None  # 候補を選び直す
    m.choose("utf8")
    m.export()
    settle(m, ctx)
    assert m.continue_export(geta_undecodable=True)
    settle(m, ctx)
    assert m.text_out is not None and m.text_out.path.read_bytes().decode() == "一,UTF-8\n〓,Shift_JIS\n"


def test_unencodable_flow_switches_to_excel(make_module: Any, tmp_path: Path) -> None:
    m, ctx = make_module({"text_output": "sjis"})
    src = _src(tmp_path, "e.txt", "a—b\n😀\n".encode())
    m.add_paths([src])
    settle(m, ctx)
    m.choose("utf8")
    m.export()
    settle(m, ctx)
    assert m.text_ask == "unencodable" and m.text_check is not None and m.text_check.unencodable == 2
    m.continue_export(out="excel")
    settle(m, ctx)
    assert m.config.text_output == "excel"
    assert m.text_out is not None and m.text_out.path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_unchanged(make_module: Any, tmp_path: Path) -> None:
    m, ctx = make_module({"text_output": "sjis"})
    m.add_paths([_src(tmp_path, "s.csv", ADDRESS.encode("cp932"))])
    settle(m, ctx)
    m.choose("sjis")
    m.export()
    settle(m, ctx)
    assert m.text_notice is not None and m.text_notice.text == TF.MSG_UNCHANGED and m.text_out is None
    assert m.ops.read()[-1]["result"] == "unchanged"


def test_changed_file_is_reloaded(make_module: Any, tmp_path: Path) -> None:
    m, ctx = make_module()
    src = _src(tmp_path, "c.csv", ADDRESS.encode("cp932"))
    m.add_paths([src])
    settle(m, ctx)
    m.choose("sjis")
    m.export()
    settle(m, ctx)
    chk = TF.check(src, TF.Options("sjis", "excel"))
    m.text_check = chk
    m.text_ask = "undecodable"
    src.write_bytes(ADDRESS.encode("cp932") * 2)
    m.continue_export(geta_undecodable=True)
    settle(m, ctx)
    assert m.text_notice is not None and m.text_notice.text == TF.MSG_CHANGED
    assert m.sniff is not None and m.sniff.size == len(ADDRESS.encode("cp932")) * 2  # 読み込み直した
    assert m.chosen is None


def test_zip_flow(make_module: Any, tmp_path: Path) -> None:
    m, ctx = make_module()
    z = _src(tmp_path, "w.zip", raw_zip([("表示/ソフト.txt".encode("cp932"), b"x")]))
    m.add_paths([z])
    settle(m, ctx)
    assert m.zip_cands and not m.can_extract()
    m.choose_zip("sjis")
    settle(m, ctx)
    assert [p.rel for p in m.zip_plan] == ["表示\\ソフト.txt"]
    assert m.extract_zip()
    settle(m, ctx)
    assert (tmp_path / "w" / "表示" / "ソフト.txt").is_file()
    assert m.zip_notice is not None and m.zip_notice.text.startswith("展開 1 件・名前を変えた 0 件・飛ばした 0 件")
    # 設定を変えると一覧を作り直す
    m.set_option("zip_compose", False)
    settle(m, ctx)
    assert m.zip_plan and m.config.zip_compose is False


def test_zip_without_mojibake_selects_itself(make_module: Any, tmp_path: Path) -> None:
    m, ctx = make_module()
    m.add_paths([_src(tmp_path, "a.zip", raw_zip([(b"a.txt", b"x")]))])
    settle(m, ctx)
    assert m.zip_notice is not None and m.zip_notice.text == Z.MSG_NO_MOJIBAKE
    assert m.zip_key == "utf8" and m.can_extract()


def test_zip_errors(make_module: Any, tmp_path: Path) -> None:
    m, ctx = make_module()
    m.add_paths([_src(tmp_path, "b.zip", b"PK\x03\x04broken")])
    settle(m, ctx)
    assert m.zip_notice is not None and m.zip_notice.text == Z.MSG_BROKEN
    assert m.ops.read()[-1]["reason"] == "broken_zip"
    names = [f"{i}.bin".encode() for i in range(8)]
    big = _src(tmp_path, "big.zip", raw_zip([(n, b"x") for n in names], sizes={n: (1, 3 * Z.GB) for n in names}))
    m.set_zip(big)
    settle(m, ctx)
    m.extract_zip()
    settle(m, ctx)
    assert m.zip_notice is not None and m.zip_notice.text == Z.MSG_BOMB
    assert m.ops.read()[-1]["reason"] == "bomb"


def test_rename_flow(make_module: Any, tmp_path: Path) -> None:
    m, ctx = make_module()
    d = tmp_path / "photos"
    d.mkdir()
    nfd = unicodedata.normalize("NFD", "がぎ.jpg")
    (d / nfd).write_bytes(b"x")
    m.add_paths([d])
    assert m.scan_folder()
    settle(m, ctx)
    assert m.rn_scan is not None and len(m.rn_scan.items) == 1 and m.rn_selected == {0}
    m.set_selected(0, False)
    assert not m.selected_items()
    m.set_selected(0, True)
    assert m.rename_selected()
    settle(m, ctx)
    assert [p.name for p in d.iterdir()] == ["がぎ.jpg"] and m.can_undo()
    assert m.undo()
    settle(m, ctx)
    assert [p.name for p in d.iterdir()] == [nfd] and not m.can_undo()
    assert [r["op"] for r in m.ops.read()] == ["rename", "undo"]


def test_rename_nothing_found_and_forbidden(make_module: Any, tmp_path: Path) -> None:
    m, ctx = make_module(env={"WINDIR": str(tmp_path / "win")})
    d = tmp_path / "plain"
    d.mkdir()
    (d / "a.txt").write_text("x")
    m.set_folder(d)
    m.scan_folder()
    settle(m, ctx)
    assert m.rn_notice is not None and m.rn_notice.text == "分かれた濁点のある名前はありませんでした(1 項目を調べました)"
    (tmp_path / "win").mkdir()
    m.set_folder(tmp_path / "win")
    assert m.rn_notice is not None and m.rn_notice.text == "このフォルダは調べられません"
    assert not m.scan_folder()


def test_stop_clears_undo_and_cancels(make_module: Any, tmp_path: Path) -> None:
    m, ctx = make_module()
    d = tmp_path / "p"
    d.mkdir()
    (d / unicodedata.normalize("NFD", "が")).write_text("x")
    m.set_folder(d)
    m.scan_folder()
    settle(m, ctx)
    m.rename_selected()
    settle(m, ctx)
    assert m.rn_done
    m.stop()
    assert m.rn_done == []


def test_ac19_no_names_in_logs_ops_diagnostics(make_module: Any, tmp_path: Path, logs: ListHandler) -> None:
    m, ctx = make_module({"text_output": "sjis"})
    body = f"{SECRET},山田太郎\r\n—\r\n"
    src = _src(tmp_path, f"{SECRET}.csv", body.encode())
    zp = _src(tmp_path, f"{SECRET}.zip", raw_zip([(f"{SECRET}/中.txt".encode("cp932"), b"x")]))
    d = tmp_path / f"{SECRET}-dir"
    d.mkdir()
    (d / unicodedata.normalize("NFD", f"{SECRET}が")).write_text("x")
    m.add_paths([src, zp, d])
    settle(m, ctx)
    m.choose("utf8")
    m.export()
    settle(m, ctx)
    m.continue_export(geta_unencodable=True)
    settle(m, ctx)
    m.choose_zip("sjis")
    settle(m, ctx)
    m.extract_zip()
    settle(m, ctx)
    m.scan_folder()
    settle(m, ctx)
    m.rename_selected()
    settle(m, ctx)
    m.undo()
    settle(m, ctx)
    ops_text = m.ops.path.read_text(encoding="utf-8")
    diag = json.dumps(m.diagnostics(), ensure_ascii=False)
    usage = json.dumps([u.__dict__ for u in m.usage(7)], ensure_ascii=False)
    for blob in (ops_text, logs.text(), diag, usage):
        assert SECRET not in blob and "山田" not in blob and "中.txt" not in blob and str(tmp_path) not in blob
    ops = [json.loads(x) for x in ops_text.splitlines()]
    assert {r["op"] for r in ops} >= {"text", "zip", "rename", "undo"}
    assert all(set(r) == {"ts", "op", "result", "reason", "codec_in", "codec_out", "in_bytes", "out_bytes", "lines",
                          "replaced", "composed", "files", "skipped", "ms"} for r in ops)
    assert m.usage(7)[0].per_day[-1] == 3 and m.usage(7)[0].primary
    assert ctx.notifications == []  # FR-3: 結果は画面にだけ出す
    assert ctx.errors == []


def test_diagnostics_shape(make_module: Any) -> None:
    m, _ctx = make_module()
    d = m.diagnostics()
    assert d["text_output"] == "excel" and d["busy"] is False
    assert all(isinstance(v, (str, int, bool)) for v in d.values())


def test_fake_ctx_is_not_used_for_notify(tmp_path: Path) -> None:
    ctx = FakeCtx(tmp_path / "x")
    ctx.notify("t", "x")
    assert ctx.notifications == [("t", "x")]
