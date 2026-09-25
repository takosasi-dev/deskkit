# 空き容量の見張り(FR-11・既定オフ)。6 時間ごとにシステムドライブの空きを読み、P3 の warn / bad なら通知する。
# 同じ status の通知は 24 時間に 1 回まで。一時停止中(ctx.is_snoozed())は読まない。最後に読んだ時刻と通知の時刻は
# watch.json に残す(再起動しても 6 時間ごと・24 時間に 1 回を守るため)。パス・ドライブ名は書かない。
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from deskkit.modules.pccheckup.checks.perf import disk_status
from deskkit.modules.pccheckup.probes import DiskInfo

INTERVAL_S = 6 * 3600
RENOTIFY_S = 24 * 3600
FIRST_MIN_DELAY_S = 60   # 起動直後は読まない(起動を遅くしないため)


class DiskWatcher:
    def __init__(self, ctx: Any, state_path: Path, read_drive: Callable[[], DiskInfo],
                 notify: Callable[[str, DiskInfo], None], log: logging.Logger,
                 now: Callable[[], float] = time.time) -> None:
        self.ctx = ctx
        self.path = state_path
        self._read = read_drive
        self._notify = notify
        self.log = log
        self._now = now
        self._timer: Any = None
        self._first: Any = None
        self.last_status: str | None = None

    # ---- 状態ファイル
    def _load(self) -> dict[str, Any]:
        try:
            v = json.loads(self.path.read_text(encoding="utf-8"))
            return v if isinstance(v, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, st: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(st), encoding="utf-8")
        except OSError as e:
            self.log.warning("watch state save failed: %s", type(e).__name__)

    # ---- タイマー
    def first_delay_s(self) -> float:
        last = self._load().get("last_check")
        if not isinstance(last, (int, float)):
            return FIRST_MIN_DELAY_S
        return float(min(INTERVAL_S, max(FIRST_MIN_DELAY_S, last + INTERVAL_S - self._now())))

    def start(self) -> None:
        delay = self.first_delay_s()
        self._first = self.ctx.start_timer(int(delay * 1000), self._on_first, single_shot=True)

    def _on_first(self) -> None:
        self.tick()
        self._timer = self.ctx.start_timer(INTERVAL_S * 1000, self.tick)

    def stop(self) -> None:
        for t in (self._first, self._timer):
            if t is not None:
                try:
                    t.stop()
                except RuntimeError:
                    pass
        self._first = self._timer = None

    # ---- 1回分
    def tick(self) -> str | None:
        """読んで、必要なら通知する。通知した status(しなければ None)。スリープ明けも1回だけ読む(QTimer は溜めない)。"""
        if self.ctx.is_snoozed():
            return None
        d = self._read()
        status = disk_status(d)
        self.last_status = status
        st = self._load()
        now = self._now()
        st["last_check"] = now
        raw = st.get("notified")
        notified: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
        sent: str | None = None
        if status in ("warn", "bad"):
            prev = notified.get(status)
            if not isinstance(prev, (int, float)) or now - prev >= RENOTIFY_S:
                notified[status] = now
                sent = status
        st["notified"] = notified
        self._save(st)
        self.log.info("watch disk status=%s notified=%s", status, bool(sent))
        if sent is not None:
            self._notify(sent, d)
        return sent
