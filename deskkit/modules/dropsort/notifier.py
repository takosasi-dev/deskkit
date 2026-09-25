# 通知の保留(D-12 / FR-23): foreground がゲームか全画面の間は通知を出さずに溜め、外れたらまとめて1件で出す。
# 移動処理そのものは止めない。判定は host の ctx.foreground() に一本化する(C-4)。
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_LEVEL_ORDER = {"info": 0, "ok": 1, "warn": 2, "error": 3}


@dataclass
class _Note:
    title: str
    text: str
    level: str
    on_click: Callable[[], None] | None


class Notifier:
    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._held: list[_Note] = []

    def blocked(self) -> bool:
        try:
            fi = self._ctx.foreground()
        except Exception:  # noqa: BLE001 - 判定できなければ通知を出す側に倒す(通知は foreground への作用ではない)
            return False
        return bool(fi.is_game) or fi.is_fullscreen is True

    @property
    def held_count(self) -> int:
        return len(self._held)

    def push(self, title: str, text: str, level: str = "info", on_click: Callable[[], None] | None = None) -> None:
        if self._held or self.blocked():
            self._held.append(_Note(title, text, level, on_click))
            self.poll()
            return
        self._ctx.notify(title, text, on_click, level=level)

    def poll(self) -> None:
        """タイマーから呼ぶ。条件が外れていれば保留分を1件にまとめて出す。"""
        if not self._held or self.blocked():
            return
        notes, self._held = self._held, []
        if len(notes) == 1:
            n = notes[0]
            self._ctx.notify(n.title, n.text, n.on_click, level=n.level)
            return
        level = max((n.level for n in notes), key=lambda lv: _LEVEL_ORDER.get(lv, 0))
        lines = [f"・{n.title}" for n in notes[:4]]
        if len(notes) > 4:
            lines.append(f"ほか {len(notes) - 4} 件")
        click = next((n.on_click for n in notes if n.on_click is not None), None)
        self._ctx.notify(f"保留していた通知 {len(notes)} 件", "\n".join(lines), click, level=level)
