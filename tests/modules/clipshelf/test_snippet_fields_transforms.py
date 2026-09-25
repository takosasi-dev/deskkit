# v0.2 C1(定型文の入力欄 {input:…} / 選択欄 {select:…} とパレット内フォーム)と
# C2(変換して貼り付け: Shift+Enter・右クリック)。値・変換結果はクリップボードに置くだけで、ログ・ops・DB に残さない。
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QComboBox, QLineEdit

from deskkit.modules.clipshelf import policy, snippets, transforms
from deskkit.modules.clipshelf._win32 import FMT_EXCLUDE_MONITOR, FMT_ORIGIN
from deskkit.modules.clipshelf.fakes import FakeClock
from deskkit.modules.clipshelf.palette import SHEET_FORM, SHEET_LIST, SHEET_TRANSFORM
from deskkit.modules.clipshelf.writer import decode_origin

VALUE_MARKER = "FORM-VALUE-7c1e"


# ------------------------------------------------------------------ snippets(純関数)
def test_fields_parse_dedupe_and_order() -> None:
    t = "{input:宛名=山田} 様 {select:至急|通常} {input:宛名} {input:件名} {select:至急|通常} {select:A| B |} {input:}"
    fs = snippets.fields(t)
    assert [(f.kind, f.label, f.default, f.options) for f in fs] == [
        ("input", "宛名", "山田", ()),
        ("select", "選択 1", "至急", ("至急", "通常")),
        ("input", "件名", "", ()),
        ("select", "選択 2", "A", ("A", "B")),
        ("input", "入力", "", ()),
    ]


def test_expand_with_values_and_defaults() -> None:
    now = FakeClock()()
    t = "{input:宛名=山田}様 [{select:至急|通常}] {input:宛名} {date} {{input:x}} {unknown}"
    key_name, key_sel = "input:宛名", "select:至急|通常"
    exp = snippets.expand(t, now, None, values={key_name: "佐藤 {date}", key_sel: "通常"})
    assert exp.text == "佐藤 {date}様 [通常] 佐藤 {date} 2026-10-01 {input:x} {unknown}"  # 値の中の {} は展開しない
    assert exp.unknown == ["unknown"]
    dflt = snippets.expand(t, now, None, values={})
    assert dflt.text.startswith("山田様 [至急] 山田 ")
    preview = snippets.expand(t, now, None)
    assert preview.text.startswith("〔宛名〕様 [〔選択 1〕] 〔宛名〕 ")


def test_expand_empty_select_is_left_with_warning() -> None:
    exp = snippets.expand("a {select:} b", FakeClock()(), None, values={})
    assert exp.text == "a {select:} b" and exp.warnings and not exp.fields


def test_existing_placeholders_unchanged() -> None:
    exp = snippets.expand("{date} {time} {clipboard} {{x}} {unknown}", FakeClock()(), "C")
    assert exp.text == "2026-10-01 10:00 C {x} {unknown}" and exp.unknown == ["unknown"] and not exp.fields


# ------------------------------------------------------------------ transforms(純関数)
@pytest.mark.parametrize(
    ("tid", "src", "want"),
    [
        ("strip", "  \t abc \n\n", "abc"),
        ("join_lines", "a\r\nb\n\n  c\rd", "a b c d"),
        ("zen_to_han", "ＡＢｃ１２３（全角）－＠", "ABc123（全角）－＠"),
        ("upper", "abcＡｂ", "ABCＡＢ"),
        ("lower", "ABC", "abc"),
        ("first_line", "\n  \n  first  \nsecond", "  first"),
        ("first_line", "   ", ""),
    ],
)
def test_transforms(tid: str, src: str, want: str) -> None:
    assert transforms.apply(tid, src) == want


