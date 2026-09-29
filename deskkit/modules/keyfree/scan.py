# 一覧の進め方。DeskKit が持つ組を snapshot() で先に「DeskKit」にし(K-7)、残りを ctx.hotkeys.probe に渡して結果を4つの状態に分ける。
# 始める前と一覧の間 200ms ごとに前面を見て、ゲーム・全画面なら始めない・中止する(FR-8・INV-6)。中止は ProbeHandle.cancel()(FR-6)。
# 結果はメモリにだけ持つ(K-8)。ログには件数と所要時間だけを書く(INV-4)。ホットキーの登録の API はここでも呼ばない(INV-1)。
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from deskkit.modules.keyfree import combos, keynames
from deskkit.modules.keyfree._win32 import KeyboardApi
from deskkit.modules.keyfree.combos import Combo

FREE, USED, DESKKIT, ERROR, RESERVED, PENDING = "free", "used", "deskkit", "error", "reserved", "pending"
FG_INTERVAL_MS = 200
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

# start() / check_one() の戻り値
STARTED, RUNNING, UNSUPPORTED, BLOCKED, FAILED, DONE = "started", "running", "unsupported", "blocked", "failed", "done"


@dataclass
class Cell:
    state: str
    error: int = 0


@dataclass
class ScanResult:
    """1回の一覧の結果(メモリだけ。K-8)。reason が None の間は調べている途中。"""

    plan: combos.Plan
    chars: dict[int, str]
    include_win: bool
    held_names: dict[Combo, str]
    cells: dict[Combo, Cell]
    started_wall: datetime
    started_mono: float
    to_probe: int
    checked: int = 0
    pressed: list[Combo] = field(default_factory=list)
    reason: str | None = None        # finished / cancelled / error / release_failed
    stop_why: str | None = None      # 中止のわけ: user / foreground / stop / timeout
    error_code: int = 0
    ms: int = 0
    finished_wall: datetime | None = None
    layout_changed: bool = False
    deskkit_changed: bool = False

    @property
    def running(self) -> bool:
        return self.reason is None

    def state(self, c: Combo) -> str | None:
        cell = self.cells.get(c)
        return cell.state if cell is not None else None

    def done_count(self) -> int:
        """進み具合の分子: 試し終えた組(probe に渡した分のうち)。"""
        return sum(1 for c, cell in self.cells.items() if cell.state in (FREE, USED, ERROR)
                   or (cell.state == DESKKIT and c not in self.held_names))

    def counts(self) -> dict[str, int]:
        n = {FREE: 0, USED: 0, DESKKIT: 0, ERROR: 0, RESERVED: 0, PENDING: 0}
        for cell in self.cells.values():
            n[cell.state] = n.get(cell.state, 0) + 1
        return {"tried": self.checked, "free": n[FREE], "used": n[USED], "deskkit": n[DESKKIT],
                "unavailable": n[ERROR] + n[RESERVED], "pressed": len(self.pressed)}


@dataclass(frozen=True)
class SingleResult:
    """1つだけ調べた結果(FR-5・FR-9・CLI check)。state は FREE / USED / DESKKIT / ERROR / RESERVED か、
    終わり方が普通でないときの reason(busy / cancelled / release_failed / error)。"""

    combo: Combo
    state: str
    error: int = 0
    holder: str | None = None        # DESKKIT のときの登録名
    pressed: tuple[Combo, ...] = ()


