# モジュールの流れ: ホットキー(FR-1・FR-2・J-1)・ゲームと全画面(AC-10)・元の窓へ戻す(AC-11)・IME の Enter(AC-9)・
# 書き込み先の確かめ(AC-14)・日付の切り替え(AC-13)・使用中の預かりと順番(AC-4 の偽物版)・取り消し(FR-15)・照合(AC-16)・
# 秘密の文字列が残らないこと(AC-15)・usage・diagnostics・画面。
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QInputMethodEvent, QKeyEvent
from PySide6.QtWidgets import QApplication

from deskkit.modules.jotdrop import target
from deskkit.modules.jotdrop.fakes import FakeWin32, norm
from deskkit.modules.jotdrop.module import HOTKEY_NAME, hotkey_problem, normalize

from .conftest import DOCS, NOTES

P = NOTES + "\\2026-09-26.md"


def send(m: Any, text: str) -> str:
    m.open_popup(500)
    return str(m.on_submit(text))


def ops(ctx: Any) -> list[dict[str, Any]]:
    p = ctx.data_dir / "ops.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


# ------------------------------------------------------------------ 設定・ホットキー
def test_defaults_and_normalize() -> None:
    today = datetime(2026, 9, 26, 12, 0)
    cfg, changed = normalize({}, today)
    assert changed and cfg["hotkey"] == "" and cfg["file_pattern"] == "{date}.md" and cfg["line_format"] == "- {time} {text}"
    cfg2, _ = normalize({"max_chars": 99999, "undo_minutes": 0, "line_format": "no text", "newline": "cr",
                         "file_pattern": "CON.md", "blank_line_before": 1, "separator": "a\nb"}, today)
    assert cfg2["max_chars"] == 5000 and cfg2["undo_minutes"] == 1 and cfg2["line_format"] == "- {time} {text}"
    assert cfg2["newline"] == "auto" and cfg2["file_pattern"] == "{date}.md" and cfg2["blank_line_before"] is False
    assert cfg2["separator"] == ""


def test_fr1_hotkey_registered_with_name(make_module: Any) -> None:
    m, ctx, _a, _c = make_module({"hotkey": "Ctrl+Alt+J"})
    assert ctx.hotkeys.registered == [(HOTKEY_NAME, "Ctrl+Alt+J")] and HOTKEY_NAME == "jotdrop.open_input"
    assert m.hotkey_ok is True and HOTKEY_NAME in ctx.hotkeys.callbacks


def test_fr1_no_hotkey_by_default(make_module: Any) -> None:
    m, ctx, _a, _c = make_module()
    assert ctx.hotkeys.registered == [] and m.hotkey_ok is None
    assert ctx.status == "キー未設定"
    assert [lb for lb, _ in ctx.tray] == ["メモを書く", "直前のメモを取り消す", "書き込み先を開く"]


def test_j1_conflict_keeps_setting(make_module: Any) -> None:
    m, ctx, _a, _c = make_module({"hotkey": "Ctrl+Alt+J"}, start=False)
    ctx.hotkeys.result = False
    m.start()
    assert m.hotkey_ok is False and "ほかのアプリが使っています" in (m.hotkey_error or "")
    assert ctx.settings()["hotkey"] == "Ctrl+Alt+J"   # 設定の値は消さない
    d = m.diagnostics()
    assert d["hotkey_set"] is True and d["hotkey_registered"] is False


@pytest.mark.parametrize("text", ["F12", "Ctrl+F12", "Win+J", "Ctrl+Win+J"])
def test_j1_reserved_keys(text: str, make_module: Any) -> None:
    assert hotkey_problem(text) is not None
    m, ctx, _a, _c = make_module({"hotkey": text})
    assert ctx.hotkeys.registered == [] and m.hotkey_ok is False


