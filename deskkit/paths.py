# 設定・データ・ログの置き場(C-7)を1か所で解決する。
# 環境変数 DESKKIT_HOME があれば、selftest やテスト用にその下へまとめて置く。
from __future__ import annotations

import os
import sys
from pathlib import Path


def _home_override() -> Path | None:
    v = os.environ.get("DESKKIT_HOME")
    return Path(v) if v else None


def roaming_dir() -> Path:
    o = _home_override()
    if o is not None:
        return o / "roaming"
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "DeskKit"


def local_dir() -> Path:
    o = _home_override()
    if o is not None:
        return o / "local"
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "DeskKit"


def settings_path() -> Path:
    return roaming_dir() / "settings.json"


def log_dir() -> Path:
    p = local_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def data_dir(module: str) -> Path:
    p = local_dir() / module
    p.mkdir(parents=True, exist_ok=True)
    return p


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def child_env() -> dict[str, str]:
    """DeskKit.exe(自分や新しい版)を別プロセスとして起動するときの環境変数。
    onefile の exe から自分の exe を起動すると、既定では親の展開先フォルダを使い回す「子」として扱われ、
    親が終わると展開先が消えて動かなくなる。PYINSTALLER_RESET_ENVIRONMENT=1 で独立した起動にする(ソース実行では無害)。"""
    env = dict(os.environ)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def source_root() -> Path:
    """ソース実行のときのリポジトリ直下(deskkit パッケージの1つ上。pip install していないので cwd に使う)。"""
    return Path(__file__).resolve().parents[1]


def launch_command() -> list[str]:
    """自動起動や再起動に使う、今の DeskKit を起動するコマンド。"""
    if is_frozen():
        return [sys.executable]
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return [str(pyw if pyw.exists() else exe), "-m", "deskkit"]
