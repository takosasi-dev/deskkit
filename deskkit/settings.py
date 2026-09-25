# %APPDATA%\DeskKit\settings.json の読み書き。書き込みは一時ファイル+os.replace(D-9)。
# 構文エラー時は前回正常に読めた設定を保持し、ファイルは書き換えない。セクション単位で検証する。
from __future__ import annotations

import copy
import json
import os
import tempfile
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
        },
        "modules": {name: {"enabled": False} for name in MODULE_NAMES},
    }


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
        if not isinstance(merged_host.get("handler_error_limit"), int) or merged_host["handler_error_limit"] < 1:
            merged_host["handler_error_limit"] = base["host"]["handler_error_limit"]
        out["host"] = merged_host
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
