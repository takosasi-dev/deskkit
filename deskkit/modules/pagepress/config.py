# PagePress の設定(仕様書 §9)。既定値と、合わない値を既定に戻す正規化だけを持つ純粋な関数。
# 最後に使ったフォルダ・ファイル名は設定に残さない(§9)。
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

TABS = ("merge", "split", "organize", "compress")
IMAGE_FITS = ("paper", "original")
PAPERS = ("a4", "letter")
MARGINS = (0, 10)
SPLIT_MODES = ("ranges", "single", "every")
LEVELS = ("light", "normal", "strong")
SPLIT_EVERY_MIN, SPLIT_EVERY_MAX = 1, 999

DEFAULTS: dict[str, Any] = {
    "last_tab": "merge",
    "image_fit": "paper",
    "paper": "a4",
    "margin_mm": 10,
    "split_mode": "ranges",
    "split_every": 10,
    "compress_level": "normal",
    "sendto_enabled": False,  # Q-2 の回答(2026-09-28): 画面でオンにしたときだけ「送る」に登録する
}


@dataclass(frozen=True)
class Config:
    last_tab: str
    image_fit: str
    paper: str
    margin_mm: int
    split_mode: str
    split_every: int
    compress_level: str
    sendto_enabled: bool


def _int_ok(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def normalize(section: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """(正規化した設定, 書き戻しが要るか)。知らないキーは落とし、足りない・合わない値は既定に戻す。"""
    out: dict[str, Any] = {}
    changed = False
    choices: dict[str, tuple[Any, ...]] = {
        "last_tab": TABS, "image_fit": IMAGE_FITS, "paper": PAPERS, "split_mode": SPLIT_MODES, "compress_level": LEVELS,
    }
    for key, default in DEFAULTS.items():
        v = section.get(key, default)
        ok: bool
        if key in choices:
            ok = isinstance(v, str) and v in choices[key]
        elif key == "margin_mm":
            ok = _int_ok(v) and v in MARGINS
        elif key == "sendto_enabled":
            ok = isinstance(v, bool)
        else:  # split_every
            ok = _int_ok(v) and SPLIT_EVERY_MIN <= v <= SPLIT_EVERY_MAX
        if key not in section or not ok:
            changed = True
        out[key] = v if ok else copy.deepcopy(default)
    if set(section) - set(DEFAULTS) - {"enabled"}:
        changed = True
    return out, changed


def parse(section: dict[str, Any]) -> Config:
    s, _ = normalize(section)
    return Config(**s)
