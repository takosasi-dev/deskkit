# 設定 `modules.cliptrim` の既定値と検証(仕様書 §9 と未確定事項への回答)。型・範囲の合わない値はその値だけ既定値に戻す。
# 撮影場所などの情報は `strip_metadata`(既定 true = 消す。回答 Q-2)で持ち、本文の `keep_metadata` は使わない。
# 「送る」への登録は `sendto_enabled`(既定 false。回答 Q-5)。開いた動画のパスは覚えない。
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

FILMSTRIP_MIN = 8
FILMSTRIP_MAX = 48

DEFAULTS: dict[str, Any] = {
    "mode": "copy",
    "output": "separate",
    "strip_metadata": True,
    "filmstrip_count": 24,
    "sendto_enabled": False,
}


@dataclass(frozen=True)
class Config:
    mode: str
    output: str
    strip_metadata: bool
    filmstrip_count: int
    sendto_enabled: bool


def normalize(section: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """(直した設定, 直したか)。知らないキーは残す(ほかの版との行き来で消さない)。"""
    out = copy.deepcopy(section)
    changed = False
    for k, v in DEFAULTS.items():
        if k not in out:
            out[k] = v
            changed = True
    if out["mode"] not in ("copy", "precise"):
        out["mode"], changed = DEFAULTS["mode"], True
    if out["output"] not in ("separate", "join"):
        out["output"], changed = DEFAULTS["output"], True
    for k in ("strip_metadata", "sendto_enabled"):
        if not isinstance(out[k], bool):
            out[k], changed = DEFAULTS[k], True
    fc = out["filmstrip_count"]
    if not isinstance(fc, int) or isinstance(fc, bool) or not FILMSTRIP_MIN <= fc <= FILMSTRIP_MAX:
        out["filmstrip_count"], changed = DEFAULTS["filmstrip_count"], True
    return out, changed


def parse(section: dict[str, Any]) -> Config:
    s, _ = normalize(section)
    return Config(s["mode"], s["output"], s["strip_metadata"], s["filmstrip_count"], s["sendto_enabled"])