def test_fr2_hotkey_toggles_popup_and_keeps_draft(make_module: Any) -> None:
    m, ctx, _a, _c = make_module({"hotkey": "Ctrl+Alt+J"})
    ctx.hotkeys.fire(HOTKEY_NAME)
    assert m.popup.isVisible()
    m.popup.edit.setText("書きかけ")
    ctx.hotkeys.fire(HOTKEY_NAME)
    assert not m.popup.isVisible() and m.draft == "書きかけ"
    ctx.hotkeys.fire(HOTKEY_NAME)
    assert m.popup.edit.text() == "書きかけ"   # FR-6
    m.popup.edit.escape.emit()
    assert not m.popup.isVisible() and m.draft == "書きかけ"


def test_popup_prebuilt_after_start_or_on_demand(make_module: Any) -> None:
    m, ctx, _a, _c = make_module({"hotkey": "Ctrl+Alt+J"}, start=False)
    m.start()
    assert m.popup is None and any(t[0] == 3_000 and t[2] for t in ctx.timers)   # 起動を遅らせない
    ctx.hotkeys.fire(HOTKEY_NAME)                                                 # タイマーより先に押された
    assert m.popup is not None and m.popup.isVisible()
    built = m.popup
    ctx.run_timers(3_000)
    assert m.popup is built                                                       # 作り直さない


def test_popup_title_never_has_text(make_module: Any) -> None:
    m, _ctx, _a, _c = make_module()
    m.open_popup(500)
    m.popup.edit.setText("ひみつXYZ")
    assert m.popup.windowTitle() == "JotDrop" and m.done_mark.windowTitle() == "JotDrop"


# ------------------------------------------------------------------ AC-10
@pytest.mark.parametrize("kw", [{"is_game": True}, {"is_fullscreen": True}, {"is_fullscreen": None}])
def test_ac10_blocked(kw: dict[str, Any], make_module: Any) -> None:
    from dataclasses import replace

    m, ctx, a, _c = make_module({"hotkey": "Ctrl+Alt+J"})
    ctx.fg = replace(ctx.fg, **kw)
    for _ in range(3):
        ctx.hotkeys.fire(HOTKEY_NAME)
    assert not m.popup.isVisible()
    assert [r["event"] for r in ops(ctx)] == ["blocked"] * 3
    assert len([n for n in ctx.notifications if "全画面" in n[1]]) == 1   # 1回の起動で1回だけ
    assert a.set_calls == [] and m.diagnostics()["blocked"] == 3


def test_elevated_foreground_still_opens(make_module: Any) -> None:
    from dataclasses import replace

    m, ctx, _a, _c = make_module({"hotkey": "Ctrl+Alt+J"})
    ctx.fg = replace(ctx.fg, is_elevated=True)
    ctx.hotkeys.fire(HOTKEY_NAME)
    assert m.popup.isVisible()


# ------------------------------------------------------------------ AC-11・J-4
def test_ac11_restore_once_after_submit(make_module: Any) -> None:
    m, ctx, a, _c = make_module({"hotkey": "Ctrl+Alt+J"})
    ctx.hotkeys.fire(HOTKEY_NAME)
    assert m.on_submit("メモ") == "sent"
    assert a.set_calls == [500]


def test_ac11_restore_after_esc_and_hotkey(make_module: Any) -> None:
    m, ctx, a, _c = make_module({"hotkey": "Ctrl+Alt+J"})
    ctx.hotkeys.fire(HOTKEY_NAME)
    m.popup.edit.escape.emit()
    ctx.hotkeys.fire(HOTKEY_NAME)
    ctx.hotkeys.fire(HOTKEY_NAME)
    assert a.set_calls == [500, 500]


def test_ac11_no_restore_cases(make_module: Any) -> None:
    from dataclasses import replace

    m, ctx, a, _c = make_module({"hotkey": "Ctrl+Alt+J"})
    ctx.fg = replace(ctx.fg, hwnd=600)          # DeskKit 自身の窓(同じプロセス)
    ctx.hotkeys.fire(HOTKEY_NAME)
    m.on_submit("a")
    ctx.fg = replace(ctx.fg, hwnd=700)          # もう無い窓
    ctx.hotkeys.fire(HOTKEY_NAME)
    m.on_submit("b")
    ctx.fg = replace(ctx.fg, hwnd=500)
    ctx.hotkeys.fire(HOTKEY_NAME)
    m.close_popup("outside")                    # 外をクリックして閉じた
    ctx.tray_cb("メモを書く")()                   # トレイから開いた(覚えていない)
    m.on_submit("c")
    assert a.set_calls == []


