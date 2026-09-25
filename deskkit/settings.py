# %APPDATA%\DeskKit\settings.json の読み書き。書き込みは一時ファイル+os.replace(D-9)。
# 構文エラー時は前回正常に読めた設定を保持し、ファイルは書き換えない。セクション単位で検証する。
from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deskkit.catalog import MODULE_NAMES

SCHEMA_VERSION = 1


def default_settings() -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "game_processes": [],
        "host": {
            "handler_error_limit": 5,
            "log_retention_days": 14,
            "notification_style": "toast",
            "fullscreen_detection": "rect",
            "show_window_on_start": True,
            "theme": "dark",
            "quick_action_hotkey": "Ctrl+Alt+Space",
            "onboarded": False,
            "update": {"auto_check": True, "repo": "takosasi-dev/deskkit", "skip_version": None, "last_check": None},
            # v0.2
            "hold_notifications": True,       # ゲーム・全画面の間は通知を保留し、終わってからまとめて出す(H1)
            "snooze_follow_quns": False,      # Windows がプレゼン中・通知を控えている間も一時停止扱いにする(H2)
            "settings_history_keep": 20,      # 設定の自動世代保存で残す数(H3)
            "usage_period": "30",             # 利用状況ページの期間(UX-5)
            "usage_view": "chart",            # 利用状況ページの表示(UX-5)
        },
        "modules": {name: {"enabled": False} for name in MODULE_NAMES},
    }


# host セクションの型と範囲。合わない値は既定値に戻す(バックアップの復元や手編集で null・文字列が入っても起動できるように)
_HOST_BOOLS = ("show_window_on_start", "onboarded", "hold_notifications", "snooze_follow_quns")
_HOST_INTS = {"handler_error_limit": (1, 100), "log_retention_days": (1, 365), "settings_history_keep": (1, 200)}
_HOST_CHOICES = {
    "notification_style": ("toast", "balloon"),
    "fullscreen_detection": ("rect", "off", "none"),
    "theme": ("dark", "light", "system"),
    "usage_period": ("7", "30", "90"),
    "usage_view": ("chart", "table"),
}


