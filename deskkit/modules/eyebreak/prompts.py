# 声かけを「出す/保留する/様子見で数える/出さない」の判定と文言(E-3〜E-11・FR-2〜FR-15)。時計と前面の状態を引数で受け取る
# 純粋な処理で、Qt・Win32 を使わない(テストは偽の時計で何時間分でも一瞬で進める)。module.py が結果(Action)を通知・記録に移す。
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from deskkit.modules.eyebreak.config import Config
from deskkit.modules.eyebreak.tracker import Tracker

EYE = "eye"
BODY = "body"
EYE_REST_WINDOW_S = 180.0   # E-5: 目の声かけのあと 3 分以内に
EYE_REST_NEED_S = 20.0      # 20 秒以上止まれば「休めた」
BODY_REST_WINDOW_S = 900.0  # 体の声かけのあと 15 分以内に break_idle_min 以上
FAILS_UNAVAILABLE = 3       # FR-1: 3 回続けて読めなければ「読めません」

TITLES = {EYE: "目を休めませんか", BODY: "ひと休みしませんか"}
AFTER_GAME_TITLE = "おつかれさまでした"
BODIES = {
    (EYE, False): "{t}つづけて画面を見ています。20秒ほど、窓の外など遠くを見てみましょう。",
    (BODY, False): "{t}つづけて使っています。少し立って、飲み物を取りに行くなどしてみましょう。",
    (EYE, True): "ゲームを含めて{t}つづけて画面を見ています。20秒ほど、遠くを見てみましょう。",
    (BODY, True): "ゲームを含めて{t}つづけて使っています。少し立って、ひと息つきませんか。",
}
DISCLAIMER = "目や体の調子が気になるときは、この道具ではなく専門家に相談してください。"
BASIS = ("目は 20 分ごとに遠くを見る目安(米国眼科学会)、体は 1 時間を超えて続けない目安"
         "(厚生労働省の情報機器作業のガイドライン)から決めました。")
# INV-6: 文言に使わない語(テストと自己検査で確かめる)
FORBIDDEN_WORDS = ("治", "効く", "効果", "予防", "症状", "病", "疲れ目", "視力", "健康")
ALL_TEXTS: tuple[str, ...] = (*TITLES.values(), AFTER_GAME_TITLE, *BODIES.values(), DISCLAIMER, BASIS)


def fmt_minutes(minutes: int) -> str:
    """「20分」「1時間」「1時間30分」の形。"""
    m = max(0, int(minutes))
    h, mm = divmod(m, 60)
    if h and mm:
        return f"{h}時間{mm}分"
    if h:
        return f"{h}時間"
    return f"{mm}分"


@dataclass(frozen=True)
class Prompt:
    kind: str
    after_game: bool = False
    minutes: int = 0

    @property
    def title(self) -> str:
        return AFTER_GAME_TITLE if self.after_game else TITLES[self.kind]

    @property
    def text(self) -> str:
        return BODIES[(self.kind, self.after_game)].format(t=fmt_minutes(self.minutes))


@dataclass(frozen=True)
class Action:
    type: str                 # show | dry | held | muted | rested | break | idle_unavailable | unmuted
    kind: str = ""
    prompt: Prompt | None = None


@dataclass(frozen=True)
class Held:
    kind: str
    game: bool                # ゲーム・全画面で保留した(「ゲームのあと」の文言を使う)


@dataclass(frozen=True)
class Later:
    kind: str
    due: float


@dataclass(frozen=True)
class RestWatch:
    kind: str
    shown_at: float
    deadline: float
    need: float


def next_day_start(wall: datetime, hour: int) -> datetime:
    """次の day_start_hour 時(E-10)。今がちょうどその時刻なら翌日。"""
    cand = wall.replace(hour=hour, minute=0, second=0, microsecond=0)
    if cand <= wall:
        cand += timedelta(days=1)
    return cand


FgReader = Callable[[], tuple[bool, bool | None]]  # (is_game, is_fullscreen)