def test_restore_refused_logged(make_module: Any) -> None:
    m, ctx, a, _c = make_module({"hotkey": "Ctrl+Alt+J"})
    a.set_result = False
    ctx.hotkeys.fire(HOTKEY_NAME)
    m.on_submit("x")
    assert a.set_calls == [500] and "restore foreground refused" in ctx.handler.text()


# ------------------------------------------------------------------ AC-9・J-6
class ClockS:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def _enter(w: Any, auto: bool = False) -> None:
    ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier, "\r", auto)
    QApplication.sendEvent(w, ev)


def test_ac9_ime_enter(qapp: Any) -> None:
    from deskkit.modules.jotdrop.popup import MemoEdit

    clk = ClockS()
    e = MemoEdit(clk)
    sent: list[int] = []
    e.submit.connect(lambda: sent.append(1))
    QApplication.sendEvent(e, QInputMethodEvent("へんかん", []))
    _enter(e)
    assert sent == []                                   # 変換中
    commit = QInputMethodEvent("", [])
    commit.setCommitString("変換")
    QApplication.sendEvent(e, commit)
    assert e.text() == "変換" and e.preedit == ""
    clk.t += 0.05
    _enter(e)
    assert sent == []                                   # 確定から 100ms 以内
    clk.t = 100.125
    _enter(e, auto=True)
    assert sent == []                                   # 押しっぱなしの繰り返し
    _enter(e)
    assert sent == [1]                                  # 確定から 100ms 後


def test_ctrl_z_only_when_empty(qapp: Any) -> None:
    from deskkit.modules.jotdrop.popup import MemoEdit

    e = MemoEdit()
    hits: list[int] = []
    e.undo_key.connect(lambda: hits.append(1))
    z = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier, "z")
    QApplication.sendEvent(e, z)
    e.setText("x")
    QApplication.sendEvent(e, z)
    assert hits == [1]


# ------------------------------------------------------------------ 書く・FR-5・FR-7・FR-16
def test_send_writes_and_marks(make_module: Any) -> None:
    m, ctx, a, _c = make_module()
    assert send(m, "  ログインの不具合は\nキャッシュが原因かも  ") == "sent"
    assert a.get(P) == "- 14:05 ログインの不具合は キャッシュが原因かも\n".encode()
    assert ctx.status.startswith("最後のメモ 14:05")
    assert [r["event"] for r in ops(ctx)] == ["sent"]
    assert m.today_count() == 1 and m.draft == ""
    assert any(t[0] == 10_000 and t[2] for t in ctx.timers)   # 10 秒後の照合


def test_fr5_empty_not_sent(make_module: Any) -> None:
    m, _ctx, a, _c = make_module()
    assert send(m, "   \n ") == "empty"
    assert "空のメモは書きません" in m.popup.msg.text() and a.get(P) is None


def test_fr7_max_chars_truncates_paste(make_module: Any) -> None:
    m, _ctx, _a, _c = make_module({"max_chars": 20})
    m.open_popup(500)
    m.popup.edit.setText("x" * 50)
    assert m.popup.edit.text() == "x" * 20 and "20 文字までです" in m.popup.msg.text()


def test_ac13_rollover_uses_enter_time(make_module: Any) -> None:
    m, _ctx, a, clock = make_module(when=datetime(2026, 9, 26, 23, 59, 59))
    m.open_popup(500)
    clock.advance(2)
    m.on_submit("深夜")
    assert a.get(NOTES + "\\2026-09-27.md") == "- 00:00 深夜\n".encode() and a.get(P) is None


