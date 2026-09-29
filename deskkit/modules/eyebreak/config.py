# EyeBreak の設定(§9)。足りないキーは既定値で補い(書き戻してよい)、型や範囲の合わない値はセクションの誤りとして
# 例外にする(共通基盤 D-9: host がこのモジュールだけを「停止中(理由)」にする)。
from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DEFAULTS: dict[str, Any] = {
    "mode": "dry_run",
    "eye": {"enabled": True, "interval_min": 20},
    "body": {"enabled": True, "interval_min": 60},
    "break_idle_min": 5,
    "later_min": 10,
    "game": "after",
    "after_game_delay_s": 30,
    "quiet_modes": [],
    "day_start_hour": 4,
}
RANGES: dict[str, tuple[int, int]] = {
    "eye.interval_min": (5, 120),
    "body.interval_min": (20, 240),
    "break_idle_min": (1, 30),
    "later_min": (1, 60),
    "after_game_delay_s": (0, 600),
    "day_start_hour": (0, 23),
}
MODES = ("dry_run", "live")
GAMES = ("after", "pause")


class EyeBreakSettingsError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    mode: str = "dry_run"
    eye_enabled: bool = True
    eye_min: int = 20
    body_enabled: bool = True
    body_min: int = 60
    break_idle_min: int = 5
    later_min: int = 10
    game: str = "after"
    after_game_delay_s: int = 30
    quiet_modes: tuple[str, ...] = ()
    day_start_hour: int = 4

    @property
    def break_idle_s(self) -> float:
        return float(self.break_idle_min * 60)

    def interval_min(self, kind: str) -> int:
        return self.body_min if kind == "body" else self.eye_min


def _check_int(name: str, v: object) -> int:
    lo, hi = RANGES[name]
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise EyeBreakSettingsError(f"{name} は {lo}〜{hi} の整数にしてください")
    return v


def parse(section: Mapping[str, Any]) -> tuple[dict[str, Any], Config, bool]:
    """(補った設定, Config, 補ったか)。誤りは EyeBreakSettingsError。"""
    sec = {k: copy.deepcopy(v) for k, v in section.items() if k != "enabled"}
    filled = False
    for k, dv in DEFAULTS.items():
        if k not in sec:
            sec[k] = copy.deepcopy(dv)
            filled = True
        elif isinstance(dv, dict):
            if not isinstance(sec[k], dict):
                raise EyeBreakSettingsError(f"{k} は {{\"enabled\": ..., \"interval_min\": ...}} の形にしてください")
            for kk, dvv in dv.items():
                if kk not in sec[k]:
                    sec[k][kk] = dvv
                    filled = True
    if sec["mode"] not in MODES:
        raise EyeBreakSettingsError('mode は "dry_run" か "live" にしてください')
    if sec["game"] not in GAMES:
        raise EyeBreakSettingsError('game は "after" か "pause" にしてください')
    for part in ("eye", "body"):
        if not isinstance(sec[part]["enabled"], bool):
            raise EyeBreakSettingsError(f"{part}.enabled は true / false にしてください")
    qm = sec["quiet_modes"]
    if not isinstance(qm, list) or not all(isinstance(x, str) for x in qm):
        raise EyeBreakSettingsError("quiet_modes はモード名の配列にしてください")
    cfg = Config(
        mode=sec["mode"],
        eye_enabled=sec["eye"]["enabled"],
        eye_min=_check_int("eye.interval_min", sec["eye"]["interval_min"]),
        body_enabled=sec["body"]["enabled"],
        body_min=_check_int("body.interval_min", sec["body"]["interval_min"]),
        break_idle_min=_check_int("break_idle_min", sec["break_idle_min"]),
        later_min=_check_int("later_min", sec["later_min"]),
        game=sec["game"],
        after_game_delay_s=_check_int("after_game_delay_s", sec["after_game_delay_s"]),
        quiet_modes=tuple(qm),
        day_start_hour=_check_int("day_start_hour", sec["day_start_hour"]),
    )
    return sec, cfg, filled