class Engine:
    def __init__(self, cfg: Config, start: float | None = None) -> None:
        self.cfg = cfg
        self.tracker = Tracker()
        self.tracker.last = start  # 動き始めた時刻から数える
        self.held: Held | None = None
        self.safe_since: float | None = None
        self.later_: Later | None = None
        self.rest_watch: RestWatch | None = None
        self.muted_until: datetime | None = None
        self.quiet = False
        self.snoozed = False
        self.fails = 0
        self.unavailable = False
        self.last_kind = EYE
        self._prev_tick: float | None = start

    # ------------------------------------------------------------ 状態の変更(操作・イベント)
    def restart_count(self) -> None:
        """0 から数え直す。保留と「あとで」も取り消す。"""
        self.tracker.reset()
        self.held = None
        self.safe_since = None
        self.later_ = None

    def force_break(self) -> None:
        """FR-14: スリープ・休止から戻った。休んだとして扱う(FR-2 と同じ)。"""
        self.restart_count()

    def later(self, kind: str | None, minutes: int, now: float) -> None:
        self.later_ = Later(kind or self.last_kind, now + minutes * 60.0)

    def mute_today(self, wall: datetime) -> datetime:
        self.muted_until = next_day_start(wall, self.cfg.day_start_hour)
        self.later_ = None
        self.held = None
        self.safe_since = None
        return self.muted_until

    def resume(self) -> None:
        """FR-8: 今日は止めている・あとで を取り消し、0 から数え直す(スヌーズは解かない)。"""
        self.muted_until = None
        self.restart_count()

    def is_muted(self, wall: datetime) -> bool:
        return self.muted_until is not None and wall < self.muted_until

    def set_quiet(self, quiet: bool) -> None:
        self.quiet = quiet
        if quiet:
            self.safe_since = None

    def set_snoozed(self, snoozed: bool) -> None:
        if self.snoozed and not snoozed:
            self.restart_count()  # FR-11: 解除されたら 0 から数え直す
        self.snoozed = snoozed

    # ------------------------------------------------------------ 5 秒ごと
    def tick(self, now: float, wall: datetime, idle_s: float | None, fg: FgReader, snoozed: bool) -> list[Action]:
        acts: list[Action] = []
        self._prev_tick = self.tracker.last  # 直前の読み取りの時刻(保留を出すまでの待ちの起点に使う)
        if idle_s is None:  # FR-1: 読めなかった回は数えない
            self.tracker.skip(now)
            self.fails += 1
            if self.fails == FAILS_UNAVAILABLE:
                self.unavailable = True
                acts.append(Action("idle_unavailable"))
            return acts
        self.fails = 0
        self.unavailable = False
        self.set_snoozed(snoozed)
        if snoozed:  # E-8: 数えず、作らず、保留も出さない
            self.tracker.skip(now)
            return acts
        if self.muted_until is not None and wall >= self.muted_until:
            self.muted_until = None
            acts.append(Action("unmuted"))
        self._watch_rest(now, idle_s, acts)
        cfg = self.cfg
        unsafe_cache: list[bool] = []

        def unsafe() -> bool:
            if not unsafe_cache:
                g, fs = fg()
                unsafe_cache.append(bool(g) or fs is True)  # E-7: None は全画面でない側
            return unsafe_cache[0]

        counting = not (cfg.game == "pause" and unsafe())
        if self.tracker.update(now, idle_s, cfg.break_idle_s, counting=counting):
            self.held = None  # FR-2: 保留と「あとで」を取り消す
            self.safe_since = None
            self.later_ = None
            acts.append(Action("break"))
        if self.tracker.in_break:
            return acts
        due: str | None = None
        t = self.tracker
        if cfg.body_enabled and t.body_s >= cfg.body_min * 60:
            due = BODY
            t.body_s = 0.0
            t.eye_s = 0.0  # E-4: 体を出すときは目も 0 に
        elif cfg.eye_enabled and t.eye_s >= cfg.eye_min * 60:
            due = EYE
            t.eye_s = 0.0
        if self.later_ is not None and now >= self.later_.due:
            lk = self.later_.kind
            self.later_ = None
            due = BODY if BODY in (due, lk) else EYE
        if due is not None:
            acts.extend(self._route(due, now, wall, unsafe))
        elif self.held is not None:
            acts.extend(self._release(now, wall, unsafe))
        return acts

    def _watch_rest(self, now: float, idle_s: float, acts: list[Action]) -> None:
        rw = self.rest_watch
        if rw is None:
            return
        eff = min(idle_s, now - rw.shown_at)  # 声かけより前の手の止まりは数えない
        if eff >= rw.need and now - eff <= rw.deadline:
            self.rest_watch = None
            acts.append(Action("rested", rw.kind))
        elif now > rw.deadline + rw.need:
            self.rest_watch = None

    def _route(self, kind: str, now: float, wall: datetime, unsafe: Callable[[], bool]) -> list[Action]:
        if self.is_muted(wall):  # FR-7: 作らない(数えは進む)
            return [Action("muted", kind)]
        if unsafe() or self.quiet or self.held is not None:
            game = unsafe()
            prev = self.held
            merged_kind = BODY if BODY in (kind, prev.kind if prev else "") else EYE
            self.held = Held(merged_kind, bool(game or (prev.game if prev else False)))
            if unsafe() or self.quiet:
                self.safe_since = None
                return [Action("held", kind)]
            # すでに保留があり、今は安全: まとめたうえで、待ち時間が済んでいれば出す
            return [Action("held", kind), *self._release(now, wall, unsafe)]
        return self._emit(Prompt(kind, False, self.cfg.interval_min(kind)), now)

    def _release(self, now: float, wall: datetime, unsafe: Callable[[], bool]) -> list[Action]:
        """FR-9・FR-12: ゲームでも全画面でもなく、静かにするモードでもない状態が after_game_delay_s 続いたら出す。"""
        h = self.held
        if h is None:
            return []
        if unsafe() or self.quiet:
            self.safe_since = None
            return []
        if self.safe_since is None:
            # 5 秒ごとにしか読まないので、ゲームを抜けた(安全になった)のは直前の読み取りの直後とみなす
            prev = self._prev_tick
            self.safe_since = prev if prev is not None and prev <= now else now
        if now - self.safe_since < self.cfg.after_game_delay_s:
            return []
        self.held = None
        self.safe_since = None
        if self.is_muted(wall):
            return [Action("muted", h.kind)]
        after_game = h.game and self.cfg.game == "after"
        minutes = int(self.tracker.use_s // 60) if after_game else self.cfg.interval_min(h.kind)
        return self._emit(Prompt(h.kind, after_game, minutes), now)

    def _emit(self, p: Prompt, now: float) -> list[Action]:
        self.last_kind = p.kind
        if self.cfg.mode != "live":
            return [Action("dry", p.kind, p)]  # FR-15・INV-2: 様子見では出さない
        if p.kind == EYE:
            self.rest_watch = RestWatch(EYE, now, now + EYE_REST_WINDOW_S, EYE_REST_NEED_S)
        else:
            self.rest_watch = RestWatch(BODY, now, now + BODY_REST_WINDOW_S, self.cfg.break_idle_s)
        return [Action("show", p.kind, p)]

    # ------------------------------------------------------------ 表示
    def status(self, wall: datetime) -> str:
        """FR-17 のトレイの状態。"""
        if self.unavailable:
            return "この PC では使っている時間を読めません"
        if self.snoozed:
            return "一時停止中"
        if self.is_muted(wall):
            return "今日は止めています"
        if self.held is not None and self.held.game and not self.quiet:
            return "ゲームのあとで出します"
        if self.quiet:
            return "静かにするモードの間は待っています"
        if self.cfg.mode != "live":
            return "様子見中"
        return self.count_text()

    def count_text(self) -> str:
        t = self.tracker
        parts = []
        if self.cfg.eye_enabled:
            parts.append(f"目 {int(t.eye_s // 60)}分")
        if self.cfg.body_enabled:
            parts.append(f"体 {int(t.body_s // 60)}分")
        return "・".join(parts) if parts else "声かけはすべてオフです"

    def with_config(self, cfg: Config) -> None:
        """設定を変えた: 今の数えは保ち、新しい間隔で判定する(§10)。"""
        self.cfg = replace(cfg)