def test_ac14_first_write_confirms_target(make_module: Any) -> None:
    m, ctx, a, _c = make_module({"target_confirmed": False})
    m.open_popup(500)
    assert m.on_submit("一回目") == "confirm"
    assert a.get(P) is None and P in m.popup.msg.text() and "もう一度 Enter" in m.popup.msg.text()
    assert m.on_submit("一回目") == "sent"
    assert a.get(P) == "- 14:05 一回目\n".encode()
    assert ctx.settings()["target_confirmed"] is True
    assert send(m, "二回目") == "sent"       # 2回目以降は確かめない
    err = m.save_settings({"folder": "C:\\Other"})
    assert err is None and m.cfg["target_confirmed"] is False


def test_default_folder_is_created_only_default(make_module: Any) -> None:
    m, _ctx, a, _c = make_module({"folder": ""})
    send(m, "既定")
    assert a.is_dir(DOCS + "\\JotDrop") and a.get(DOCS + "\\JotDrop\\2026-09-26.md") == "- 14:05 既定\n".encode()
    m2, _ctx2, a2, _c2 = make_module({"folder": "C:\\Missing"})
    send(m2, "x")
    assert not a2.is_dir("C:\\Missing") and m2.pending.items()[0].reason == target.REASON_FOLDER_MISSING


# ------------------------------------------------------------------ AC-4(偽物)・FR-11・FR-12
def test_ac4_busy_then_retry_keeps_order(make_module: Any) -> None:
    m, ctx, a, clock = make_module()
    a.put(P, b"# log\n")
    a.locked.add(norm(P))
    send(m, "一件目")
    assert a.get(P) == b"# log\n"
    items = m.pending.items()
    assert len(items) == 1 and items[0].reason == target.REASON_BUSY
    n = ctx.notifications[-1]
    assert n[2] == "warn" and "あとで自動で書きます" in n[1] and n[3] is not None
    clock.advance(60)
    send(m, "二件目")                         # まだ使用中: 二件目も預かる(先に書かない)
    assert a.get(P) == b"# log\n" and len(m.pending.items()) == 2
    a.locked.clear()
    ctx.snoozed = True
    assert m.retry_pending("timer") is False  # 一時停止中は自動で書かない
    ctx.snoozed = False
    assert m.retry_pending("timer") is True
    assert a.get(P) == "# log\n- 14:05 一件目\n- 14:06 二件目\n".encode()
    assert m.pending.count() == 0
    assert [r["event"] for r in ops(ctx)] == ["pending", "pending", "retry_ok", "retry_ok"]


def test_pending_written_before_newer_memo(make_module: Any) -> None:
    m, _ctx, a, clock = make_module()
    a.locked.add(norm(P))
    send(m, "古い")
    a.locked.clear()
    clock.advance(60)
    send(m, "新しい")                          # 書く前に同じファイルの預かり分を先に書く(J-9)
    assert a.get(P) == "- 14:05 古い\n- 14:06 新しい\n".encode()


def test_non_auto_reasons_not_retried(make_module: Any) -> None:
    m, ctx, a, _c = make_module()
    a.put(P, "メモ帳".encode("cp932"))
    send(m, "x")
    assert m.pending.items()[0].reason == target.REASON_NOT_UTF8
    assert "もう一度書く" in ctx.notifications[-1][1]
    assert m.retry_pending("timer") is False
    a.put(P, b"")
    m.rewrite(m.pending.items()[0].id)         # 手で「もう一度書く」
    assert m.pending.count() == 0 and a.get(P) == b"- 14:05 x\n"


def test_retry_on_hotkey_and_start(make_module: Any) -> None:
    a = FakeWin32()
    m, ctx, a, _c = make_module({"hotkey": "Ctrl+Alt+J"}, api=a)
    a.locked.add(norm(P))
    send(m, "x")
    a.locked.clear()
    ctx.hotkeys.fire(HOTKEY_NAME)            # 次にホットキーが押されたとき
    assert m.pending.count() == 0
    # 起動時: 預かりが残っていれば書く
    a.locked.add(norm(P))
    send(m, "y")
    a.locked.clear()
    m.stop()
    m2, _ctx2, _a2, _c2 = make_module(api=a, start=False)
    m2.pending = m.pending
    m2.start()
    assert m2.pending.count() == 0 and (a.get(P) or b"").endswith(b"- 14:05 y\n")