def test_transform_catalog() -> None:
    assert transforms.IDS == ("strip", "join_lines", "zen_to_han", "upper", "lower", "first_line")
    assert [transforms.LABELS[i] for i in transforms.IDS] == [
        "前後の空白を除く", "改行を除く(スペースに)", "全角英数を半角に", "大文字", "小文字", "1行目だけ"]
    with pytest.raises(ValueError):
        transforms.apply("nope", "x")


# ------------------------------------------------------------------ module.choose
def _no_marker_anywhere(m: Any, ctx: Any, records: list[logging.LogRecord], marker: str) -> None:
    for p in Path(ctx.data_dir).rglob("*"):
        if p.is_file():
            raw = p.read_bytes()
            assert marker.encode("utf-8") not in raw and marker.encode("utf-16-le") not in raw, p.name
    assert not any(marker in r.getMessage() for r in records)


@pytest.fixture
def records() -> Any:
    got: list[logging.LogRecord] = []

    class H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            got.append(record)

    h = H(logging.DEBUG)
    lg = logging.getLogger("deskkit")
    old = lg.level
    lg.setLevel(logging.DEBUG)
    lg.addHandler(h)
    yield got
    lg.removeHandler(h)
    lg.setLevel(old)


def test_choose_with_values_and_transform(module_factory: Any, records: Any) -> None:
    m, ctx, api, _ = module_factory({"mode": "record"})
    snip = m.save_snippet(None, "宛名", "{input:宛名}様 {select:至急|通常}")
    m.choose(snip, values={"input:宛名": f"  {VALUE_MARKER}"}, transform="strip")
    assert api.text_now() == f"{VALUE_MARKER}様 至急"
    assert decode_origin(api.clip[api.fmt_id(FMT_ORIGIN)]) == snip.id
    assert m.monitor.process().reason == policy.SELF_ORIGIN  # 自分の書き込みは記録しない
    assert m.store.counts()["history"] == 0
    ops = [o for o in m.ops.read() if o["op"] == "transform_paste"]
    assert ops == [{"ts": ops[0]["ts"], "op": "transform_paste", "transform": "strip"}]
    _no_marker_anywhere(m, ctx, records, VALUE_MARKER)


def test_transform_history_keeps_exclusion_marks(module_factory: Any) -> None:
    m, ctx, api, _ = module_factory({"mode": "record"})
    snip = m.save_snippet(None, "包む", "[{clipboard}]\n2行目")
    api.put(" pw ", formats={FMT_EXCLUDE_MONITOR: b"\x01\x00\x00\x00"})
    m.choose(snip, transform="first_line")
    assert api.text_now() == "[ pw ]" and FMT_EXCLUDE_MONITOR in api.format_names_now()  # INV-7


def test_transform_to_empty_does_not_write(module_factory: Any) -> None:
    m, ctx, api, _ = module_factory({"mode": "record"})
    api.put("   ")
    item = m.store.add_history("   ", None)
    before = dict(api.clip)
    m.choose(item, transform="strip")
    assert api.clip == before and "空" in ctx.notifications[-1][1]


# ------------------------------------------------------------------ パレット: 入力フォーム(C1)
def _open_snippets(m: Any) -> Any:
    m.open_palette(snippets_tab=True)
    pal = m._palette
    assert pal is not None and pal.isVisible()
    return pal


