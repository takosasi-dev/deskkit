# KeyFree の画面のテスト(offscreen)。確認してから始める(FR-1)・コピー(AC-5・FR-4)・使用中の案内と調べ直す(FR-5)・
# 押された組の知らせ(AC-7)・古い host(AC-13)・DeskKit のキー(FR-10)・1つだけ調べる(FR-9)・Windows 用の印(AC-4)・956 組の表。
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QApplication, QLabel

from deskkit.hotkeys import FailedKey
from deskkit.modules.keyfree import combos, module, scan, trykey
from deskkit.modules.keyfree.fakes import OldHotkeys
from deskkit.modules.keyfree.page import OWNER, USED_HELP, KeyFreePage

C, A, S, WIN = combos.MOD_CONTROL, combos.MOD_ALT, combos.MOD_SHIFT, combos.MOD_WIN
K, J, SPACE = 0x4B, 0x4A, 0x20


def _labels(w: Any) -> str:
    return "\n".join(lb.text() for lb in w.findChildren(QLabel))


def _scan(page: KeyFreePage, ctx: Any) -> None:
    page._ask_start()
    assert page.confirm.isVisibleTo(page)
    page._start()
    ctx.hotkeys.flush()


def test_confirm_before_start(make_module: Any) -> None:
    m, ctx = make_module()
    page = KeyFreePage(m)
    page._ask_start()
    assert ctx.hotkeys.calls == []                  # 押しただけでは始めない(FR-1)
    assert "数秒、キーボードに触れないでください" in _labels(page)
    page._start()
    assert len(ctx.hotkeys.calls) == 1
    page._start()                                   # 連打は無視
    assert len(ctx.hotkeys.calls) == 1
    assert page.progress_card.isVisibleTo(page)
    assert "347 組中 0 組" in page.progress_label.text()
    ctx.hotkeys.flush(batches=3)
    assert "347 組中 96 組" in page.progress_label.text()
    ctx.hotkeys.flush()
    assert not page.progress_card.isVisibleTo(page)
    assert page.t_free.value.text() == "347"
    assert len(page.groups) == 4


def test_about_text(make_module: Any) -> None:
    m, _ctx = make_module()
    text = _labels(KeyFreePage(m))
    assert "開いているアプリの中だけで効くキー" in text      # FR-18
    assert OWNER in text                                     # K-6


def test_free_click_copies_format(make_module: Any) -> None:
    """AC-5: 空きのボタンを押すと、偽の format の文字列がクリップボードに入る。"""
    m, ctx = make_module()
    ctx.hotkeys.format_override = lambda mods, vk: f"FMT-{mods}-{vk}"
    page = KeyFreePage(m)
    _scan(page, ctx)
    cap = page.groups[3].caps[(C | A | S, K)]
    cap.click()
    assert QApplication.clipboard().text() == f"FMT-{C | A | S}-{K}"
    assert page.toast.text() == f"FMT-{C | A | S}-{K} をコピーしました"
    assert page.selected == (C | A | S, K)
    assert page.groups[3].detail.isVisibleTo(page)
    assert "押して確かめる" in "\n".join(b.text() for b in page.groups[3].detail.findChildren(type(page.scan_btn)))


def test_used_click_shows_help_and_recheck(make_module: Any) -> None:
    m, ctx = make_module()
    ctx.hotkeys.used.add((C | A, SPACE))
    page = KeyFreePage(m)
    _scan(page, ctx)
    QApplication.clipboard().setText("before")
    page.groups[0].caps[(C | A, SPACE)].click()
    assert QApplication.clipboard().text() == "before"      # 使用中はコピーしない
    assert USED_HELP in _labels(page.groups[0].detail)
    ctx.hotkeys.used.clear()
    page._recheck((C | A, SPACE))
    ctx.hotkeys.flush()
    assert page.groups[0].caps[(C | A, SPACE)].state == scan.FREE
    assert "Ctrl+Alt+Space: 空き" in page.recheck_text[(C | A, SPACE)]