def test_stop_stores_unwritten(make_module: Any) -> None:
    from deskkit.modules.jotdrop import writer

    m, _ctx, _a, _c = make_module()
    job = writer.Job("left", datetime(2026, 9, 26, 14, 5), P, "- 14:05 残り")
    m.worker.stop = lambda wait_s=2.0: [writer.Task("call", job=job)]  # type: ignore[method-assign]
    m.stop()
    assert m.pending.get("left") is not None and m.pending.get("left").reason == target.REASON_BUSY  # type: ignore[union-attr]


# ------------------------------------------------------------------ FR-15・J-18
def test_undo_restores_text_and_file(make_module: Any) -> None:
    m, ctx, a, _c = make_module()
    a.put(P, b"keep\n")
    send(m, "取り消す文")
    assert m.undo_label() == "14:05"
    m.open_popup(500)
    assert "14:05 の1行を取り消す" in m.popup.undo_link.text()
    m.popup.edit.clear()
    m.popup.edit.undo_key.emit()
    assert a.get(P) == b"keep\n" and m.popup.edit.text() == "取り消す文"
    assert m.undo_label() is None
    assert [r["event"] for r in ops(ctx)] == ["sent", "undo"]


def test_undo_time_limit_and_changed(make_module: Any) -> None:
    m, ctx, a, clock = make_module({"undo_minutes": 1})
    send(m, "x")
    clock.advance(61)
    assert m.undo_label() is None
    ctx.tray_cb("直前のメモを取り消す")()
    assert "取り消せるメモはありません" in ctx.notifications[-1][1]
    m2, ctx2, a2, _c2 = make_module()
    send(m2, "y")
    a2.files[norm(P)] += b"edited\n"
    m2.undo_last()
    assert "そのあとファイルが変わったので取り消せません" in ctx2.notifications[-1][1]
    assert (a2.get(P) or b"").endswith(b"edited\n")
    assert ops(ctx2)[-1]["event"] == "undo_refused" and ops(ctx2)[-1]["reason"] == "changed"


def test_undo_pending_memo(make_module: Any) -> None:
    m, _ctx, a, _c = make_module()
    a.locked.add(norm(P))
    send(m, "まだ書いていない")
    assert m.undo_label() == "14:05"
    m.undo_last()
    assert m.pending.count() == 0 and m.popup.edit.text() == "まだ書いていない"


def test_undo_busy(make_module: Any) -> None:
    m, ctx, a, _c = make_module()
    send(m, "x")
    a.locked.add(norm(P))
    m.undo_last()
    assert "今は取り消せません" in ctx.notifications[-1][1]


# ------------------------------------------------------------------ AC-16・J-19
def test_ac16_verify_missing_not_rewritten(make_module: Any) -> None:
    m, ctx, a, _c = make_module()
    send(m, "消される行")
    a.put(P, b"")                              # 外から消された
    ctx.run_timers(10_000)
    items = m.pending.items()
    assert len(items) == 1 and items[0].reason == target.REASON_MISSING
    assert ops(ctx)[-1]["event"] == "missing"
    assert ctx.notifications[-1][2] == "warn"
    assert m.retry_pending("timer") is False and a.get(P) == b""   # 自動では書き直さない


def test_undo_before_verify_is_not_missing(make_module: Any) -> None:
    # 書いて 10 秒以内に取り消しても、照合で「見つからない」にして預かりに戻さない
    m, ctx, a, _c = make_module()
    send(m, "すぐ取り消す")
    m.undo_last()
    assert a.get(P) == b""
    n_before = len(ctx.notifications)
    ctx.run_timers(10_000)
    assert m.pending.count() == 0
    assert [r["event"] for r in ops(ctx)] == ["sent", "undo"]
    assert len(ctx.notifications) == n_before


