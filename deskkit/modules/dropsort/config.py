# DropSort の設定(settings.json の modules.dropsort)の既定値・補完・型検査と、ルール定義の読み込み。
# 全体の設定が不正なら ConfigError(モジュールごと停止中)、ルール単位の不正はそのルールだけ無効化する。
# GUI の「テンプレートから追加」で使うルールの雛形もここに置く(移動先は利用者が選ぶので空)。
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

MODES = ("dry-run", "apply")
WATCH_MODES = ("rdcw", "poll")

DEFAULTS: dict[str, Any] = {
    "paused": False,
    "downloads_dir_override": None,
    "watch_mode": "rdcw",
    "temp_extensions": [".crdownload", ".part", ".tmp", ".partial"],
    "stable_seconds": 5,
    "full_scan_interval_s": 30,
    "exec_extensions": [".exe", ".scr", ".com", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".msi", ".hta", ".lnk"],
    "max_suffix": 99,
    "locked_retry_max": 10,
    "undo_default_count": 1,
    "archive": {"enabled": False, "mode": "dry-run", "idle_days": 30, "dir_name": "_archive", "use_atime": False},
    "hotkeys": {"undo_last": ""},
    "rules": [],
}

EMPTY_MATCH: dict[str, Any] = {"ext": None, "name_regex": None, "size_min": None, "size_max": None,
                               "motw": None, "zone_ids": None, "host_domain": None}

