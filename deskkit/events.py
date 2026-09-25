# 同一プロセス内イベントバス(D-8)。同期・メインスレッド・永続化なし。§9.4 の登録済みイベントだけを通す。
# 受け手がいなくても送信側はエラーにしない。未登録の名前はログに警告して捨てる(FR-12)。
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

log = logging.getLogger("deskkit.host.events")

REGISTERED_EVENTS: frozenset[str] = frozenset({
    "layout.apply", "layout.applied", "modeshift.switched",
    # v0.2(docs/INTERFACES_v0.2.md §2)
    "modeshift.reverted",   # ModeShift → {"mode": str, "run_id": str}(戻す前のモード名)
    "host.snooze_changed",  # 本体 → {"snoozed": bool, "until": str | None}(ISO 8601、無期限・QUNS 連動は None)
})
Handler = Callable[[Mapping[str, Any]], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[tuple[str, Handler]]] = {}
        self.delivered = 0  # selftest 用

    def on(self, owner: str, event: str, handler: Handler) -> bool:
        if event not in REGISTERED_EVENTS:
            log.warning("未登録のイベントを購読しようとしました owner=%s event=%s", owner, event)
            return False
        self._handlers.setdefault(event, []).append((owner, handler))
        return True

    def emit(self, owner: str, event: str, payload: Mapping[str, Any]) -> None:
        if event not in REGISTERED_EVENTS:
            log.warning("未登録のイベントを破棄しました owner=%s event=%s", owner, event)
            return
        for _o, h in list(self._handlers.get(event, [])):
            self.delivered += 1
            h(dict(payload))  # h は ModuleContext.safe で包まれている

    def drop_owner(self, owner: str) -> None:
        for ev in list(self._handlers):
            self._handlers[ev] = [(o, h) for (o, h) in self._handlers[ev] if o != owner]