def test_undo_pending_after_retry_wrote_it(make_module: Any) -> None:
    # 預かり分の取り消しより先に書き直しが済んだら、取り消さずに知らせる(同じメモが2件にならない)
    m, ctx, a, _c = make_module()
    a.locked.add(norm(P))
    send(m, "預かり")
    a.locked.clear()
    orig = m.worker.submit

    def submit(task: Any) -> None:
        m._retry_task()                          # 列の先にいた自動の書き直しが先に書く
        orig(task)

    m.worker.submit = submit
    m.undo_last()
    assert "書き込みが済みました" in ctx.notifications[-1][1]
    assert a.get(P) == "- 14:05 預かり\n".encode() and m.pending.count() == 0
    assert "undo" not in [r["event"] for r in ops(ctx)]


def test_verify_found_keeps_quiet(make_module: Any) -> None:
    m, ctx, _a, _c = make_module()
    send(m, "残る行")
    ctx.run_timers(10_000)
    assert m.pending.count() == 0 and not ctx.notifications


# ------------------------------------------------------------------ AC-15・INV-3
def test_ac15_no_secret_text_or_path(make_module: Any, tmp_path: Any) -> None:
    secret, pathmark = "ひみつXYZ", "PATHMARK"
    folder = f"C:\\{pathmark}"
    a = FakeWin32()
    a.add_dir(folder)
    m, ctx, a, clock = make_module({"folder": folder, "hotkey": "Ctrl+Alt+J"}, api=a)
    fpath = f"{folder}\\2026-09-26.md"
    send(m, f"{secret} 1")                       # 送る
    ctx.run_timers(10_000)                       # 照合(見つかる)
    m.undo_last()                                # 取り消す
    a.locked.add(norm(fpath))
    send(m, f"{secret} 2")                       # 預かる
    a.locked.clear()
    m.retry_pending("timer")
    a.put(fpath, b"")
    ctx.run_timers(10_000)                       # 照合(消えた)
    clock.advance(1)
    blobs = [ctx.handler.text(), (ctx.data_dir / "ops.jsonl").read_text(encoding="utf-8"), repr(m.diagnostics()),
             repr([(s.key, s.label, s.hint) for s in m.usage(7)]), repr(ctx.notifications), ctx.status,
             m.popup.windowTitle()]
    for b in blobs:
        assert secret not in b and pathmark not in b and pathmark.lower() not in b.lower(), b[:200]
    # 本文とパスを持つのは pending.jsonl だけ
    assert secret in (ctx.data_dir / "pending.jsonl").read_text(encoding="utf-8")


# ------------------------------------------------------------------ FR-18・FR-20・FR-21・FR-22
def test_fr18_open_target(make_module: Any, tmp_path: Any) -> None:
    m, _ctx, _a, _c = make_module({"folder": str(tmp_path)})
    assert m.open_target() is True and m.opened == [str(tmp_path)]
    f = tmp_path / "2026-09-26.md"
    f.write_text("x", encoding="utf-8")
    assert m.open_target() is True and m.opened[-1] == str(f)
    m2, ctx2, _a2, _c2 = make_module({"folder": str(tmp_path / "missing")})
    assert m2.open_target() is False and m2.opened == []


def test_fr20_ops_pruned_on_start(make_module: Any) -> None:
    m, ctx, _a, _c = make_module(start=False)
    base = datetime(2026, 9, 26, 14, 5).astimezone()      # 偽の時計の今
    old = (base - timedelta(days=91)).isoformat(timespec="seconds")
    new = (base - timedelta(days=89)).isoformat(timespec="seconds")
    (ctx.data_dir / "ops.jsonl").write_text(
        json.dumps({"ts": old, "event": "sent", "reason": None, "retries": 0, "ms": 1}) + "\n"
        + json.dumps({"ts": new, "event": "sent", "reason": None, "retries": 0, "ms": 1}) + "\nbroken\n", encoding="utf-8")
    m.start()
    rows = ops(ctx)
    assert len(rows) == 1 and rows[0]["ts"] == new


