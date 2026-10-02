# v0.4.1(仕様書 §13 Q-9): 定型文の書き出し・読み込み。
# 書き出しは定型文だけ(履歴は入れない)・UTF-8 の JSON・版番号付き。読み込みは既存に足し、同じ名前・同じ本文は足さない。
# 読めない・形が違うファイルは何も変えない。上限を超えた分は足さずに数える。ログ・ops には件数と理由コードだけ(パスも書かない)。
# ファイルの選択窓は出さない(選んだパスを受け取る関数を直接呼ぶ)。
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from deskkit.modules.clipshelf import transfer
from deskkit.modules.clipshelf.crypto import CryptoError
from deskkit.modules.clipshelf.fakes import FakeCipher, FakeClock
from deskkit.modules.clipshelf.store import Store, StoreError
from deskkit.modules.clipshelf.transfer import Snippet, TransferError

MARK = "ZqSnipBody7c"      # 定型文の本文の目印(ログ・ops に出てはいけない)
PMARK = "ZqPathName4e"     # 書き出し先のフォルダ名の目印(ログ・ops に出てはいけない)
HMARK = "ZqHistoryBody2a"  # 履歴の本文の目印(書き出しに出てはいけない)


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@pytest.fixture
def logs() -> Any:
    h = _Records()
    lg = logging.getLogger("deskkit.clipshelf")
    lg.addHandler(h)
    yield h
    lg.removeHandler(h)


def _doc(snips: list[dict[str, Any]], **over: Any) -> bytes:
    return json.dumps({"format": transfer.FORMAT_ID, "version": 1, "snippets": snips, **over},
                      ensure_ascii=False).encode("utf-8")


# ------------------------------------------------------------------ 純粋な処理
def test_build_export_format() -> None:
    now = datetime(2026, 9, 29, 10, 0).astimezone()
    raw = transfer.build_export([Snippet("署名", "山田\n{date}"), Snippet("", "本文だけ"), Snippet("あいさつ", "こんにちは")], now)
    assert not raw.startswith(b"\xef\xbb\xbf")  # BOM なし
    doc = json.loads(raw.decode("utf-8"))
    assert doc["format"] == transfer.FORMAT_ID and doc["version"] == transfer.FORMAT_VERSION == 1
    assert doc["exported_at"].startswith("2026-09-29T10:00:00")
    assert "暗号化されていません" in doc["note"]
    assert {(s["name"], s["text"]) for s in doc["snippets"]} == {
        ("署名", "山田\n{date}"), (transfer.DEFAULT_NAME, "本文だけ"), ("あいさつ", "こんにちは")}
    assert set(doc["snippets"][0]) == {"name", "text"}  # 日時・ID・コピー元は書かない
    assert "署名".encode() in raw  # 日本語は \\u にしない(メモ帳で読める)
    # そのまま読み戻せる
    assert [(s.name, s.text) for s in transfer.parse(raw)] == [(s["name"], s["text"]) for s in doc["snippets"]]


@pytest.mark.parametrize(("raw", "code"), [
    (b"\xff\xfe\x00\x00garbage", transfer.ERR_NOT_UTF8),
    (b"{not json", transfer.ERR_NOT_JSON),
    (b"[]", transfer.ERR_WRONG_FORMAT),
    (json.dumps({"format": "other", "version": 1, "snippets": []}).encode(), transfer.ERR_WRONG_FORMAT),
    (json.dumps({"format": transfer.FORMAT_ID, "snippets": []}).encode(), transfer.ERR_WRONG_FORMAT),
    (json.dumps({"format": transfer.FORMAT_ID, "version": True, "snippets": []}).encode(), transfer.ERR_WRONG_FORMAT),
    (json.dumps({"format": transfer.FORMAT_ID, "version": "1", "snippets": []}).encode(), transfer.ERR_WRONG_FORMAT),
    (json.dumps({"format": transfer.FORMAT_ID, "version": 2, "snippets": []}).encode(), transfer.ERR_NEWER_VERSION),
    (json.dumps({"format": transfer.FORMAT_ID, "version": 1, "snippets": {}}).encode(), transfer.ERR_WRONG_FORMAT),
    (_doc(["文字だけ"]), transfer.ERR_BAD_ENTRY),
    (_doc([{"name": "a", "text": 3}]), transfer.ERR_BAD_ENTRY),
    (_doc([{"name": 5, "text": "x"}]), transfer.ERR_BAD_ENTRY),
    (_doc([{"name": "a"}]), transfer.ERR_BAD_ENTRY),
    (_doc([{"name": "a", "text": "ok"}, {"name": "b", "text": "  "}]), transfer.ERR_BAD_ENTRY),
])
def test_parse_rejects(raw: bytes, code: str) -> None:
    with pytest.raises(TransferError) as ei:
        transfer.parse(raw)
    assert ei.value.code == code
    assert ei.value.text  # 利用者向けの文がある


