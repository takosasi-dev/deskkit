# 設定セクション "modeshift" の既定値補完・検証(FR-1)・モード定義ハッシュ(confirmed_hash 用)。
# 不正なモードはそのモードだけ無効にし理由を持たせる。他のモードは使える。
# Qt に依存しない(GUI の入力フォームも normalize_action を使ってその場で検証する)。
from __future__ import annotations

import copy
import hashlib
import json
import ntpath
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from deskkit.hotkeys import parse_hotkey
from deskkit.modules.modeshift.model import ACTION_TYPES, TYPE_LABELS

GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")
PREVIEW_POLICIES = ("unconfirmed_only", "always")
ON_EXIT = ("undo", "none")
# open_path で開かない拡張子(既定のハンドラ経由で実行・昇格要求になり得るもの。INV-7)
EXEC_EXTENSIONS = frozenset({".exe", ".com", ".bat", ".cmd", ".scr", ".msi", ".ps1", ".vbs", ".vbe", ".js",
                             ".jse", ".wsf", ".wsh", ".hta", ".lnk", ".pif", ".cpl", ".msc", ".reg"})
POLL_MIN_S, POLL_MAX_S = 0.5, 3600.0
TIMEOUT_MIN_S, TIMEOUT_MAX_S = 1.0, 300.0


def default_section() -> dict[str, Any]:
    return {
        "preview": "unconfirmed_only",
        "allow_force_kill": False,
        "undo_hotkey": None,
        "modes": [],
        "auto_switch": {"enabled": False, "poll_interval_s": None, "rules": []},
    }


