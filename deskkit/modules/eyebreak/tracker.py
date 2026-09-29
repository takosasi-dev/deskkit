# 続けて使っている時間と「休んだ」の判定(E-1・E-2)。時刻(time.monotonic の秒)と入力なしの秒数を受け取るだけで、
# Qt も Win32 も使わない。入力なしが break_idle 秒に達したら休んだとし、数えを 0 に戻す。それより短い手の止まりは使っている時間に含める。
from __future__ import annotations

DT_CAP_S = 60.0  # 1回の読み取りで進める最大の秒数(タイマーが遅れた・スリープから戻ったときに、止まっていた時間を数えない)


class Tracker:
    def __init__(self) -> None:
        self.eye_s = 0.0
        self.body_s = 0.0
        self.use_s = 0.0      # 最後に休んでから使っている時間(「ゲームのあと」の文言に使う)
        self.in_break = False
        self.last: float | None = None

    def reset(self) -> None:
        self.eye_s = self.body_s = self.use_s = 0.0

    def skip(self, now: float) -> None:
        """数えずに時刻だけ進める(読めなかった回・一時停止中)。"""
        self.last = now

    def update(self, now: float, idle_s: float, break_idle_s: float, *, counting: bool = True) -> bool:
        """1回の読み取り。休んだ(入力なしが break_idle_s に達した)瞬間だけ True を返す。"""
        dt = 0.0 if self.last is None else min(max(0.0, now - self.last), DT_CAP_S)
        self.last = now
        if idle_s >= break_idle_s:
            if self.in_break:
                return False
            self.in_break = True
            self.reset()
            return True
        self.in_break = False
        if counting:
            self.eye_s += dt
            self.body_s += dt
            self.use_s += dt
        return False
