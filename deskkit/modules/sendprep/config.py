# SendPrep の設定(modules.sendprep)の既定値・検証・プリセットの追加/名前変更/上限変更/削除。
# 既定の4つのプリセットは消せず、名前と上限も変えられない。壊れた値は既定値へ戻す(起動を止めない)。
from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MB = 1_000_000  # FR-7: 1MB = 1,000,000 バイト
LIMIT_MIN_MB = 1
LIMIT_MAX_MB = 500
MAX_CUSTOM_PRESETS = 5
LABEL_MAX_CHARS = 30
MAX_WORDS = 20
WORD_MAX_CHARS = 40
REDACT_STYLES = ("fill", "mosaic")
CUSTOM_ID = re.compile(r"^custom-(\d{1,6})$")

BUILTIN_PRESETS: tuple[dict[str, Any], ...] = (
    {"id": "meta", "label": "位置情報を消すだけ", "limit_mb": None},
    {"id": "discord", "label": "Discord(10MB)", "limit_mb": 10},
    {"id": "mail", "label": "メール(7MB)", "limit_mb": 7},
    {"id": "small", "label": "小さめ(3MB)", "limit_mb": 3},
)
BUILTIN_IDS = frozenset(p["id"] for p in BUILTIN_PRESETS)

DEFAULTS: dict[str, Any] = {
    "presets": [dict(p) for p in BUILTIN_PRESETS],
    "last_preset": "meta",
    "rename_to_date": False,
    "redact_style": "fill",
    "my_words": [],
    "sendto_enabled": False,
}


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    limit_mb: int | None

    @property
    def builtin(self) -> bool:
        return self.id in BUILTIN_IDS

    @property
    def limit_bytes(self) -> int | None:
        return None if self.limit_mb is None else self.limit_mb * MB

    def describe(self) -> str:
        return "上限なし" if self.limit_mb is None else f"{self.limit_mb}MB まで"