def fill_defaults(section: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """足りないキーを既定値で補う。戻り値は (補った section, 変更があったか)。"""
    out = copy.deepcopy(dict(section))
    changed = False
    for k, v in default_section().items():
        if k not in out:
            out[k] = v
            changed = True
    if isinstance(out.get("auto_switch"), dict):
        for k, v in default_section()["auto_switch"].items():
            if k not in out["auto_switch"]:
                out["auto_switch"][k] = v
                changed = True
    return out, changed


# ------------------------------------------------------------------ 小道具
def _is_num(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def exe_name_ok(exe: str) -> bool:
    return bool(exe) and exe.lower().endswith(".exe") and "\\" not in exe and "/" not in exe and ":" not in exe


# http / https の URL(スキーム・ホスト・パス・クエリ・フラグメント)。空白を含むものは受け付けない
_URL_RE = re.compile(r"^(?P<scheme>https?)://(?P<host>[^/?#\s]+)(?P<path>[^?#\s]*)(?P<rest>[?#]\S*)?$", re.IGNORECASE)


def url_ok(url: str) -> bool:
    return bool(_URL_RE.match(url))


def safe_url(url: str) -> str:
    """画面・ログ用: クエリとフラグメントを落とした URL(INV-11)。"""
    m = _URL_RE.match(url)
    if not m:
        return "(解釈できない URL)"
    s = f"{m.group('scheme').lower()}://{m.group('host')}{m.group('path')}"
    if m.group("rest"):
        s += "?…"
    return s


@dataclass
class FsCheck:
    """ファイルの存在確認(テストで差し替える)。"""

    is_file: Callable[[str], bool] = os.path.isfile
    is_dir: Callable[[str], bool] = os.path.isdir
    exists: Callable[[str], bool] = os.path.exists


def normalize_action(a: Any, game_processes: Iterable[str] = (), fs: FsCheck | None = None) -> tuple[dict[str, Any], list[str]]:
    """1アクションを既定値つきの正規形にし、問題点(日本語)を返す。問題が1つでもあればそのモードは無効。"""
    fs = fs or FsCheck()
    games = {g.lower() for g in game_processes}
    errs: list[str] = []
    if not isinstance(a, Mapping):
        return {"type": None}, ["アクションがオブジェクトではありません"]
    t = a.get("type")
    if t not in ACTION_TYPES:
        return {"type": t}, [f"未知のアクション種別 '{t}'"]
    label = TYPE_LABELS[t]
    out: dict[str, Any] = {"type": t}
    if t == "launch_app":
        path = a.get("path")
        args = a.get("args", [])
        cwd = a.get("cwd")
        sir = a.get("skip_if_running", True)
        if not isinstance(path, str) or not path.strip():
            errs.append(f"{label}: path がありません")
            path = ""
        else:
            path = path.strip()
            if not ntpath.isabs(path) or not path.lower().endswith(".exe"):
                errs.append(f"{label}: path は exe の絶対パスにしてください({path})")
            elif not fs.is_file(path):
                errs.append(f"{label}: exe が見つかりません({path})")
        if args is None:
            args = []
        if not isinstance(args, list) or not all(isinstance(x, str) for x in args):
            errs.append(f"{label}: args は文字列の配列にしてください")
            args = []
        if cwd is not None and (not isinstance(cwd, str) or not cwd.strip()):
            cwd = None
        if isinstance(cwd, str) and not fs.is_dir(cwd):
            errs.append(f"{label}: 作業フォルダが見つかりません({cwd})")
        if not isinstance(sir, bool):
            errs.append(f"{label}: skip_if_running は true / false にしてください")
            sir = True
        out.update(path=path, args=list(args), cwd=cwd, skip_if_running=sir)
    elif t in ("close_app", "app_volume"):
        exe = a.get("exe")
        if not isinstance(exe, str) or not exe_name_ok(exe.strip()):
            errs.append(f"{label}: exe はファイル名(例: app.exe)で指定してください")
            exe = exe if isinstance(exe, str) else ""
        exe = exe.strip().lower()
        out["exe"] = exe
        if t == "close_app":
            if exe in games:
                errs.append(f"{label}: ゲーム(game_processes)の {exe} は閉じる対象にできません")
            to = a.get("timeout_s", 10)
            if not _is_num(to) or not (TIMEOUT_MIN_S <= float(to) <= TIMEOUT_MAX_S):
                errs.append(f"{label}: timeout_s は {TIMEOUT_MIN_S:g}〜{TIMEOUT_MAX_S:g} 秒にしてください")
                to = 10
            fot = a.get("force_on_timeout", False)
            if not isinstance(fot, bool):
                errs.append(f"{label}: force_on_timeout は true / false にしてください")
                fot = False
            out.update(timeout_s=float(to), force_on_timeout=fot)
        else:
            av: Any = a.get("level")
            if not _is_num(av) or not (0.0 <= float(av) <= 1.0):
                errs.append(f"{label}: level は 0.0〜1.0 にしてください")
                av = 0.0
            out["level"] = float(av)
    elif t == "power_plan":
        g = a.get("guid")
        if not isinstance(g, str) or not GUID_RE.match(g.strip()):
            errs.append(f"{label}: guid が GUID の形式ではありません")
            g = g if isinstance(g, str) else ""
        out["guid"] = g.strip().lower()
    elif t == "master_volume":
        lv = a.get("level")
        mute = a.get("mute")
        if lv is not None and (not _is_num(lv) or not (0.0 <= float(lv) <= 1.0)):
            errs.append(f"{label}: level は 0.0〜1.0 にしてください")
            lv = None
        if mute is not None and not isinstance(mute, bool):
            errs.append(f"{label}: mute は true / false / null にしてください")
            mute = None
        if lv is None and mute is None and not errs:
            errs.append(f"{label}: level か mute のどちらかを指定してください")
        out.update(level=None if lv is None else float(lv), mute=mute)
    elif t == "open_path":
        p = a.get("path")
        if not isinstance(p, str) or not p.strip():
            errs.append(f"{label}: path がありません")
            p = ""
        else:
            p = p.strip()
            if not ntpath.isabs(p):
                errs.append(f"{label}: path は絶対パスにしてください")
            elif os.path.splitext(p)[1].lower() in EXEC_EXTENSIONS:
                errs.append(f"{label}: 実行ファイル・ショートカットは開けません(起動は「アプリ起動」を使ってください)")
        out["path"] = p
    elif t == "open_url":
        u = a.get("url")
        if not isinstance(u, str) or not url_ok(u.strip()):
            errs.append(f"{label}: http / https の URL だけ指定できます")
            u = u if isinstance(u, str) else ""
        out["url"] = u.strip()
    elif t == "layout_apply":
        lay = a.get("layout")
        if lay is not None and (not isinstance(lay, str) or not lay.strip()):
            lay = None
        ws = a.get("wait_s", 0)
        if not _is_num(ws) or not (0 <= float(ws) <= 600):
            errs.append(f"{label}: wait_s は 0〜600 秒にしてください")
            ws = 0
        out.update(layout=lay.strip() if isinstance(lay, str) else None, wait_s=float(ws))
    return out, errs


def definition_hash(name: str, actions: list[dict[str, Any]]) -> str:
    """§9.2: actions と name を正規化した JSON の SHA-256。"""
    doc = json.dumps({"name": name, "actions": actions}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(doc.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 検証結果
@dataclass
class ModeDef:
    index: int
    name: str
    label: str
    hotkey: str | None            # 登録に使う表記(無効化されたら None)
    hotkey_raw: str | None
    confirmed_hash: str | None
    actions: list[dict[str, Any]]  # 正規形
    def_hash: str
    accent: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors

    @property
    def confirmed(self) -> bool:
        return bool(self.confirmed_hash) and self.confirmed_hash == self.def_hash

    def reason(self) -> str:
        return " / ".join(self.errors)


@dataclass
class AutoRule:
    exe: str
    mode: str
    on_exit: str
    error: str | None = None


@dataclass
class Config:
    preview: str
    allow_force_kill: bool
    undo_hotkey: str | None
    modes: list[ModeDef]
    auto_enabled: bool
    poll_interval_s: float | None
    rules: list[AutoRule]
    auto_error: str | None = None
    issues: list[str] = field(default_factory=list)

    def mode(self, name: str) -> ModeDef | None:
        for m in self.modes:
            if m.name == name:
                return m
        return None

    def valid_modes(self) -> list[ModeDef]:
        return [m for m in self.modes if m.valid]

    def invalid_modes(self) -> list[ModeDef]:
        return [m for m in self.modes if not m.valid]

    @property
    def auto_active(self) -> bool:
        return self.auto_enabled and self.auto_error is None and self.poll_interval_s is not None


def _hk_key(text: str) -> tuple[int, int] | None:
    try:
        return parse_hotkey(text)
    except ValueError:
        return None


def validate_section(section: Mapping[str, Any], game_processes: Iterable[str] = (), fs: FsCheck | None = None) -> Config:
    games = frozenset(g.lower() for g in game_processes)
    issues: list[str] = []
    sec = dict(section)
    preview = sec.get("preview", "unconfirmed_only")
    if preview not in PREVIEW_POLICIES:
        issues.append(f"preview '{preview}' は解釈できないため unconfirmed_only として扱います")
        preview = "unconfirmed_only"
    afk = sec.get("allow_force_kill", False)
    if not isinstance(afk, bool):
        issues.append("allow_force_kill は true / false にしてください(false として扱います)")
        afk = False
    used_keys: dict[tuple[int, int], str] = {}
    undo_hk = sec.get("undo_hotkey")
    if undo_hk is not None and (not isinstance(undo_hk, str) or not undo_hk.strip()):
        undo_hk = None
    if isinstance(undo_hk, str):
        k = _hk_key(undo_hk)
        if k is None:
            issues.append(f"undo_hotkey '{undo_hk}' を解釈できないため無効にしました")
            undo_hk = None
        else:
            used_keys[k] = "元に戻す"

    modes_raw = sec.get("modes", [])
    if not isinstance(modes_raw, list):
        issues.append("modes が配列ではないため、モードは0件として扱います")
        modes_raw = []
    modes: list[ModeDef] = []
    seen: set[str] = set()
    for i, raw in enumerate(modes_raw):
        if not isinstance(raw, Mapping):
            modes.append(ModeDef(i, f"#{i + 1}", f"#{i + 1}", None, None, None, [], "", errors=["モードがオブジェクトではありません"]))
            continue
        name = raw.get("name")
        errors: list[str] = []
        warnings: list[str] = []
        if not isinstance(name, str) or not NAME_RE.match(name):
            errors.append("name は半角英数字・_・- の1〜40文字にしてください")
            name = str(name) if name is not None else f"#{i + 1}"
        elif name in seen:
            errors.append(f"name '{name}' が重複しています")
        seen.add(name)
        label = raw.get("label")
        if not isinstance(label, str) or not label.strip():
            label = name
        acts_raw = raw.get("actions", [])
        actions: list[dict[str, Any]] = []
        if not isinstance(acts_raw, list):
            errors.append("actions が配列ではありません")
            acts_raw = []
        if not acts_raw:
            errors.append("アクションが1つもありません")
        for j, a in enumerate(acts_raw):
            norm, errs = normalize_action(a, games, fs)
            actions.append(norm)
            errors.extend(f"{j + 1}番目: {e}" for e in errs)
        hk_raw = raw.get("hotkey")
        hk: str | None = hk_raw.strip() if isinstance(hk_raw, str) and hk_raw.strip() else None
        if hk is not None:
            k = _hk_key(hk)
            if k is None:
                warnings.append(f"ホットキー '{hk}' を解釈できないため無効にしました")
                hk = None
            elif k in used_keys:
                warnings.append(f"ホットキー '{hk}' が「{used_keys[k]}」と重複するため無効にしました")
                hk = None
            else:
                used_keys[k] = str(label)
        ch = raw.get("confirmed_hash")
        accent = raw.get("accent") if isinstance(raw.get("accent"), str) else None
        modes.append(ModeDef(
            index=i, name=name, label=str(label).strip(), hotkey=hk, hotkey_raw=hk_raw if isinstance(hk_raw, str) else None,
            confirmed_hash=ch if isinstance(ch, str) else None, actions=actions,
            def_hash=definition_hash(name, actions), accent=accent, errors=errors, warnings=warnings,
        ))

    auto = sec.get("auto_switch", {})
    if not isinstance(auto, Mapping):
        issues.append("auto_switch がオブジェクトではないため自動切替は無効です")
        auto = {}
    enabled = auto.get("enabled", False)
    if not isinstance(enabled, bool):
        enabled = False
    poll = auto.get("poll_interval_s")
    auto_error: str | None = None
    if poll is not None and (not _is_num(poll) or not (POLL_MIN_S <= float(poll) <= POLL_MAX_S)):
        auto_error = f"poll_interval_s は {POLL_MIN_S:g}〜{POLL_MAX_S:g} 秒にしてください"
        poll = None
    if enabled and poll is None and auto_error is None:
        auto_error = "poll_interval_s が未設定のため自動切替は無効です"
    rules: list[AutoRule] = []
    rr = auto.get("rules", [])
    if not isinstance(rr, list):
        rr = []
    names = {m.name for m in modes if m.valid}
    for r in rr:
        if not isinstance(r, Mapping):
            continue
        exe = str(r.get("exe") or "").strip().lower()
        mode = str(r.get("mode") or "")
        on_exit = r.get("on_exit") or "none"
        err = None
        if not exe_name_ok(exe):
            err = "exe はファイル名(例: game.exe)で指定してください"
        elif mode not in names:
            err = f"モード '{mode}' が無いか無効です"
        elif on_exit not in ON_EXIT:
            err = "on_exit は undo / none にしてください"
        rules.append(AutoRule(exe, mode, str(on_exit), err))
    return Config(
        preview=str(preview), allow_force_kill=afk, undo_hotkey=undo_hk, modes=modes,
        auto_enabled=enabled, poll_interval_s=float(poll) if poll is not None else None,
        rules=rules, auto_error=auto_error, issues=issues,
    )
