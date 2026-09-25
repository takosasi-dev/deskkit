# L3: 新しく開いたトップレベルウィンドウを EnumWindows の差分(ポーリング)で見つける。イベントフック・ウィンドウフックは使わない。
# 対象になり得ない窓(targets の exe・クラスに当たらない、オーナー付き等)は1回見たら以後は見ない。
# 見えて位置が落ち着いた窓だけを「判定待ち」として返す。1つの窓は1回だけ扱う(扱った hwnd を覚え、消えたら忘れる)。
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ._win32 import Win32Api
from .model import RawWindow, Rect, exe_basename

SETTLE_POLLS = 2  # 同じ矩形が続けて見えた回数がこれに達したら判定する(アプリ自身の位置合わせを待つ)
MAX_PENDING_S = 15.0  # 出てきてからこの秒数のうちに見える・落ち着くにならなければ諦める


@dataclass
class _Pending:
    first: float
    rect: Rect | None = None
    same: int = 0


@dataclass
class PlaceEvent:
    """新規ウィンドウ1枚の判定結果(画面の一覧と診断用。タイトルは持たない)。"""

    ts: str
    exe_name: str
    cls: str
    hwnd: int
    action: str  # "would_move" | "moved" | "skip"
    key: str  # 理由コード(move / ambiguous / not_matched / elevated / snoozed / game / ...)
    rule: str = ""
    preset: str = ""
    before: Rect | None = None
    after: Rect | None = None

    def log_dict(self) -> dict[str, Any]:
        return {"exe": self.exe_name, "class": self.cls, "hwnd": self.hwnd, "action": self.action, "key": self.key,
                "rule": self.rule, "before": list(self.before) if self.before else None,
                "after": list(self.after) if self.after else None}


class NewWindowWatcher:
    """新しいトップレベルウィンドウの検出。poll_new() で差分を取り、ready() が判定してよい窓の RawWindow を返す。"""

    def __init__(self, api: Win32Api, could_be_target: Callable[[RawWindow], bool],
                 now: Callable[[], float] = time.monotonic) -> None:
        self.api = api
        self._could_be_target = could_be_target
        self._now = now
        self._seen: set[int] = set()
        self._pending: dict[int, _Pending] = {}
        self._handled: set[int] = set()
        self._primed = False
        self.polls = 0

    def reset(self) -> None:
        """次の poll で今あるウィンドウを「既にあったもの」として覚え直す(有効にした直後・構成変化の後)。"""
        self._primed = False
        self._pending.clear()

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def handled_count(self) -> int:
        return len(self._handled)

    def poll_new(self) -> bool:
        """EnumWindows だけを呼び、新しい窓・判定待ちの窓があるかを返す(ここでは窓の中身を読まない)。"""
        self.polls += 1
        cur = set(self.api.enum_windows())
        if not self._primed:
            self._seen = cur
            self._primed = True
            self._handled &= cur
            return False
        for h in cur - self._seen:
            if h not in self._handled:
                self._pending[h] = _Pending(first=self._now())
        self._seen = cur
        # 消えた窓は忘れる(hwnd の再利用に備える)
        self._handled &= cur
        for h in [h for h in self._pending if h not in cur]:
            del self._pending[h]
        return bool(self._pending)

    def absorb(self) -> int:
        """今の判定待ちを全部「扱い済み」にする(スヌーズ中・ゲーム中に出てきた窓を後から動かさない)。件数を返す。"""
        n = len(self._pending)
        self._handled |= set(self._pending)
        self._pending.clear()
        return n

    def ready(self) -> list[RawWindow]:
        """判定待ちの窓を読み、見えていて位置が落ち着いたものを返す(返した窓は mark_handled で扱い済みにする)。"""
        out: list[RawWindow] = []
        now = self._now()
        for h, st in list(self._pending.items()):
            raw = self.api.describe_window(h)
            if raw is None or raw.owned or raw.toolwindow or not raw.exe or not self._could_be_target(raw):
                self._drop(h)  # 対象になり得ない窓は以後見ない
                continue
            if not raw.visible or raw.iconic or raw.cloaked or raw.placement is None:
                if now - st.first > MAX_PENDING_S:
                    self._drop(h)
                continue
            if raw.screen_rect == st.rect:
                st.same += 1
            else:
                st.rect, st.same = raw.screen_rect, 1
            if st.same >= SETTLE_POLLS:
                out.append(raw)
            elif now - st.first > MAX_PENDING_S:
                self._drop(h)
        return out

    def mark_handled(self, hwnd: int) -> None:
        self._drop(hwnd)

    def _drop(self, hwnd: int) -> None:
        self._pending.pop(hwnd, None)
        self._handled.add(hwnd)


def event_for(raw: RawWindow, ts: str, action: str, key: str, **kw: Any) -> PlaceEvent:
    return PlaceEvent(ts=ts, exe_name=exe_basename(raw.exe), cls=raw.cls, hwnd=raw.hwnd, action=action, key=key, **kw)