def test_pressed_notice_on_page(make_module: Any) -> None:
    """AC-7: 押された組の名前は画面に出る(ログには件数だけ。conftest が確かめる)。"""
    m, ctx = make_module()
    page = KeyFreePage(m)
    page._ask_start()
    page._start()
    ctx.hotkeys.pressed_plan = [(C | A | S, K)]
    ctx.hotkeys.flush()
    text = _labels(page.notice_card)
    assert "調べている間に Ctrl+Alt+Shift+K が押されました。そのアプリには届いていないかもしれません。もう一度押してください" in text
    assert page.notice_card.isVisibleTo(page)


def test_old_host_page(make_module: Any) -> None:
    """AC-13: probe が無いと FR-17 の文言が出て、例外が出ない。"""
    m, _ctx = make_module(hotkeys=OldHotkeys())
    page = KeyFreePage(m)
    assert module.TEXT_UNSUPPORTED in _labels(page.notice_card)
    assert not page.scan_btn.isEnabled()
    assert not page.dk_card.isVisibleTo(page)
    page._ask_start()
    page._start()


def test_windows_mark_and_recommend(make_module: Any) -> None:
    """AC-4: Windows キーと PrintScreen の空きの組に印があり、おすすめに入らない。"""
    m, ctx = make_module({"include_win": True, "recommend_count": 20,
                          "recommend_order": ["Win+Ctrl", "Ctrl+Alt"]})
    page = KeyFreePage(m)
    _scan(page, ctx)
    assert len(page.groups) == 11
    win_card = next(g for g in page.groups if g.mods == WIN | C)
    assert all(cap.windows for cap in win_card.caps.values())
    assert page.groups[0].caps[(C | A, 0x2C)].windows
    assert not page.groups[0].caps[(C | A, K)].windows
    recs = m.recommendations()
    assert recs and all(not combos.windows_mark(*c) for c in recs)
    assert recs[0] == (C | A, 0x41)


def test_symbol_hidden_from_table(make_module: Any) -> None:
    from deskkit.modules.keyfree.fakes import full_symbols

    chars = full_symbols()
    del chars[0xE2]
    m, ctx = make_module(chars=chars)
    page = KeyFreePage(m)
    _scan(page, ctx)
    assert all((mods, 0xE2) not in g.caps for g in page.groups for mods in [g.mods])
    assert len(page.groups[0].caps) == 87


def test_deskkit_card_failed_and_recommend(make_module: Any) -> None:
    m, ctx = make_module()
    ctx.hotkeys.held["jotdrop.open_input"] = (C | A | S, J)
    ctx.hotkeys.failed = [FailedKey("host.quick", C | A, SPACE, 1409)]
    ctx.hotkeys.used.add((C | A, SPACE))
    page = KeyFreePage(m)
    text = _labels(page.dk_card)
    assert "一行メモを書く" in text and "クイックアクション" in text
    assert "ほかのアプリが使っているため、DeskKit では使えていません。" in text
    assert "「調べる」を押すと、代わりの候補を3つ出します。" in text
    _scan(page, ctx)
    btns = [b.text() for b in page.dk_card.findChildren(type(page.scan_btn))]
    assert sum(1 for t in btns if "Ctrl+Alt+Shift+" in t) == 3


def test_single_check_messages(make_module: Any) -> None:
    m, ctx = make_module()
    ctx.hotkeys.used.add((C | A, SPACE))
    page = KeyFreePage(m)
    page.one_edit.setText("K")
    page._check_one()
    assert page.one_result.text() == module.TEXT_NO_MODS
    page.one_edit.setText("Ctrl+Alt+Nope")
    page._check_one()
    assert page.one_result.text() == module.TEXT_BAD_SYNTAX
    page.one_edit.setText("Ctrl+Alt+Space")
    page._check_one()
    assert "調べています" in page.one_result.text()
    ctx.hotkeys.flush()
    assert page.one_result.text().startswith("Ctrl+Alt+Space: 使用中")
    page.one_edit.setText("Ctrl+Alt+F12")
    page._check_one()
    assert page.one_result.text() == "Ctrl+Alt+F12: 調べられない(Windows 用)"


def test_try_key_flow_on_page(make_module: Any) -> None:
    m, ctx = make_module()
    page = KeyFreePage(m)
    _scan(page, ctx)
    page.select((C | A, K))
    page._try((C | A, K))
    assert m.trykey.active == (C | A, K)
    assert "今、このキーを押してみてください" in _labels(page.groups[0].detail)
    ctx.hotkeys.fire(trykey.HOTKEY_NAME)
    assert "届きました。このキーは使えます" in _labels(page.groups[0].detail)


