# 設定のバックアップと復元。settings.json の中身だけを1ファイルに書き出し・読み込む。
# 履歴・定型文・レイアウトなどのデータ本体は含めない(ClipShelf の本文は DPAPI で暗号化されており、別 PC では読めない)。
from __future__ import annotations

import datetime as _dt
import json
import shutil
from pathlib import Path
from typing import Any

from deskkit import __version__
from deskkit.settings import SettingsError, SettingsStore

MAGIC = "deskkit-settings-backup"


def export(store: SettingsStore, dest: Path) -> None:
    data = store.data
    upd = data.get("host", {}).get("update")
    if isinstance(upd, dict):
        upd = dict(upd)
        upd.pop("last_check", None)
        data["host"]["update"] = upd
    doc = {"format": MAGIC, "schema": 1, "app_version": __version__,
           "exported_at": _dt.datetime.now().isoformat(timespec="seconds"), "settings": data}
    dest.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read(src: Path) -> dict[str, Any]:
    """バックアップを読み、検証済みの settings を返す。形式が違えば SettingsError。"""
    try:
        doc = json.loads(src.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise SettingsError("ファイルを読めません(JSON ではありません)") from e
    if not isinstance(doc, dict) or doc.get("format") != MAGIC or not isinstance(doc.get("settings"), dict):
        raise SettingsError("DeskKit の設定バックアップではありません")
    SettingsStore._validate(doc["settings"])  # noqa: SLF001 - 形の検査だけ
    return dict(doc["settings"])


def summary(settings: dict[str, Any]) -> list[str]:
    mods = settings.get("modules", {})
    on = [n for n, s in mods.items() if isinstance(s, dict) and s.get("enabled")]
    lines = [f"有効なモジュール: {', '.join(on) if on else 'なし'}",
             f"ゲームとして扱うアプリ: {len(settings.get('game_processes', []))} 件"]
    for name, key, unit in (("dropsort", "rules", "ルール"), ("modeshift", "modes", "モード"), ("layoutkeep", "targets", "対象")):
        sec = mods.get(name, {})
        if isinstance(sec, dict) and isinstance(sec.get(key), list):
            lines.append(f"{name}: {len(sec[key])} {unit}")
    return lines


def restore(store: SettingsStore, settings: dict[str, Any]) -> Path:
    """今の settings.json を .bak-<日時> に退避してから、バックアップの内容で置き換える。退避先を返す。"""
    bak = store.path.with_name(f"settings.json.bak-{_dt.datetime.now():%Y%m%d-%H%M%S}")
    if store.path.exists():
        shutil.copyfile(store.path, bak)
    store._atomic_write(settings)  # noqa: SLF001
    return bak