def _sanitize_host(h: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    for k in _HOST_BOOLS:
        if not isinstance(h.get(k), bool):
            h[k] = base[k]
    for k, (lo, hi) in _HOST_INTS.items():
        v = h.get(k)
        h[k] = min(hi, max(lo, v)) if isinstance(v, int) and not isinstance(v, bool) else base[k]
    for k, choices in _HOST_CHOICES.items():
        if h.get(k) not in choices:
            h[k] = base[k]
    if not isinstance(h.get("quick_action_hotkey"), str):
        h["quick_action_hotkey"] = "" if h.get("quick_action_hotkey") is None else base["quick_action_hotkey"]
    upd_base = base["update"]
    upd = h.get("update")
    upd = dict(upd) if isinstance(upd, dict) else {}
    merged = dict(upd_base)
    merged.update(upd)
    if not isinstance(merged.get("auto_check"), bool):
        merged["auto_check"] = upd_base["auto_check"]
    if not isinstance(merged.get("repo"), str) or not merged["repo"].strip():
        merged["repo"] = upd_base["repo"]
    for k in ("skip_version", "last_check"):
        if merged.get(k) is not None and not isinstance(merged.get(k), str):
            merged[k] = None
    h["update"] = merged
    return h


class SettingsError(Exception):
    def __init__(self, message: str, line: int | None = None) -> None:
        super().__init__(message)
        self.line = line


@dataclass
class LoadResult:
    ok: bool
    created: bool = False
    error: str | None = None
    line: int | None = None
    module_errors: dict[str, str] = field(default_factory=dict)


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, Any] = default_settings()
        self.module_errors: dict[str, str] = {}
        # 書き込みのたびに呼ぶ(設定の自動世代保存 H3 用)。どのスレッドから呼ばれてもよい作りにすること
        self.on_written: Callable[[], None] | None = None

    # ---- 読み込み
    def load(self) -> LoadResult:
        created = False
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(default_settings())
            created = True
        try:
            raw = self.path.read_text(encoding="utf-8-sig")
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            return LoadResult(ok=False, error=f"JSON 構文エラー: {e.msg}", line=e.lineno)
        except OSError as e:
            return LoadResult(ok=False, error=f"読み込めません: {e.strerror}")
        try:
            data, module_errors = self._validate(data)
        except SettingsError as e:
            return LoadResult(ok=False, error=str(e))
        self._data = data
        self.module_errors = module_errors
        return LoadResult(ok=True, created=created, module_errors=dict(module_errors))

    @staticmethod
    def _validate(data: Any) -> tuple[dict[str, Any], dict[str, str]]:
        if not isinstance(data, dict):
            raise SettingsError("最上位が JSON オブジェクトではありません")
        base = default_settings()
        out: dict[str, Any] = {"version": data.get("version", SCHEMA_VERSION)}
        gp = data.get("game_processes", [])
        if not isinstance(gp, list) or not all(isinstance(x, str) for x in gp):
            raise SettingsError("game_processes は文字列の配列にしてください")
        out["game_processes"] = gp
        host = data.get("host", {})
        if not isinstance(host, dict):
            raise SettingsError("host はオブジェクトにしてください")
        merged_host = dict(base["host"])
        merged_host.update(host)
        out["host"] = _sanitize_host(merged_host, base["host"])
        mods = data.get("modules", {})
        if not isinstance(mods, dict):
            raise SettingsError("modules はオブジェクトにしてください")
        errors: dict[str, str] = {}
        out_mods: dict[str, Any] = {}
        for name, sec in mods.items():
            if not isinstance(sec, dict):
                errors[name] = "セクションがオブジェクトではありません"
                out_mods[name] = {"enabled": False}
                continue
            if not isinstance(sec.get("enabled", False), bool):
                errors[name] = "enabled は true / false にしてください"
            out_mods[name] = sec
        for name in MODULE_NAMES:
            out_mods.setdefault(name, {"enabled": False})
        out["modules"] = out_mods
        return out, errors

    # ---- 参照
    @property
    def data(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def host(self) -> dict[str, Any]:
        return copy.deepcopy(self._data["host"])

    def game_processes(self) -> frozenset[str]:
        return frozenset(x.strip().lower() for x in self._data.get("game_processes", []) if x.strip())

    def module_names(self) -> list[str]:
        return list(self._data["modules"].keys())

    def module_section(self, name: str) -> dict[str, Any]:
        return copy.deepcopy(self._data["modules"].get(name, {"enabled": False}))

    def is_enabled(self, name: str) -> bool:
        return bool(self._data["modules"].get(name, {}).get("enabled", False)) and name not in self.module_errors

    # ---- 書き込み(ファイルの現状を読み直し、該当箇所だけ差し替える)
    def _read_current_for_write(self) -> dict[str, Any]:
        if not self.path.exists():
            return default_settings()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as e:
            raise SettingsError(f"settings.json に構文エラーがあるため保存できません({e.lineno} 行目)", e.lineno) from e
        if not isinstance(data, dict):
            raise SettingsError("settings.json の最上位がオブジェクトではありません")
        return data

    def write_module(self, name: str, section: dict[str, Any]) -> None:
        data = self._read_current_for_write()
        data.setdefault("modules", {})[name] = section
        self._atomic_write(data)
        self._data["modules"][name] = copy.deepcopy(section)
        self.module_errors.pop(name, None)

    def set_enabled(self, name: str, enabled: bool) -> None:
        sec = self.module_section(name)
        sec["enabled"] = enabled
        self.write_module(name, sec)

    def write_host(self, values: dict[str, Any]) -> None:
        data = self._read_current_for_write()
        data.setdefault("host", {}).update(values)
        self._atomic_write(data)
        self._data["host"].update(values)

    def write_game_processes(self, names: list[str]) -> None:
        data = self._read_current_for_write()
        data["game_processes"] = names
        self._atomic_write(data)
        self._data["game_processes"] = list(names)

    def replace_all(self, data: dict[str, Any]) -> None:
        """ファイル全体を data で置き換える(世代の復元・バックアップの読み込み用)。形が違えば SettingsError。"""
        self._validate(data)
        self._atomic_write(data)

    def _atomic_write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        cb = self.on_written
        if cb is not None:
            try:
                cb()
            except Exception:  # noqa: BLE001 - 世代保存の失敗で設定の保存を失敗にしない
                logging.getLogger("deskkit.host.settings").exception("書き込み後の処理で例外")