def test_page_rebuilt_after_module_changes(make_module: Any) -> None:
    """結果はモジュールが持つので、画面を作り直しても残る(K-8: 「N 分前に調べた結果」)。"""
    m, ctx = make_module()
    _scan(KeyFreePage(m), ctx)
    page2 = KeyFreePage(m)
    assert len(page2.groups) == 4
    assert "調べた結果" in page2.pill.text()


def _fake_quick(ctx: Any, ok: bool = True) -> list[str]:
    """ctx.set_quick_action_hotkey の偽物(v0.4.1)。ok なら host.quick をその組で持ったことにする。"""
    got: list[str] = []

    def set_quick(text: str, *, revert_on_fail: bool = False) -> bool:
        assert revert_on_fail          # KeyFree から選んだときは、取れなければ前のキーに戻してもらう
        got.append(text)
        if ok:
            ctx.hotkeys.failed = []
            ctx.hotkeys.held["host.quick"] = ctx.hotkeys.parse(text)
        return ok

    ctx.set_quick_action_hotkey = set_quick
    return got


def test_quick_action_from_deskkit_card(make_module: Any) -> None:
    """v0.4.1: クイックアクションが取れていないとき、候補を押すとそのキーに変わる。"""
    m, ctx = make_module()
    ctx.hotkeys.failed = [FailedKey("host.quick", C | A, SPACE, 1409)]
    ctx.hotkeys.used.add((C | A, SPACE))
    got = _fake_quick(ctx)
    page = KeyFreePage(m)
    assert "「調べる」を押すと、代わりの候補を3つ出します。" in _labels(page.dk_card)
    _scan(page, ctx)
    assert "下の空いている組を押すと、そのキーに変えます。" in _labels(page.dk_card)
    btns = [b for b in page.dk_card.findChildren(type(page.scan_btn)) if b.text().endswith(" にする")]
    assert len(btns) == 3
    target = m.recommendations(3)[0]
    btns[0].click()
    assert got == [m.format(target)]
    assert page.toast.text() == f"クイックアクションのキーを {m.format(target)} にしました"
    res = m.result()
    assert res is not None and res.state(target) == scan.DESKKIT and res.held_names[target] == "host.quick"
    assert "ほかのアプリが使っているため" not in _labels(page.dk_card)
    # もう一度ほかの組にすると、前の組は「まだ調べていない」に戻る
    other = m.recommendations(3)[0]
    assert m.use_for_quick_action(other) == module.QUICK_OK
    assert res.state(other) == scan.DESKKIT and res.state(target) == scan.PENDING
    assert list(res.held_names.values()).count("host.quick") == 1


def test_quick_action_from_detail_conflict(make_module: Any) -> None:
    """空きの組の詳しい欄の「クイックアクションに使う」。直前に取られていたら使用中に直して知らせる。"""
    m, ctx = make_module()
    got = _fake_quick(ctx, ok=False)
    page = KeyFreePage(m)
    _scan(page, ctx)
    page.groups[3].caps[(C | A | S, K)].click()
    b = next(b for b in page.groups[3].detail.findChildren(type(page.scan_btn)) if "クイックアクションに使う" in b.text())
    b.click()
    assert got == ["Ctrl+Alt+Shift+K"]
    assert page.toast.text() == "Ctrl+Alt+Shift+K は今ほかのアプリが使っています。クイックアクションのキーは前のままです"
    assert m.result().state((C | A | S, K)) == scan.USED  # type: ignore[union-attr]


def test_quick_action_old_host(make_module: Any) -> None:
    m, ctx = make_module()
    _scan(KeyFreePage(m), ctx)
    assert m.use_for_quick_action((C | A | S, K)) == module.QUICK_UNSUPPORTED   # 古い本体(ctx に入口が無い)


def test_settings_controls(make_module: Any) -> None:
    m, ctx = make_module()
    page = KeyFreePage(m)
    page.sw_win.setChecked(True)
    assert m.cfg["include_win"] is True and ctx.writes[-1]["include_win"] is True
    page.order.insertItem(0, page.order.takeItem(3))
    page._save_order()
    assert m.cfg["recommend_order"][0] == "Ctrl+Shift"