@dataclass(frozen=True)
class Config:
    presets: tuple[Preset, ...]
    last_preset: str
    rename_to_date: bool
    redact_style: str
    my_words: tuple[str, ...]
    sendto_enabled: bool

    def preset(self, pid: str) -> Preset | None:
        return next((p for p in self.presets if p.id == pid), None)

    def custom_presets(self) -> list[Preset]:
        return [p for p in self.presets if not p.builtin]


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def clean_label(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    s = " ".join(raw.split())
    return s if 1 <= len(s) <= LABEL_MAX_CHARS else None


def clean_limit(raw: Any) -> int | None:
    return raw if _is_int(raw) and LIMIT_MIN_MB <= raw <= LIMIT_MAX_MB else None


def clean_word(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    return s if 1 <= len(s) <= WORD_MAX_CHARS else None


def normalize(section: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """設定を検証し、足りない・壊れた値を既定値で埋めた辞書と「変えたか」を返す(enabled は扱わない)。"""
    src = {k: v for k, v in section.items() if k != "enabled"}
    out: dict[str, Any] = {}
    # プリセット: 既定の4つは常に先頭に固定の値で置き、追加分は検証して最大5個
    presets: list[dict[str, Any]] = [dict(p) for p in BUILTIN_PRESETS]
    seen: set[str] = set(BUILTIN_IDS)
    raw_presets = src.get("presets")
    for p in raw_presets if isinstance(raw_presets, list) else []:
        if not isinstance(p, dict):
            continue
        pid = p.get("id")
        if not isinstance(pid, str) or not CUSTOM_ID.match(pid) or pid in seen:
            continue
        label, limit = clean_label(p.get("label")), clean_limit(p.get("limit_mb"))
        if label is None or limit is None or len(presets) >= len(BUILTIN_PRESETS) + MAX_CUSTOM_PRESETS:
            continue
        seen.add(pid)
        presets.append({"id": pid, "label": label, "limit_mb": limit})
    out["presets"] = presets
    last = src.get("last_preset")
    out["last_preset"] = last if isinstance(last, str) and last in seen else "meta"
    out["rename_to_date"] = src.get("rename_to_date") if isinstance(src.get("rename_to_date"), bool) else False
    style = src.get("redact_style")
    out["redact_style"] = style if style in REDACT_STYLES else "fill"
    words: list[str] = []
    raw_words = src.get("my_words")
    for w in raw_words if isinstance(raw_words, list) else []:
        cw = clean_word(w)
        if cw is not None and cw not in words and len(words) < MAX_WORDS:
            words.append(cw)
    out["my_words"] = words
    out["sendto_enabled"] = src.get("sendto_enabled") if isinstance(src.get("sendto_enabled"), bool) else False
    # 知らないキーは残す(新しい版の設定を古い版で消さないため)
    for k, v in src.items():
        if k not in out:
            out[k] = copy.deepcopy(v)
    return out, out != src


def parse(section: Mapping[str, Any]) -> Config:
    n, _ = normalize(section)
    return Config(
        presets=tuple(Preset(p["id"], p["label"], p["limit_mb"]) for p in n["presets"]),
        last_preset=n["last_preset"],
        rename_to_date=n["rename_to_date"],
        redact_style=n["redact_style"],
        my_words=tuple(n["my_words"]),
        sendto_enabled=n["sendto_enabled"],
    )


# ------------------------------------------------------------------ プリセットの編集(FR-5)。どれも section を直接書き換える
def add_custom(section: dict[str, Any], label: str, limit_mb: int) -> str:
    """追加したプリセットの id を返す。範囲外なら ValueError(画面に出す文言)。"""
    presets = section.setdefault("presets", [])
    customs = [p for p in presets if isinstance(p, dict) and p.get("id") not in BUILTIN_IDS]
    if len(customs) >= MAX_CUSTOM_PRESETS:
        raise ValueError(f"追加できるプリセットは {MAX_CUSTOM_PRESETS} 個までです")
    lb = clean_label(label)
    if lb is None:
        raise ValueError(f"名前は 1〜{LABEL_MAX_CHARS} 文字で入れてください")
    lim = clean_limit(limit_mb)
    if lim is None:
        raise ValueError(f"上限は {LIMIT_MIN_MB}〜{LIMIT_MAX_MB}MB の整数で入れてください")
    nums = [int(m.group(1)) for p in customs if isinstance(p.get("id"), str) and (m := CUSTOM_ID.match(p["id"]))]
    pid = f"custom-{max(nums, default=0) + 1}"
    presets.append({"id": pid, "label": lb, "limit_mb": lim})
    return pid


def _custom(section: dict[str, Any], pid: str) -> dict[str, Any]:
    if pid in BUILTIN_IDS:
        raise ValueError("最初からあるプリセットは変えられません")
    for p in section.get("presets", []):
        if isinstance(p, dict) and p.get("id") == pid:
            return p
    raise ValueError("プリセットが見つかりません")


def rename_custom(section: dict[str, Any], pid: str, label: str) -> None:
    p = _custom(section, pid)
    lb = clean_label(label)
    if lb is None:
        raise ValueError(f"名前は 1〜{LABEL_MAX_CHARS} 文字で入れてください")
    p["label"] = lb


def set_custom_limit(section: dict[str, Any], pid: str, limit_mb: int) -> None:
    p = _custom(section, pid)
    lim = clean_limit(limit_mb)
    if lim is None:
        raise ValueError(f"上限は {LIMIT_MIN_MB}〜{LIMIT_MAX_MB}MB の整数で入れてください")
    p["limit_mb"] = lim


def delete_custom(section: dict[str, Any], pid: str) -> None:
    _custom(section, pid)
    section["presets"] = [p for p in section.get("presets", []) if not (isinstance(p, dict) and p.get("id") == pid)]
    if section.get("last_preset") == pid:
        section["last_preset"] = "meta"