def test_parse_accepts_bom_missing_name_and_extra_keys() -> None:
    raw = b"\xef\xbb\xbf" + _doc([{"text": "名前なし"}, {"name": None, "text": "x"}, {"name": " a ", "text": "y", "future": 1}],
                                 extra="ignored")
    got = transfer.parse(raw)
    assert [(s.name, s.text) for s in got] == [(transfer.DEFAULT_NAME, "名前なし"), (transfer.DEFAULT_NAME, "x"), ("a", "y")]


def test_read_file_too_large_and_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "big.json"
    p.write_bytes(_doc([{"name": "a", "text": "x" * 200}]))
    monkeypatch.setattr(transfer, "MAX_FILE_BYTES", 100)
    with pytest.raises(TransferError) as ei:
        transfer.read_file(p)
    assert ei.value.code == transfer.ERR_TOO_LARGE
    with pytest.raises(TransferError) as ei2:
        transfer.read_file(tmp_path / "none.json")
    assert ei2.value.code == transfer.ERR_UNREADABLE


def test_plan_import_dedup_and_limit() -> None:
    existing = [Snippet("署名", "山田"), Snippet(transfer.DEFAULT_NAME, "本文")]
    incoming = [
        Snippet("署名", "山田"),        # 既にある
        Snippet(" 署名 ", "山田"),      # 名前の前後の空白は無視して同じ
        Snippet("", "本文"),            # 空の名前は「無題の定型文」と同じ
        Snippet("署名2", "山田"),       # 本文が同じでも名前が違えば足す
        Snippet("署名", "山田 "),       # 本文が 1 文字でも違えば足す
        Snippet("新", "a"),
        Snippet("新", "a"),             # ファイルの中の重複
        Snippet("新2", "b"),
    ]
    plan = transfer.plan_import(existing, incoming, limit=5)
    assert [(s.name, s.text) for s in plan.to_add] == [("署名2", "山田"), ("署名", "山田 "), ("新", "a")]
    assert plan.duplicates == 4 and plan.over_limit == 1
    full = transfer.plan_import(existing, [Snippet("x", "y")], limit=2)
    assert full.to_add == [] and full.over_limit == 1