def test_fr21_usage_and_fr22_diagnostics(make_module: Any) -> None:
    m, ctx, _a, _c = make_module({"hotkey": "Ctrl+Alt+J"})
    ctx.run_timers(10_000)
    send(m, "a")
    send(m, "b")
    m.undo_last()
    u = m.usage(7)
    # 記録の時刻は偽の時計(2026-09-26)なので、日の位置ではなく合計で見る
    assert u[0].key == "sent" and u[0].primary and sum(u[0].per_day) == 2 and "R-2" in (u[0].hint or "")
    assert u[1].key == "undo" and sum(u[1].per_day) == 1 and len(u[0].per_day) == 7
    d = m.diagnostics()
    assert d == {"hotkey_set": True, "hotkey_registered": True, "target_default": False, "pattern_has_date": True,
                 "format": "bullet", "target_confirmed": True, "pending": 0, "last_result": "sent", "blocked": 0}
    assert all(isinstance(v, (str, int, bool)) for v in d.values())


def test_handle_cli_unsupported(make_module: Any) -> None:
    m, _ctx, _a, _c = make_module()
    assert m.handle_cli(["open", "x"]) == (2, "unsupported")


# ------------------------------------------------------------------ 画面(FR-19)
def test_page_builds_and_width(make_module: Any) -> None:
    from deskkit.modules.jotdrop.page import JotDropPage

    m, _ctx, a, _c = make_module()
    a.locked.add(norm(P))
    send(m, "預かりの行")
    page = m.create_page()
    assert isinstance(page, JotDropPage)
    assert page.banner.isVisibleTo(page)                 # キー未設定の案内(J-1)
    assert page.table.rowCount() == 1 and page.table.item(0, 1).text() == "- 14:05 預かりの行"
    assert page.tile_pending.value.text() == "1"
    assert "C:\\Notes\\" in page.pv_path.text() and "- " in page.pv_existing[1].text()
    page.resize(1000, 800)
    page.show()
    QApplication.processEvents()
    assert page.minimumSizeHint().width() <= 900 and page.widget().minimumSizeHint().width() <= 900
    page.close()


def test_page_saves_settings(make_module: Any) -> None:
    m, ctx, _a, _c = make_module()
    page = m.create_page()
    page.preset.set_value("heading", emit=True)
    assert m.cfg["line_format"] == "### [{time}] {text}" and m.cfg["blank_line_before"] is True
    page.format_edit.setText("{text} only")
    page.format_edit.editingFinished.emit()
    assert m.cfg["line_format"] == "{text} only" and page.preset.value() == "custom"
    page.format_edit.setText("no var")
    page.format_edit.editingFinished.emit()
    assert m.cfg["line_format"] == "{text} only" and page.format_msg.isVisibleTo(page)
    page.pattern_edit.setText("CON.md")
    page.pattern_edit.editingFinished.emit()
    assert m.cfg["file_pattern"] == "{date}.md"
    page.pattern_edit.setText("{date}.txt")
    page.pattern_edit.editingFinished.emit()
    assert m.cfg["file_pattern"] == "{date}.txt" and m.cfg["target_confirmed"] is False
    page._on_hotkey("Ctrl+F12")
    assert m.cfg["hotkey"] == "" and "F12" in page.key_msg.text()
    page._on_hotkey("Ctrl+Alt+J")
    assert ctx.writes[-1] == ({**ctx.writes[-1][0]}, True) and ctx.settings()["hotkey"] == "Ctrl+Alt+J"
    page.close()


def test_page_pending_actions(make_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from deskkit.ui import widgets as W

    m, _ctx, a, _c = make_module()
    a.locked.add(norm(P))
    send(m, "捨てる行")
    page = m.create_page()
    page._copy(m.pending.items()[0].line)
    assert QApplication.clipboard().text() == "- 14:05 捨てる行"
    monkeypatch.setattr(W, "confirm", lambda *a, **k: (True, []))
    page._discard(m.pending.items()[0].id)
    assert m.pending.count() == 0 and page.table.rowCount() == 0
    page.close()


def test_nfr5_import_is_light() -> None:
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    code = ("import sys, deskkit.modules.jotdrop as m; "
            "print(','.join(x for x in ('PySide6.QtWidgets', 'deskkit.modules.jotdrop.module') if x in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=str(root),
                         env={**os.environ})
    assert out.stdout.strip() == ""