class Scanner:
    def __init__(self, ctx: Any, kb: KeyboardApi, *, on_change: Callable[[], None],
                 on_finished: Callable[[ScanResult], None], log: logging.Logger | None = None,
                 mono: Callable[[], float] = time.monotonic, wall: Callable[[], datetime] = datetime.now) -> None:
        self.ctx = ctx
        self.kb = kb
        self.log = log or ctx.log
        self._on_change = on_change
        self._on_finished = on_finished
        self._mono = mono
        self._wall = wall
        self.result: ScanResult | None = None        # 直近の一覧(途中も含む)
        self._prev: ScanResult | None = None
        self._gen = 0
        self._handle: Any = None
        self._fg_timer: Any = None
        self._single_handle: Any = None
        self._single_gen = 0
        self.single_running = False
        self.last_busy = False                         # 直近の一覧を host が busy で断った

    # ------------------------------------------------------------ 状態
    def supported(self) -> bool:
        """host に probe と snapshot があるか(FR-17)。"""
        hk = getattr(self.ctx, "hotkeys", None)
        return callable(getattr(hk, "probe", None)) and callable(getattr(hk, "snapshot", None))

    @property
    def running(self) -> bool:
        return self.result is not None and self.result.running

    def foreground_blocked(self) -> bool:
        """前のウィンドウが DeskKit 自身でなく、unsafe_for_input() が真なら True(FR-8)。読めなければ止める側。"""
        try:
            fg = self.ctx.foreground()
        except Exception as e:  # noqa: BLE001 - 前面を読めないときは試さない(INV-6)
            self.log.warning("foreground check failed: %s", type(e).__name__)
            return True
        if fg.pid is not None and int(fg.pid) == os.getpid():
            return False
        return bool(fg.unsafe_for_input())

    def held(self) -> dict[Combo, str]:
        snap = self.ctx.hotkeys.snapshot()
        return {(int(h.mods) & combos.MOD_MASK, int(h.vk)): str(h.name) for h in snap.held}

    # ------------------------------------------------------------ 一覧
    def start(self, include_win: bool, batch: int) -> str:
        if self.running or self.single_running:
            return RUNNING
        if not self.supported():
            return UNSUPPORTED
        if self.foreground_blocked():
            return BLOCKED
        chars = keynames.layout_chars(self.kb)
        plan = combos.build(include_win, chars)
        held = self.held()
        cells: dict[Combo, Cell] = {c: Cell(PENDING) for c in plan.combos()}
        for c in plan.reserved:
            cells[c] = Cell(RESERVED)
        to_probe: list[Combo] = []
        for c in plan.probe:
            if c in held:
                cells[c] = Cell(DESKKIT)      # K-7: DeskKit の組は試さない
            else:
                to_probe.append(c)
        res = ScanResult(plan, chars, include_win, {c: n for c, n in held.items() if c in cells}, cells,
                         self._wall(), self._mono(), len(to_probe))
        self._prev = self.result
        self.result = res
        self.last_busy = False
        self._gen += 1
        gen = self._gen
        self._handle = None
        self.log.info("scan start groups=%d probe=%d skip=%d", len(plan.groups), len(to_probe), len(cells) - len(to_probe))
        self._on_change()
        try:
            handle = self.ctx.hotkeys.probe(to_probe, lambda b: self._on_batch(gen, b), lambda d: self._on_done(gen, d),
                                            batch=int(batch))
        except Exception as e:  # noqa: BLE001 - host の例外で画面を止めない
            self.log.warning("probe call failed: %s", type(e).__name__)
            self._finish(gen, "error", 0, 0)
            return FAILED
        if self._gen == gen and self.running:
            self._handle = handle
            if res.stop_why is not None:   # probe() から戻る前に中止が頼まれていた
                handle.cancel()
            self._fg_timer = self.ctx.start_timer(FG_INTERVAL_MS, lambda: self._fg_tick(gen))
        return STARTED

    def cancel(self, why: str = "user") -> bool:
        """一覧と「1つだけ調べる」を止める(FR-6・FR-16)。止めるものがあれば True。"""
        did = False
        res = self.result
        if res is not None and res.running:
            if res.stop_why is None:
                res.stop_why = why
            if self._handle is not None:
                self._handle.cancel()
            did = True
        if self.single_running and self._single_handle is not None:
            self._single_handle.cancel()
            did = True
        return did

    def _fg_tick(self, gen: int) -> None:
        if gen != self._gen or not self.running:
            self._stop_fg_timer()
            return
        if self.foreground_blocked():
            self.log.info("scan cancel: foreground")
            self.cancel("foreground")

    def _stop_fg_timer(self) -> None:
        t, self._fg_timer = self._fg_timer, None
        if t is not None:
            try:
                t.stop()
            except RuntimeError:
                pass

    def _on_batch(self, gen: int, batch: Any) -> None:
        res = self.result
        if gen != self._gen or res is None:
            return
        for r in batch.results:
            c = (int(r.mods) & combos.MOD_MASK, int(r.vk))
            state = str(r.state)
            if state not in (FREE, USED, DESKKIT, ERROR):
                state = ERROR
            res.cells[c] = Cell(state, int(r.error) if state == ERROR else 0)
        for m, vk in batch.pressed:
            res.pressed.append((int(m) & combos.MOD_MASK, int(vk)))
        self._on_change()

    def _on_done(self, gen: int, done: Any) -> None:
        if gen != self._gen:
            return
        self._finish(gen, str(done.reason), int(done.checked), int(done.error))

    def _finish(self, gen: int, reason: str, checked: int, error: int) -> None:
        res = self.result
        self._stop_fg_timer()
        self._handle = None
        if res is None or gen != self._gen:
            return
        if reason == "busy":
            # 別の調べ物(CLI など)が動いていた: 何も試していないので、前の結果に戻す
            self.result = self._prev
            self.last_busy = True
            self.log.info("scan busy")
            self._on_change()
            return
        res.reason = reason
        res.checked = checked
        res.error_code = error
        res.ms = int((self._mono() - res.started_mono) * 1000)
        res.finished_wall = self._wall()
        if reason == "finished":
            try:
                res.layout_changed = keynames.layout_chars(self.kb) != res.chars      # §10: 配列が変わった
                after = self.held()
                before = {c for c in res.held_names}
                res.deskkit_changed = {c for c in after if c in res.cells} != before  # FR-13
            except Exception as e:  # noqa: BLE001
                self.log.warning("scan recheck failed: %s", type(e).__name__)
        n = res.counts()
        self.log.info("scan done reason=%s why=%s tried=%d free=%d used=%d deskkit=%d unavailable=%d pressed=%d ms=%d",
                      reason, res.stop_why or "-", n["tried"], n["free"], n["used"], n["deskkit"], n["unavailable"],
                      n["pressed"], res.ms)
        try:
            self._on_finished(res)
        finally:
            self._on_change()

    # ------------------------------------------------------------ 1つだけ
    def check_one(self, mods: int, vk: int, on_result: Callable[[SingleResult], None]) -> str:
        """1組だけ試す(FR-5・FR-9)。試さない組・DeskKit の組はすぐ on_result。戻り値は DONE / STARTED か断ったわけ。"""
        c = (int(mods) & combos.MOD_MASK, int(vk))
        if self.running or self.single_running:
            return RUNNING
        if not self.supported():
            return UNSUPPORTED
        if combos.is_reserved(*c):
            on_result(SingleResult(c, RESERVED))
            return DONE
        held = self.held()
        if c in held:
            self._apply_single(SingleResult(c, DESKKIT, holder=held[c]))
            on_result(SingleResult(c, DESKKIT, holder=held[c]))
            return DONE
        if self.foreground_blocked():
            return BLOCKED
        self._single_gen += 1
        gen = self._single_gen
        self.single_running = True
        box: dict[str, Any] = {"state": None, "error": 0, "pressed": []}
        self._single_handle = None

        def on_batch(b: Any) -> None:
            if gen != self._single_gen:
                return
            for r in b.results:
                if (int(r.mods) & combos.MOD_MASK, int(r.vk)) == c:
                    box["state"] = str(r.state)
                    box["error"] = int(r.error)
            box["pressed"].extend((int(m) & combos.MOD_MASK, int(v)) for m, v in b.pressed)

        def on_done(d: Any) -> None:
            if gen != self._single_gen:
                return
            self.single_running = False
            self._single_handle = None
            reason = str(d.reason)
            state = box["state"]
            if reason != "finished" or state not in (FREE, USED, DESKKIT, ERROR):
                state = reason if reason != "finished" else ERROR
            r = SingleResult(c, str(state), int(box["error"] or d.error), None, tuple(box["pressed"]))
            self.log.info("check done reason=%s", reason)
            self._apply_single(r)
            try:
                on_result(r)
            finally:
                self._on_change()

        self._on_change()
        try:
            handle = self.ctx.hotkeys.probe([c], on_batch, on_done, batch=1)
        except Exception as e:  # noqa: BLE001
            self.log.warning("probe call failed: %s", type(e).__name__)
            self.single_running = False
            self._on_change()
            return FAILED
        if self.single_running and gen == self._single_gen:
            self._single_handle = handle
        return STARTED

    def _apply_single(self, r: SingleResult) -> None:
        """調べ直した結果を、直近の一覧の表にも反映する(その組が表にあれば)。"""
        res = self.result
        if res is None or res.running or r.combo not in res.cells:
            return
        if r.state in (FREE, USED, DESKKIT, ERROR):
            res.cells[r.combo] = Cell(r.state, r.error if r.state == ERROR else 0)
            if r.state == DESKKIT and r.holder:
                res.held_names[r.combo] = r.holder

    def mark_used(self, c: Combo) -> None:
        """押して確かめるで登録できなかった組を「使用中」に直す(§10)。"""
        res = self.result
        if res is not None and not res.running and c in res.cells:
            res.cells[c] = Cell(USED)
            self._on_change()
