# 利用状況(MODULE_GUIDE §7)とクイックアクションの検査。usage は復号せずに数えること、
# クイックアクションからパレットを開くときも D-13(ゲーム・全画面では開かない)が効くことを確かめる。
from __future__ import annotations

from typing import Any

from deskkit.foreground import ForegroundInfo
from deskkit.modules.clipshelf import policy
from deskkit.modules.clipshelf._win32 import FMT_EXCLUDE_MONITOR

GAME = ForegroundInfo(0x7001, 5, "game.exe", True, False, False)


def _copy(m: Any, api: Any, text: str, **kw: Any) -> Any:
    api.put(text, **kw)
    return m.monitor.process()


def test_usage_series(module_factory: Any, monkeypatch: Any) -> None:
    m, ctx, api, clock = module_factory({"mode": "record"})
    for i in range(3):
        _copy(m, api, f"a{i}")
    _copy(m, api, "pw", formats={FMT_EXCLUDE_MONITOR: b"\x00"})
    _copy(m, api, "x", owner_exe=None)
    m.open_palette()
    m._palette.dismiss(animated=False)
    m.open_palette()
    m._palette.dismiss(animated=False)
    # usage は復号しない(平文の created_at 列と ops.jsonl だけ)
    calls = {"n": 0}
    orig = m.cipher.unprotect

    def counting(b: bytes) -> bytes:
        calls["n"] += 1
        return orig(b)

    monkeypatch.setattr(m.cipher, "unprotect", counting)
    series = m.usage(7)
    assert calls["n"] == 0
    by = {s.key: s for s in series}
    assert all(len(s.per_day) == 7 for s in series)
    assert by["palette_open"].primary and by["palette_open"].per_day[-1] == 2 and "R-4" in (by["palette_open"].hint or "")
    assert sum(1 for s in series if s.primary) == 1
    assert by["recorded"].per_day[-1] == 3
    assert by["excluded"].per_day[-1] == 2
    assert by["excluded"].extra["by_reason"] == {"除外形式あり": 1, "コピー元が不明": 1}
    assert "auto_paste_sent" not in by  # 自動貼り付けが off で記録も無い
    # ops.jsonl には理由コードだけ(本文なし)
    text = m.ops.path.read_text(encoding="utf-8")
    assert "pw" not in text.replace("pins", "") and '"a0"' not in text


def test_usage_observe_and_auto_paste(module_factory: Any) -> None:
    m, ctx, api, clock = module_factory({"auto_paste": "dry_run"})
    _copy(m, api, "t")
    by = {s.key: s for s in m.usage(30)}
    assert by["observe_only"].per_day[-1] == 1 and by["recorded"].per_day == [0] * 30
    assert "auto_paste_sent" in by and "auto_paste_skipped" in by


def test_quick_actions(module_factory: Any) -> None:
    m, ctx, api, clock = module_factory()
    labels = [q.label for q in ctx.quick]
    # トレイにある操作(パレットを開く・書式なし・全消去)は host が自動で候補に入れるので重複登録しない
    assert "パレットを開く" not in labels and "書式なしにする" not in labels
    pause, resume = ctx.quick_action("記録を一時停止する"), ctx.quick_action("記録を再開する")
    assert pause.enabled() and not resume.enabled()
    pause.callback()
    assert m.paused and not pause.enabled() and resume.enabled()
    resume.callback()
    assert not m.paused
    rec = ctx.quick_action("ClipShelf: 記録を始める(記録モード)")
    assert rec.enabled()
    rec.callback()
    assert m.config.mode == "record" and ctx.writes[-1][0]["mode"] == "record" and not rec.enabled()
    snip = ctx.quick_action("定型文を開く")
    snip.callback()
    assert m._palette is not None and m._palette.isVisible() and m._palette.tabs.index() == 1
    m._palette.dismiss(animated=False)
    assert not ctx.errors


def test_quick_action_palette_respects_d13(module_factory: Any) -> None:
    m, ctx, api, clock = module_factory()
    ctx.fg = GAME  # クイックアクションの窓が閉じて元の foreground(ゲーム)に戻った後に呼ばれる
    ctx.quick_action("定型文を開く").callback()
    assert m._palette is None
    assert m.ops.read()[-1] == {"ts": m.ops.read()[-1]["ts"], "op": "palette_blocked", "reason": "game"}
    assert policy.PAUSED  # 語彙は変えていない
