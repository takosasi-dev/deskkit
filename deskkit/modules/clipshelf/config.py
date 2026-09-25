# ClipShelf の設定セクションの既定値(MODULE_GUIDE §5)と検証。足りないキーは既定値で補う。
# 不正な値は ValueError(host がモジュールを「停止中(理由)」にする)。
from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MODES = ("observe", "record")
UNKNOWN_OWNER_POLICIES = ("skip", "record")
AUTO_PASTE_MODES = ("off", "dry_run", "on")
HOTKEY_NAMES = ("open_palette", "plain_text", "toggle_pause")

DEFAULTS: dict[str, Any] = {
    "mode": "observe",
    "hotkeys": {"open_palette": "Ctrl+Alt+V", "plain_text": "", "toggle_pause": ""},
    "debounce_ms": 150,
    "open_retry": 5,
    "max_chars": 100000,
    "retention": {"max_items": 1000, "max_days": 30},
    "exclude_exes": [],
    "unknown_owner_policy": "skip",
    "date_format": "%Y-%m-%d",
    "time_format": "%H:%M",
    "auto_paste": "off",
    "auto_paste_deny_exes": [],
}


@dataclass(frozen=True)
class Config:
    mode: str
    hotkeys: dict[str, str]
    debounce_ms: int
    open_retry: int
    max_chars: int
    max_items: int
    max_days: int
    exclude_exes: frozenset[str]
    unknown_owner_policy: str
    date_format: str
    time_format: str
    auto_paste: str
    auto_paste_deny_exes: frozenset[str]


def merge_defaults(section: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """既定値で足りないキーを補った設定と、補ったかどうかを返す。"""
    out = copy.deepcopy(dict(section))
    changed = False
    for key, default in DEFAULTS.items():
        if key not in out:
            out[key] = copy.deepcopy(default)
            changed = True
        elif isinstance(default, dict) and isinstance(out[key], dict):
            for sub, sub_default in default.items():
                if sub not in out[key]:
                    out[key][sub] = copy.deepcopy(sub_default)
                    changed = True
    return out, changed


def _int(sec: Mapping[str, Any], key: str, value: Any, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"clipshelf.{key} は整数にしてください")
    if not lo <= value <= hi:
        raise ValueError(f"clipshelf.{key} は {lo}〜{hi} の範囲にしてください")
    return value


def _choice(key: str, value: Any, choices: tuple[str, ...]) -> str:
    if value not in choices:
        raise ValueError(f"clipshelf.{key} は {' / '.join(choices)} のいずれかにしてください")
    return str(value)


def _exe_list(key: str, value: Any) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError(f"clipshelf.{key} は exe 名の配列にしてください")
    return frozenset(normalize_exe(x) for x in value if normalize_exe(x))


def normalize_exe(name: str) -> str:
    """exe 名の比較用表記(小文字・前後空白なし・パスはファイル名だけ)。"""
    s = name.strip().strip('"').replace("/", "\\")
    s = s.rsplit("\\", 1)[-1]
    return s.lower()


def parse(section: Mapping[str, Any]) -> Config:
    sec, _ = merge_defaults(section)
    hk = sec["hotkeys"]
    if not isinstance(hk, dict):
        raise ValueError("clipshelf.hotkeys はオブジェクトにしてください")
    hotkeys: dict[str, str] = {}
    for name in HOTKEY_NAMES:
        v = hk.get(name) or ""
        if not isinstance(v, str):
            raise ValueError(f"clipshelf.hotkeys.{name} は文字列にしてください")
        hotkeys[name] = v
    ret = sec["retention"]
    if not isinstance(ret, dict):
        raise ValueError("clipshelf.retention はオブジェクトにしてください")
    for key in ("date_format", "time_format"):
        if not isinstance(sec[key], str) or not sec[key]:
            raise ValueError(f"clipshelf.{key} は空でない文字列にしてください")
    return Config(
        mode=_choice("mode", sec["mode"], MODES),
        hotkeys=hotkeys,
        debounce_ms=_int(sec, "debounce_ms", sec["debounce_ms"], 0, 5000),
        open_retry=_int(sec, "open_retry", sec["open_retry"], 1, 50),
        max_chars=_int(sec, "max_chars", sec["max_chars"], 1, 10_000_000),
        max_items=_int(sec, "retention.max_items", ret.get("max_items"), 0, 1_000_000),
        max_days=_int(sec, "retention.max_days", ret.get("max_days"), 0, 36500),
        exclude_exes=_exe_list("exclude_exes", sec["exclude_exes"]),
        unknown_owner_policy=_choice("unknown_owner_policy", sec["unknown_owner_policy"], UNKNOWN_OWNER_POLICIES),
        date_format=str(sec["date_format"]),
        time_format=str(sec["time_format"]),
        auto_paste=_choice("auto_paste", sec["auto_paste"], AUTO_PASTE_MODES),
        auto_paste_deny_exes=_exe_list("auto_paste_deny_exes", sec["auto_paste_deny_exes"]),
    )
