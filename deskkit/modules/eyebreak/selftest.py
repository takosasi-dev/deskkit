# EyeBreak の自己検査。偽の時計・偽の入力時刻・偽の前面で、20 分で目・60 分で体・休んだら 0・ゲーム中は保留して抜けたら1回・
# 様子見は出さない・32 ビットの桁あふれ・文言の禁止語、を確かめる。本物の入力・通知・%LOCALAPPDATA%\DeskKit には触れない。
# run() は 0=合格 / 1=不合格。
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from deskkit.modules.eyebreak import _win32
from deskkit.modules.eyebreak.config import Config
from deskkit.modules.eyebreak.prompts import ALL_TEXTS, FORBIDDEN_WORDS, Action, Engine

STEP = 5.0


class _Sim:
    def __init__(self, cfg: Config) -> None:
        self.e = Engine(cfg, 0.0)
        self.now = 0.0
        self.base = datetime(2026, 9, 28, 9, 0, 0)
        self.idle = 0.0
        self.game = False
        self.fs: bool | None = False
        self.acts: list[Action] = []

    def run(self, seconds: float, *, active: bool = True) -> None:
        n = int(round(seconds / STEP))
        for _ in range(n):
            self.now += STEP
            self.idle = 0.0 if active else self.idle + STEP
            self.acts += self.e.tick(self.now, self.base + timedelta(seconds=self.now), self.idle,
                                     lambda: (self.game, self.fs), False)

    def shown(self) -> list[Action]:
        return [a for a in self.acts if a.type == "show"]


class _Result:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, name: str, ok: bool) -> None:
        print(f"  [{'OK' if ok else 'NG'}] {name}")
        if not ok:
            self.failed += 1


LIVE = Config(mode="live")


def run() -> int:
    print("EyeBreak 自己検査")
    r = _Result()
    print("入力なしの秒数(AC-13)")
    r.check("0xFFFFFF00 → 0x100 は 512 ミリ秒", _win32.idle_ms(0xFFFFFF00, 0x100) == 512)
    r.check("dwTime が今より新しいと 0", _win32.idle_ms(0x200, 0x100) == 0)
    fa = _win32.FakeApi()
    fa.fail = True
    r.check("GetLastInputInfo の失敗は None", _win32.read_idle_seconds(fa) is None)

    print("間隔と休んだの判定(AC-1〜AC-3)")
    s = _Sim(LIVE)
    s.run(20 * 60)
    sh = s.shown()
    r.check("20 分で目の声かけが1回、本文に「20分」", len(sh) == 1 and sh[0].kind == "eye" and sh[0].prompt is not None
            and "20分" in sh[0].prompt.text)
    s.run(40 * 60)
    sh = s.shown()
    r.check("60 分では体だけ", len(sh) == 3 and sh[-1].kind == "body" and sh[-2].kind == "eye")
    s.run(20 * 60)
    r.check("その 20 分後に目", s.shown()[-1].kind == "eye" and len(s.shown()) == 4)
    s = _Sim(LIVE)
    s.run(15 * 60)
    s.run(5 * 60, active=False)
    r.check("15 分 + 入力なし 5 分では出ない", s.shown() == [])
    s.run(20 * 60)
    r.check("そこから 20 分で出る", len(s.shown()) == 1)

    print("ゲーム中は保留して、抜けたら1回(AC-4)")
    s = _Sim(LIVE)
    s.game = True
    s.run(120 * 60)
    r.check("ゲーム中は出ない", s.shown() == [])
    s.game = False
    s.run(25)
    r.check("抜けて 25 秒では出ない", s.shown() == [])
    s.run(5)
    sh = s.shown()
    r.check("30 秒で「ゲームのあと(体)」が1回", len(sh) == 1 and sh[0].kind == "body" and sh[0].prompt is not None
            and sh[0].prompt.after_game and "2時間" in sh[0].prompt.text)

    print("様子見は出さない(AC-12)")
    s = _Sim(replace(LIVE, mode="dry_run"))
    s.run(60 * 60)
    r.check("show が無く dry がある", s.shown() == [] and any(a.type == "dry" for a in s.acts))

    print("文言の禁止語(AC-16)")
    bad = [w for w in FORBIDDEN_WORDS for t in ALL_TEXTS if w in t]
    r.check("禁止語が無い", bad == [])
    print("合格" if r.failed == 0 else f"不合格({r.failed} 件)")
    return 0 if r.failed == 0 else 1