def test_palette_form_keyboard_flow(module_factory: Any, records: Any) -> None:
    m, ctx, api, _ = module_factory({"mode": "record"})
    m.save_snippet(None, "挨拶", "{input:宛名=山田}様、{select:お世話になります|はじめまして}。{input:件名}")
    pal = _open_snippets(m)
    QTest.keyClick(pal.search, Qt.Key.Key_Return)
    assert pal.sheets.currentIndex() == SHEET_FORM and api.text_now() is None  # まだ貼らない
    ws = [w for _f, w in pal._form_widgets]
    assert isinstance(ws[0], QLineEdit) and isinstance(ws[1], QComboBox) and isinstance(ws[2], QLineEdit)
    assert pal.focusWidget() is ws[0] and ws[0].text() == "山田"
    QTest.keyClicks(ws[0], VALUE_MARKER)  # 全選択されているので上書き
    QTest.keyClick(ws[0], Qt.Key.Key_Tab)
    assert pal.focusWidget() is ws[1]
    ws[1].setCurrentIndex(1)
    QTest.keyClick(ws[1], Qt.Key.Key_Tab)
    assert pal.focusWidget() is ws[2]
    QTest.keyClicks(ws[2], "re")  # QTest.keyClicks は非 ASCII 文字でテスト環境ごと落ちるので ASCII で打つ
    QTest.keyClick(ws[2], Qt.Key.Key_Tab)
    assert pal.focusWidget() is ws[0]  # 最後の欄から最初へ回る
    QTest.keyClick(ws[0], Qt.Key.Key_Backtab)
    assert pal.focusWidget() is ws[2]
    assert f"{VALUE_MARKER}様、はじめまして。re" in pal.form_preview.toPlainText()
    QTest.keyClick(ws[2], Qt.Key.Key_Return)
    assert api.text_now() == f"{VALUE_MARKER}様、はじめまして。re"
    assert not pal.isVisible() and pal.sheets.currentIndex() == SHEET_LIST and pal._form_widgets == []
    _no_marker_anywhere(m, ctx, records, VALUE_MARKER)


def test_palette_form_escape_goes_back(module_factory: Any) -> None:
    m, ctx, api, _ = module_factory({"mode": "record"})
    m.save_snippet(None, "x", "{input:a}")
    pal = _open_snippets(m)
    QTest.keyClick(pal.search, Qt.Key.Key_Return)
    w = pal._form_widgets[0][1]
    QTest.keyClicks(w, "typed")
    QTest.keyClick(w, Qt.Key.Key_Escape)
    assert pal.isVisible() and pal.sheets.currentIndex() == SHEET_LIST and pal._form_widgets == []
    assert api.text_now() is None and pal.focusWidget() is pal.search
    QTest.keyClick(pal.search, Qt.Key.Key_Escape)  # もう一度 Esc で閉じる
    pal.dismiss(animated=False)


def test_snippet_without_fields_pastes_directly(module_factory: Any) -> None:
    m, ctx, api, _ = module_factory({"mode": "record"})
    m.save_snippet(None, "署名", "よろしく {date}")
    pal = _open_snippets(m)
    QTest.keyClick(pal.search, Qt.Key.Key_Return)
    assert api.text_now() == "よろしく 2026-10-01"


# ------------------------------------------------------------------ パレット: 変換して貼り付け(C2)
def _history(m: Any, api: Any, clock: Any, texts: list[str]) -> None:
    for t in texts:
        clock.advance(minutes=1)
        api.put(t)
        m.monitor.process()


