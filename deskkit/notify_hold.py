# 通知の保留(H1)。前面がゲーム・全画面の間は error 以外の通知を出さずにためておき、前面が安全になったら
# 「保留中の通知 N 件」を1件にまとめて出す(1件だけなら元の通知をそのまま出す)。前面の確認は保留中だけ 2 秒ごと。
# 通知の記録(Control Center の「最近の通知」)は保留とは関係なくすぐに行う(host 側)。
from __future__ import annotations

import datetime as _dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QObject, QTimer

POLL_MS = 2000
MAX_HELD = 100       # メモリに持つ上限(件数は数え続ける)
SUMMARY_TITLES = 3   # まとめ通知の本文に載せるタイトルの数(新しい順)


@dataclass
class Note:
    source: str
    title: str
    text: str
    on_click: Callable[[], Any] | None
    level: str
    ts: _dt.datetime = field(default_factory=_dt.datetime.now)


class NotificationHold(QObject):
    """is_busy() が True の間は offer() された通知をためる。show(note) で実際に出す。"""

    def __init__(self, is_busy: Callable[[], bool], enabled: Callable[[], bool], show: Callable[[Note], None],
                 summarize: Callable[[list[Note], int], Note], poll_ms: int = POLL_MS) -> None:
        super().__init__()
        self._is_busy = is_busy
        self._enabled = enabled
        self._show = show
        self._summarize = summarize
        self._held: list[Note] = []
        self._count = 0
        self._timer = QTimer(self)
        self._timer.setInterval(poll_ms)
        self._timer.timeout.connect(self._tick)

    @property
    def count(self) -> int:
        return self._count

    def offer(self, note: Note) -> bool:
        """保留したら True。False なら呼び出し側がすぐに出す(保留中のものがあれば先にまとめて出しておく)。"""
        if note.level != "error" and self._enabled() and self._busy():
            self._held.append(note)
            del self._held[:-MAX_HELD]
            self._count += 1
            if not self._timer.isActive():
                self._timer.start()
            return True
        if self._count and note.level != "error":
            self.flush()
        return False

    def _busy(self) -> bool:
        try:
            return bool(self._is_busy())
        except Exception:  # noqa: BLE001 - 判定できなければ保留しない(通知を失わない)
            return False

    def _tick(self) -> None:
        if not self._count:
            self._timer.stop()
            return
        if not self._enabled() or not self._busy():
            self.flush()

    def flush(self) -> None:
        held, n = self._held, self._count
        self._held, self._count = [], 0
        self._timer.stop()
        if not n:
            return
        note = held[-1] if n == 1 and held else self._summarize(held, n)
        self._show(note)


def summary_note(held: list[Note], total: int, title_of: Callable[[str], str],
                 on_click: Callable[[], Any] | None) -> Note:
    """「保留中の通知 N 件」。本文は新しい順に最新数件のタイトル(本文は載せない)。"""
    latest = list(reversed(held))[:SUMMARY_TITLES]
    lines = [f"・{title_of(n.source)}: {n.title}" for n in latest]
    if total > len(latest):
        lines.append(f"ほか {total - len(latest)} 件")
    level = "warn" if any(n.level == "warn" for n in held) else "info"
    return Note("host", f"保留中の通知 {total} 件", "\n".join(lines), on_click, level)