@pytest.fixture(autouse=True)
def _no_real_recycle(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """テストでは本物のごみ箱に送らない。送ったことにして、ファイルは一時フォルダの別の名前へ退避する。"""
    sent: list[Path] = []

    def fake(p: Path) -> bool:
        sent.append(p)
        p.replace(p.with_name(p.name + ".recycled"))
        return True

    monkeypatch.setattr(transfer, "_recycle_one", fake)
    return sent


def test_write_file_recycles_existing_and_leaves_no_part(tmp_path: Path, _no_real_recycle: list[Path]) -> None:
    p = tmp_path / "out.json"
    p.write_text("old", encoding="utf-8")
    transfer.write_file(p, b"new")
    assert p.read_bytes() == b"new" and _no_real_recycle == [p]   # 前のファイルはごみ箱へ(v0.4.1 レビュー 5)
    assert sorted(x.name for x in tmp_path.iterdir()) == ["out.json", "out.json.recycled"]
    with pytest.raises(TransferError):
        transfer.write_file(tmp_path / "no_such_dir" / "x.json", b"x")


def test_write_file_does_not_touch_user_part_file(tmp_path: Path) -> None:
    """利用者の「.<名前>.part」という名前のファイルを上書き・削除しない(レビュー 5)。"""
    user = tmp_path / ".out.json.part"
    user.write_bytes(b"USER DATA")
    transfer.write_file(tmp_path / "out.json", b"new")
    assert user.read_bytes() == b"USER DATA"
    assert sorted(x.name for x in tmp_path.iterdir()) == [".out.json.part", "out.json"]


def test_write_file_keeps_existing_when_recycle_fails(tmp_path: Path) -> None:
    p = tmp_path / "out.json"
    p.write_text("old", encoding="utf-8")
    with pytest.raises(TransferError) as ei:
        transfer.write_file(p, b"new", recycle=lambda _p: False)
    assert ei.value.code == transfer.ERR_CANNOT_REPLACE
    assert p.read_text(encoding="utf-8") == "old" and [x.name for x in tmp_path.iterdir()] == ["out.json"]


def test_store_add_snippets_is_all_or_nothing(tmp_path: Path) -> None:
    class Flaky(FakeCipher):
        n = 0

        def protect(self, data: bytes) -> bytes:
            self.n += 1
            if self.n == 3:
                raise CryptoError("fake", 1)
            return super().protect(data)

    st = Store(tmp_path / "db.db", Flaky(), FakeClock())
    st.open()
    try:
        with pytest.raises(CryptoError):
            st.add_snippets([("a", "1"), ("b", "2"), ("c", "3")])
        assert st.row_count() == 0 and st.snippets() == []
        got = st.add_snippets([("d", "4"), ("e", "5")])
        assert [i.name for i in got] == ["d", "e"] and st.row_count() == 2
        assert st.add_snippets([]) == []
    finally:
        st.close()
    # 読み直しても同じ(暗号化して保存している)
    st2 = Store(tmp_path / "db.db", FakeCipher(), FakeClock())
    st2.open()
    assert sorted((i.name, i.text) for i in st2.snippets()) == [("d", "4"), ("e", "5")]
    st2.close()


# ------------------------------------------------------------------ モジュール
def _copy(m: Any, api: Any, text: str) -> None:
    api.put(text)
    m.monitor.process()


def test_module_export_only_snippets(module_factory: Any, tmp_path: Path, logs: Any) -> None:
    m, ctx, api, clock = module_factory({"mode": "record"})
    _copy(m, api, f"{HMARK} 履歴")
    m.save_snippet(None, "署名", f"{MARK} 山田")
    m.save_snippet(None, "", "名前なし")
    out = tmp_path / PMARK / "snips.json"
    out.parent.mkdir()
    r = m.export_snippets(out)
    assert r.ok and r.count == 2 and r.error is None
    raw = out.read_bytes()
    assert HMARK.encode() not in raw  # 履歴は書き出さない
    doc = json.loads(raw.decode("utf-8"))
    assert sorted((s["name"], s["text"]) for s in doc["snippets"]) == sorted(
        [("署名", f"{MARK} 山田"), (transfer.DEFAULT_NAME, "名前なし")])
    assert doc["exported_at"].startswith(clock().date().isoformat())  # モジュールの時計
    op = [o for o in m.ops.read() if o["op"] == "snippets_export"][-1]
    assert op == {"ts": op["ts"], "op": "snippets_export", "result": "ok", "count": 2}
    blob = "\n".join(logs.lines) + (ctx.data_dir / "ops.jsonl").read_text(encoding="utf-8")
    for s in (MARK, PMARK, "snips.json", str(tmp_path)):
        assert s not in blob
    # 書き出せない場所は失敗を返す(パスは文に入れない)
    bad = m.export_snippets(tmp_path / "missing_dir" / "x.json")
    assert not bad.ok and bad.error and "missing_dir" not in bad.error
    assert [o for o in m.ops.read() if o["op"] == "snippets_export"][-1]["result"] == "failed"


def test_module_export_without_snippets(module_factory: Any, tmp_path: Path) -> None:
    m, *_ = module_factory()
    r = m.export_snippets(tmp_path / "x.json")
    assert not r.ok and r.error and not (tmp_path / "x.json").exists()


def test_module_import_adds_and_skips_duplicates(module_factory: Any, tmp_path: Path, logs: Any) -> None:
    m, ctx, api, _ = module_factory()
    m.save_snippet(None, "署名", "山田")
    src = tmp_path / PMARK / "in.json"
    src.parent.mkdir()
    src.write_bytes(_doc([{"name": "署名", "text": "山田"}, {"name": "新", "text": f"{MARK} 本文"},
                          {"name": "新", "text": f"{MARK} 本文"}]))
    changed: list[int] = []
    m.notifier.changed.connect(lambda: changed.append(1))
    r = m.import_snippets(src)
    assert r.ok and (r.read, r.count, r.duplicates, r.over_limit) == (3, 1, 2, 0)
    assert sorted((s.name, s.text) for s in m.store.snippets()) == [("新", f"{MARK} 本文"), ("署名", "山田")]
    assert changed
    # 2 回目は全部同じなので何も足さない
    r2 = m.import_snippets(src)
    assert r2.ok and r2.count == 0 and r2.duplicates == 3 and len(m.store.snippets()) == 2
    ops = [o for o in m.ops.read() if o["op"] == "snippets_import"]
    assert ops[0]["result"] == "ok" and (ops[0]["read"], ops[0]["added"], ops[0]["duplicates"], ops[0]["over_limit"]) == (3, 1, 2, 0)
    blob = "\n".join(logs.lines) + (ctx.data_dir / "ops.jsonl").read_text(encoding="utf-8")
    for s in (MARK, PMARK, "in.json", str(tmp_path)):
        assert s not in blob
    # DB の中は暗号化したまま(平文の本文が DB ファイルに無い)
    assert MARK.encode() not in (ctx.data_dir / "clipshelf.db").read_bytes()


def test_module_import_bad_file_changes_nothing(module_factory: Any, tmp_path: Path) -> None:
    m, ctx, api, _ = module_factory()
    m.save_snippet(None, "a", "b")
    rows = m.store.row_count()
    for name, raw in (("x.json", b"{broken"), ("y.json", _doc([{"name": "a", "text": "ok"}, {"name": "b", "text": 1}])),
                      ("z.json", json.dumps({"hello": 1}).encode())):
        p = tmp_path / name
        p.write_bytes(raw)
        r = m.import_snippets(p)
        assert not r.ok and r.error and r.count == 0
        assert m.store.row_count() == rows
    r = m.import_snippets(tmp_path / "none.json")
    assert not r.ok and m.store.row_count() == rows
    codes = [o["result"] for o in m.ops.read() if o["op"] == "snippets_import"]
    assert codes == [transfer.ERR_NOT_JSON, transfer.ERR_BAD_ENTRY, transfer.ERR_WRONG_FORMAT, transfer.ERR_UNREADABLE]


def test_module_import_limit(module_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    m, *_ = module_factory()
    m.save_snippet(None, "a", "1")
    monkeypatch.setattr(transfer, "MAX_SNIPPETS", 3)
    p = tmp_path / "in.json"
    p.write_bytes(_doc([{"name": f"n{i}", "text": str(i)} for i in range(5)]))
    r = m.import_snippets(p)
    assert r.ok and r.count == 2 and r.over_limit == 3
    assert len(m.store.snippets()) == 3


def test_module_import_store_failure_changes_nothing(module_factory: Any, tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    m, *_ = module_factory()
    p = tmp_path / "in.json"
    p.write_bytes(_doc([{"name": "a", "text": "1"}]))

    def boom(_pairs: Any) -> Any:
        raise StoreError("DB に書けません(OperationalError)")

    monkeypatch.setattr(m.store, "add_snippets", boom)
    r = m.import_snippets(p)
    assert not r.ok and r.error and m.store.snippets() == []
    assert [o for o in m.ops.read() if o["op"] == "snippets_import"][-1]["result"] == "failed"


def test_round_trip_between_two_modules(module_factory: Any, tmp_path: Path) -> None:
    a, *_ = module_factory()
    a.save_snippet(None, "署名", "山田 太郎\n{date} {time}")
    a.save_snippet(None, "住所", "東京都 {input:部屋番号=101}")
    f = tmp_path / "snips.json"
    assert a.export_snippets(f).ok
    b, *_ = module_factory()
    r = b.import_snippets(f)
    assert r.ok and r.count == 2
    assert sorted((s.name, s.text) for s in b.store.snippets()) == sorted((s.name, s.text) for s in a.store.snippets())


# ------------------------------------------------------------------ 画面
def test_page_buttons_confirm_and_flow(module_factory: Any, qapp: Any, tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    from deskkit.ui import widgets

    m, ctx, api, _ = module_factory()
    m.save_snippet(None, "署名", "山田")
    page = m.create_page()
    # ボタンは定型文の欄の中(新しい定型文の下)にある
    assert page.snip_export.text().endswith("書き出す") and page.snip_import.text().endswith("読み込む")
    card = page.snip_list.parentWidget()
    assert page.snip_export.parentWidget() is card and page.snip_import.parentWidget() is card

    # 確認の窓の文言: 暗号化されない・誰でも読める・履歴は書き出さない
    text = page.export_confirm_text(1)
    for s in ("暗号化されません", "誰でも読めます", "履歴は書き出しません", "定型文 1 件"):
        assert s in text

    asked: list[str] = []
    monkeypatch.setattr(page, "_ask_export_path", lambda: (asked.append("x"), str(tmp_path / "o.json"))[1])
    seen: list[tuple[str, str, dict[str, Any]]] = []
    answer = [False]
    monkeypatch.setattr(widgets, "confirm", lambda _p, title, body, **k: (seen.append((title, body, k)), (answer[0], []))[1])
    msgs: list[tuple[str, str, str]] = []
    monkeypatch.setattr(widgets, "message", lambda _p, title, body, **k: msgs.append((title, body, k.get("kind", "info"))))

    # 確認で「キャンセル」なら選択窓も出さず、何も書かない
    page._export_snippets()
    assert seen and "暗号化されません" in seen[0][0] and seen[0][2]["ok_text"] == "平文で書き出す"
    assert asked == [] and not (tmp_path / "o.json").exists()
    # 承認すると選んだ場所に書く
    answer[0] = True
    page._export_snippets()
    assert asked == ["x"] and (tmp_path / "o.json").exists()
    assert page.saved_pill.text().endswith("定型文を 1 件書き出しました")

    # 読み込み: 選んだパスを渡す関数を直接呼ぶ
    src = tmp_path / "in.json"
    src.write_bytes(_doc([{"name": "署名", "text": "山田"}, {"name": "挨拶", "text": "こんにちは"}]))
    r = page.import_snippets_from(str(src))
    assert r.ok and r.count == 1 and r.duplicates == 1
    title, body, kind = msgs[-1]
    assert title == "読み込みました" and kind == "ok"
    assert "1 件足しました" in body and "1 件は足しませんでした" in body and "暗号化されていません" in body
    names = [page.snip_list.item(i).text() for i in range(page.snip_list.count())]
    assert sorted(names) == ["挨拶", "署名"]

    # 形が違うファイル: 何も変えずに知らせる
    bad = tmp_path / "bad.json"
    bad.write_text("[]", encoding="utf-8")
    r = page.import_snippets_from(str(bad))
    assert not r.ok and msgs[-1][0] == "読み込めませんでした" and msgs[-1][2] == "error"
    assert "定型文は変えていません" in msgs[-1][1]
    assert len(m.store.snippets()) == 2

    # 選択窓で取り消したら何もしない
    monkeypatch.setattr(page, "_ask_import_path", lambda: "")
    n = len(msgs)
    page._import_snippets()
    assert len(msgs) == n
    page.close()
    assert not ctx.errors


def test_page_export_without_snippets_says_so(module_factory: Any, qapp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from deskkit.ui import widgets

    m, *_ = module_factory()
    page = m.create_page()
    confirms: list[int] = []
    msgs: list[str] = []
    monkeypatch.setattr(widgets, "confirm", lambda *a, **k: (confirms.append(1), (True, []))[1])
    monkeypatch.setattr(widgets, "message", lambda _p, _t, body, **k: msgs.append(body))
    page._export_snippets()
    assert confirms == [] and msgs == ["書き出す定型文がありません。"]
    page.close()


def test_page_import_keeps_unsaved_edit(module_factory: Any, qapp: Any, tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    from deskkit.ui import widgets

    m, *_ = module_factory()
    m.save_snippet(None, "署名", "山田")
    page = m.create_page()
    monkeypatch.setattr(widgets, "message", lambda *a, **k: None)
    page.snip_editor.set_values("署名", "書きかけ")
    page._on_snippet_edited()
    src = tmp_path / "in.json"
    src.write_bytes(_doc([{"name": "挨拶", "text": "こんにちは"}]))
    assert page.import_snippets_from(str(src)).count == 1
    assert page.snip_editor.values() == ("署名", "書きかけ")  # 未保存の変更は消さない
    assert page.snip_list.count() == 2
    page.close()


def test_import_summary_texts() -> None:
    from deskkit.modules.clipshelf.module import TransferResult
    from deskkit.modules.clipshelf.page import ClipShelfPage

    text, kind = ClipShelfPage.import_summary(TransferResult(True, count=0, duplicates=0, over_limit=0, read=0))
    assert "ファイルに定型文がありませんでした" in text and kind == "info"
    text, kind = ClipShelfPage.import_summary(TransferResult(True, count=1, over_limit=2, read=3))
    assert "2 件は足しませんでした" in text and kind == "warn"