def test_shift_enter_transform_with_number_key(module_factory: Any) -> None:
    m, ctx, api, clock = module_factory({"mode": "record"})
    _history(m, api, clock, ["  Hello World  "])
    m.open_palette()
    pal = m._palette
    QTest.keyClick(pal.search, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    assert pal.sheets.currentIndex() == SHEET_TRANSFORM and pal.focusWidget() is pal.tf_list
    assert pal.tf_preview.toPlainText() == "Hello World"  # 1番目(前後の空白を除く)のプレビュー
    QTest.keyClick(pal.tf_list, Qt.Key.Key_Down)
    QTest.keyClick(pal.tf_list, Qt.Key.Key_Down)
    QTest.keyClick(pal.tf_list, Qt.Key.Key_Down)
    assert pal._current_transform() == "upper" and pal.tf_preview.toPlainText() == "  HELLO WORLD  "
    QTest.keyClick(pal.tf_list, Qt.Key.Key_5)  # 数字で直接選ぶ(小文字)
    assert api.text_now() == "  hello world  " and not pal.isVisible()
    assert m.store.counts()["history"] == 1  # 元の履歴は変わらず、新しい行も増えない
    assert m.monitor.process().reason == policy.SELF_ORIGIN
    assert m.store.history()[0].text == "  Hello World  "


def test_transform_enter_and_escape(module_factory: Any) -> None:
    m, ctx, api, clock = module_factory({"mode": "record"})
    _history(m, api, clock, ["a\nb"])
    m.open_palette()
    pal = m._palette
    QTest.keyClick(pal.search, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    QTest.keyClick(pal.tf_list, Qt.Key.Key_Escape)
    assert pal.isVisible() and pal.sheets.currentIndex() == SHEET_LIST and api.text_now() == "a\nb"
    QTest.keyClick(pal.search, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    QTest.keyClick(pal.tf_list, Qt.Key.Key_Down)
    QTest.keyClick(pal.tf_list, Qt.Key.Key_Return)
    assert api.text_now() == "a b"


def test_transform_then_form_for_snippet(module_factory: Any) -> None:
    m, ctx, api, _ = module_factory({"mode": "record"})
    m.save_snippet(None, "x", "id: {input:ID}")
    pal = _open_snippets(m)
    QTest.keyClick(pal.search, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    QTest.keyClick(pal.tf_list, Qt.Key.Key_4)  # 大文字 → 値を聞くフォームへ
    assert pal.sheets.currentIndex() == SHEET_FORM and "大文字" in pal.form_pill.text()
    w = pal._form_widgets[0][1]
    QTest.keyClicks(w, "ab12")
    assert pal.form_preview.toPlainText() == "ID: AB12"
    QTest.keyClick(w, Qt.Key.Key_Enter)
    assert api.text_now() == "ID: AB12"


def test_context_menu_actions(module_factory: Any) -> None:
    m, ctx, api, clock = module_factory({"mode": "record"})
    _history(m, api, clock, ["ｘｙｚ１"])
    m.open_palette()
    pal = m._palette
    item = pal.current_item()
    menu = pal.build_context_menu(item)
    labels = [a.text() for a in menu.actions()]
    assert labels[0].startswith("貼り付け") and any(t.startswith("ピン留め") for t in labels)
    sub = next(a.menu() for a in menu.actions() if a.menu() is not None)
    acts = sub.actions()
    assert len(acts) == 6 and "全角英数を半角に" in acts[2].text()
    acts[2].trigger()
    assert api.text_now() == "xyz1"
    menu.deleteLater()


def test_palette_hints_follow_sheet(module_factory: Any) -> None:
    m, ctx, api, clock = module_factory({"mode": "record"})
    _history(m, api, clock, ["a"])
    m.open_palette()
    pal = m._palette

    def hint_keys() -> list[str]:
        out = []
        for i in range(pal.hints_box.count()):
            w = pal.hints_box.itemAt(i).widget()
            out.append(w.findChildren(type(pal.hits))[0].text())
        return out

    assert "Shift+Enter" in hint_keys()
    QTest.keyClick(pal.search, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    assert "1〜6" in hint_keys() and not pal.clear_btn.isVisibleTo(pal)
    pal.dismiss(animated=False)
    assert pal.sheets.currentIndex() == SHEET_LIST


# ------------------------------------------------------------------ 編集部品のチップ
def test_editor_chip_selects_label(qapp: Any) -> None:
    from deskkit.modules.clipshelf.editor import SnippetEditor

    ed = SnippetEditor(lambda t: snippets.expand(t, FakeClock()(), None), "#3366FF")
    ed._insert("{input:ラベル}")
    assert ed.body.toPlainText() == "{input:ラベル}" and ed.body.textCursor().selectedText() == "ラベル"
    ed.body.insertPlainText("宛名")
    assert ed.body.toPlainText() == "{input:宛名}"
    ed._insert("{select:A|B|C}")
    assert ed.body.textCursor().selectedText() == "A|B|C"