# (表示名, ルール名, 拡張子)
TEMPLATES: list[tuple[str, str, list[str]]] = [
    ("PDF", "PDF", [".pdf"]),
    ("画像", "画像", [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".tif", ".tiff", ".svg"]),
    ("動画", "動画", [".mp4", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".m4v"]),
    ("音声", "音声", [".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"]),
    ("圧縮ファイル", "圧縮", [".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".tgz"]),
    ("インストーラ", "インストーラ", [".exe", ".msi", ".msix", ".msixbundle", ".appx"]),
    ("Office 文書", "Office文書", [".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".csv", ".odt", ".ods", ".odp"]),
    ("ディスクイメージ", "ディスクイメージ", [".iso", ".img", ".vhd", ".vhdx"]),
]

ZONE_NAMES = {0: "ローカル", 1: "イントラネット", 2: "信頼済み", 3: "インターネット", 4: "制限付き"}


class ConfigError(Exception):
    pass


def norm_ext(s: str) -> str:
    s = s.strip().lower()
    if not s:
        return ""
    return s if s.startswith(".") else "." + s


def new_rule(name: str, exts: list[str] | None = None, dest: str = "") -> dict[str, Any]:
    m = dict(EMPTY_MATCH)
    m["ext"] = list(exts) if exts else None
    return {"name": name, "mode": "dry-run", "match": m, "dest": dest}


def fill_defaults(section: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """足りないキーを既定値で補う。戻り値は (補った設定, 変更があったか)。"""
    out = copy.deepcopy(section)
    changed = False
    for k, v in DEFAULTS.items():
        if k not in out:
            out[k] = copy.deepcopy(v)
            changed = True
        elif isinstance(v, dict) and isinstance(out[k], dict):
            for kk, vv in v.items():
                if kk not in out[k]:
                    out[k][kk] = copy.deepcopy(vv)
                    changed = True
    rules = out.get("rules")
    if isinstance(rules, list):
        for r in rules:
            if isinstance(r, dict):
                if "mode" not in r:
                    r["mode"] = "dry-run"  # FR-8: 未指定は dry-run
                    changed = True
                if not isinstance(r.get("match"), dict):
                    r["match"] = dict(EMPTY_MATCH)
                    changed = True
    return out, changed


@dataclass(frozen=True)
class Match:
    ext: frozenset[str] | None = None
    name_regex: str | None = None
    size_min: int | None = None
    size_max: int | None = None
    motw: bool | None = None
    zone_ids: frozenset[int] | None = None
    host_domain: tuple[str, ...] | None = None


@dataclass(frozen=True)
class RuleDef:
    index: int
    name: str
    mode: str
    match: Match
    dest: str
    error: str | None = None  # 形の不正(読み込み時に確定する無効理由)
    pattern: re.Pattern[str] | None = field(default=None, compare=False, repr=False)

    @property
    def apply(self) -> bool:
        return self.mode == "apply"


@dataclass(frozen=True)
class ArchiveCfg:
    enabled: bool
    mode: str
    idle_days: int
    dir_name: str
    use_atime: bool


@dataclass(frozen=True)
class Config:
    paused: bool
    downloads_dir_override: str | None
    watch_mode: str
    temp_extensions: frozenset[str]
    stable_seconds: float
    full_scan_interval_s: float
    exec_extensions: frozenset[str]
    max_suffix: int
    locked_retry_max: int
    undo_default_count: int
    archive: ArchiveCfg
    hotkey_undo_last: str
    rules: tuple[RuleDef, ...]


def _num(sec: dict[str, Any], key: str, lo: float, hi: float, *, integer: bool = True) -> Any:
    v = sec.get(key)
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise ConfigError(f"{key} は数値にしてください")
    if integer and not float(v).is_integer():
        raise ConfigError(f"{key} は整数にしてください")
    if not (lo <= v <= hi):
        raise ConfigError(f"{key} は {lo:g}〜{hi:g} の範囲にしてください")
    return int(v) if integer else float(v)


def _str_list(sec: dict[str, Any], key: str) -> frozenset[str]:
    v = sec.get(key)
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ConfigError(f"{key} は文字列の配列にしてください")
    return frozenset(e for e in (norm_ext(x) for x in v) if e)


def _opt_int(v: Any, what: str) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise ValueError(f"{what} は 0 以上の整数(バイト)か null にしてください")
    return int(v)


def parse_rule(i: int, raw: Any) -> RuleDef:
    if not isinstance(raw, dict):
        return RuleDef(i, f"#{i + 1}", "dry-run", Match(), "", error="ルールがオブジェクトではありません")
    name = str(raw.get("name") or f"#{i + 1}")
    mode = raw.get("mode", "dry-run")
    dest = raw.get("dest")
    try:
        if mode not in MODES:
            raise ValueError(f"mode は {' / '.join(MODES)} のどちらかにしてください")
        if not isinstance(dest, str) or not dest.strip():
            raise ValueError("移動先(dest)が空です")
        m = raw.get("match") or {}
        if not isinstance(m, dict):
            raise ValueError("match がオブジェクトではありません")
        ext = m.get("ext")
        if ext is not None:
            if isinstance(ext, str):
                ext = [ext]
            if not isinstance(ext, list) or not all(isinstance(x, str) for x in ext):
                raise ValueError("ext は文字列の配列にしてください")
            ext_set: frozenset[str] | None = frozenset(e for e in (norm_ext(x) for x in ext) if e) or None
        else:
            ext_set = None
        rx = m.get("name_regex")
        pat: re.Pattern[str] | None = None
        if rx is not None:
            if not isinstance(rx, str):
                raise ValueError("name_regex は文字列にしてください")
            try:
                pat = re.compile(rx)
            except re.error as e:
                raise ValueError(f"name_regex をコンパイルできません: {e}") from e
        smin = _opt_int(m.get("size_min"), "size_min")
        smax = _opt_int(m.get("size_max"), "size_max")
        if smin is not None and smax is not None and smin > smax:
            raise ValueError("size_min が size_max より大きくなっています")
        motw = m.get("motw")
        if motw is not None and not isinstance(motw, bool):
            raise ValueError("motw は true / false / null にしてください")
        zids = m.get("zone_ids")
        zset: frozenset[int] | None = None
        if zids is not None:
            if not isinstance(zids, list) or not all(isinstance(z, int) and not isinstance(z, bool) for z in zids):
                raise ValueError("zone_ids は整数の配列にしてください")
            zset = frozenset(zids)
        hd = m.get("host_domain")
        hdt: tuple[str, ...] | None = None
        if hd is not None:
            if isinstance(hd, str):
                hd = [hd]
            if not isinstance(hd, list) or not all(isinstance(x, str) for x in hd):
                raise ValueError("host_domain は文字列(の配列)にしてください")
            hdt = tuple(x.strip().lower().lstrip(".") for x in hd if x.strip()) or None
        match = Match(ext_set, rx, smin, smax, motw, zset, hdt)
        return RuleDef(i, name, str(mode), match, dest.strip(), None, pat)
    except ValueError as e:
        return RuleDef(i, name, mode if mode in MODES else "dry-run", Match(), str(dest or ""), error=str(e))


def load_config(section: dict[str, Any]) -> Config:
    """補完済みのセクションを型付きの Config にする。全体が不正なら ConfigError。"""
    sec, _ = fill_defaults(section)
    if not isinstance(sec.get("paused"), bool):
        raise ConfigError("paused は true / false にしてください")
    ov = sec.get("downloads_dir_override")
    if ov is not None and not isinstance(ov, str):
        raise ConfigError("downloads_dir_override は文字列か null にしてください")
    wm = sec.get("watch_mode")
    if wm not in WATCH_MODES:
        raise ConfigError("watch_mode は rdcw / poll のどちらかにしてください")
    arch = sec.get("archive")
    if not isinstance(arch, dict):
        raise ConfigError("archive はオブジェクトにしてください")
    if not isinstance(arch.get("enabled"), bool) or not isinstance(arch.get("use_atime"), bool):
        raise ConfigError("archive.enabled / archive.use_atime は true / false にしてください")
    if arch.get("mode") not in MODES:
        raise ConfigError("archive.mode は dry-run / apply のどちらかにしてください")
    dn = arch.get("dir_name")
    if not isinstance(dn, str) or not dn.strip() or any(c in dn for c in '\\/:*?"<>|'):
        raise ConfigError("archive.dir_name はフォルダ名1つにしてください")
    hk = sec.get("hotkeys")
    if not isinstance(hk, dict):
        raise ConfigError("hotkeys はオブジェクトにしてください")
    rules_raw = sec.get("rules")
    if not isinstance(rules_raw, list):
        raise ConfigError("rules は配列にしてください")
    return Config(
        paused=bool(sec["paused"]),
        downloads_dir_override=ov.strip() if isinstance(ov, str) and ov.strip() else None,
        watch_mode=str(wm),
        temp_extensions=_str_list(sec, "temp_extensions"),
        stable_seconds=_num(sec, "stable_seconds", 0, 3600, integer=False),
        full_scan_interval_s=_num(sec, "full_scan_interval_s", 2, 86400, integer=False),
        exec_extensions=_str_list(sec, "exec_extensions"),
        max_suffix=_num(sec, "max_suffix", 1, 9999),
        locked_retry_max=_num(sec, "locked_retry_max", 1, 10000),
        undo_default_count=_num(sec, "undo_default_count", 1, 1000),
        archive=ArchiveCfg(bool(arch["enabled"]), str(arch["mode"]), _num(arch, "idle_days", 1, 36500),
                           dn.strip(), bool(arch["use_atime"])),
        hotkey_undo_last=str(hk.get("undo_last") or ""),
        rules=tuple(parse_rule(i, r) for i, r in enumerate(rules_raw)),
    )
