# LayoutKeep の設定セクション(§9.1)の既定値・検証・正規化。
# 足りないキーは既定値で補う。モード等の致命的な不正は ValueError、ルール(targets)単位の不正はその行だけ無効にする。
from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .model import DEFAULT_FIELDS, ID_SOURCES, as_dict, as_list

MODES = ("dry_run", "live")

DEFAULTS: dict[str, Any] = {
    "mode": "dry_run",
    "auto_apply": False,
    "debounce_ms": 2500,
    "settle_checks": 2,
    "max_wait_s": 20,
    "signature": {"id_source": "device_interface", "fields": list(DEFAULT_FIELDS)},
    "targets": [],
    "exclude_classes": [],
    "hotkeys": {"save": "", "apply": ""},
    "names": {},
}

ID_SOURCE_LABELS = {
    "device_interface": "デバイスインタフェース",
    "displayconfig_path": "DisplayConfig パス",
    "edid": "EDID(製造者・製品)",
}
ID_SOURCE_HELP = {
    "device_interface": "EnumDisplayDevices が返すモニタのデバイスパス。同じポートに挿し直す分には変わりにくい。",
    "displayconfig_path": "QueryDisplayConfig が返すモニタのデバイスパス。多くの環境で上と同じ値になる。",
    "edid": "モニタ本体の製造者・製品コード。ポートを変えても同じだが、同じ型のモニタ2台は区別できない。",
}


@dataclass(frozen=True)
class Target:
    exe: str
    cls: str | None
    title_regex: str | None
    pattern: re.Pattern[str] | None = field(default=None, compare=False)

    def to_json(self) -> dict[str, Any]:
        return {"exe": self.exe, "class": self.cls, "title_regex": self.title_regex}

    def matches(self, exe_path: str | None, cls: str, title: str) -> bool:
        if not exe_path:
            return False
        want = self.exe.strip().lower()
        have = exe_path.lower()
        if "\\" in want or "/" in want:
            if want.replace("/", "\\") != have:
                return False
        elif have.rsplit("\\", 1)[-1] != want:
            return False
        if self.cls and self.cls != cls:
            return False
        return not (self.pattern is not None and not self.pattern.search(title))


@dataclass
class Config:
    mode: str
    auto_apply: bool
    debounce_ms: int
    settle_checks: int
    max_wait_s: int
    id_source: str
    fields: tuple[str, ...]
    targets: list[Target]
    exclude_classes: frozenset[str]
    hotkeys: dict[str, str]
    names: dict[str, str]
    invalid_targets: list[str] = field(default_factory=list)  # 無効にした targets の説明(タイトルは含めない)

    @property
    def live(self) -> bool:
        return self.mode == "live"


def _int(v: Any, default: int, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    return max(lo, min(hi, int(v)))


def fill_defaults(section: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """足りないキーを既定値で補ったセクションと、補ったかどうか。"""
    sec = copy.deepcopy(dict(section))
    changed = False
    for k, v in DEFAULTS.items():
        if k not in sec:
            sec[k] = copy.deepcopy(v)
            changed = True
    if isinstance(sec.get("signature"), dict):
        for k, v in DEFAULTS["signature"].items():
            if k not in sec["signature"]:
                sec["signature"][k] = copy.deepcopy(v)
                changed = True
    if isinstance(sec.get("hotkeys"), dict):
        for k, v in DEFAULTS["hotkeys"].items():
            if k not in sec["hotkeys"]:
                sec["hotkeys"][k] = v
                changed = True
    return sec, changed


def compile_target(d: Any) -> Target:
    if not isinstance(d, dict):
        raise ValueError("オブジェクトではありません")
    exe = d.get("exe")
    if not isinstance(exe, str) or not exe.strip():
        raise ValueError("exe が空です")
    cls = d.get("class")
    if cls is not None and not isinstance(cls, str):
        raise ValueError("class は文字列にしてください")
    tr = d.get("title_regex")
    if tr is not None and not isinstance(tr, str):
        raise ValueError("title_regex は文字列にしてください")
    pat = None
    if tr:
        try:
            pat = re.compile(tr)
        except re.error as e:
            raise ValueError(f"title_regex を解釈できません({e.msg})") from e
    return Target(exe=exe.strip(), cls=cls or None, title_regex=tr or None, pattern=pat)


def parse(section: Mapping[str, Any]) -> Config:
    """セクションを検証して Config にする。モード・id_source の不正は ValueError(モジュールごと停止)。"""
    sec, _ = fill_defaults(section)
    mode = sec.get("mode")
    if mode not in MODES:
        raise ValueError(f"mode は {' / '.join(MODES)} のどれかにしてください(現在: {mode!r})")
    sig = as_dict(sec.get("signature"))
    id_source = sig.get("id_source")
    if id_source not in ID_SOURCES:
        raise ValueError(f"signature.id_source は {' / '.join(ID_SOURCES)} のどれかにしてください(現在: {id_source!r})")
    fields_raw = sig.get("fields")
    if not isinstance(fields_raw, list) or not fields_raw or not all(f in DEFAULT_FIELDS for f in fields_raw) or "id" not in fields_raw:
        raise ValueError(f"signature.fields は {list(DEFAULT_FIELDS)} の部分集合(id を含む)にしてください")
    targets: list[Target] = []
    invalid: list[str] = []
    raw_targets = as_list(sec.get("targets"))
    for i, t in enumerate(raw_targets):
        try:
            targets.append(compile_target(t))
        except ValueError as e:
            name = t.get("exe") if isinstance(t, dict) else None
            invalid.append(f"targets[{i}]({name or '?'}): {e}")
    ex = sec.get("exclude_classes")
    exclude = frozenset(x for x in ex if isinstance(x, str) and x) if isinstance(ex, list) else frozenset()
    hk = as_dict(sec.get("hotkeys"))
    names_raw = as_dict(sec.get("names"))
    return Config(
        mode=str(mode),
        auto_apply=bool(sec.get("auto_apply") is True),
        debounce_ms=_int(sec.get("debounce_ms"), 2500, 100, 120_000),
        settle_checks=_int(sec.get("settle_checks"), 2, 1, 20),
        max_wait_s=_int(sec.get("max_wait_s"), 20, 0, 600),
        id_source=str(id_source),
        fields=tuple(f for f in DEFAULT_FIELDS if f in fields_raw),
        targets=targets,
        exclude_classes=exclude,
        hotkeys={"save": str(hk.get("save") or ""), "apply": str(hk.get("apply") or "")},
        names={str(k): str(v) for k, v in names_raw.items() if isinstance(v, str) and v.strip()},
        invalid_targets=invalid,
    )
