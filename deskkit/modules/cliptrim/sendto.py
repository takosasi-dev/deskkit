# エクスプローラーの「送る」への登録/解除(Q-5 の回答)。%APPDATA%\Microsoft\Windows\SendTo\ClipTrim で切り出す.lnk を作る(SendPrep から写した)。
# 消すのは自分が作ったもの(リンク先が DeskKit で、引数が cliptrim open)だけ。同じ名前の他人のショートカットは消さない・上書きしない。
# DESKKIT_HOME があるとき(テスト・selftest)は、その下の SendTo フォルダを使い、本物の「送る」には触れない。
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from deskkit.modules.cliptrim._win32 import ShellLinkApi

LINK_NAME = "ClipTrim で切り出す.lnk"
DESCRIPTION = "DeskKit の ClipTrim で、動画の要るところだけを切り出します"
CLI_ARGS = "cliptrim open"

STATUS_REGISTERED = "registered"
STATUS_ABSENT = "absent"
STATUS_OTHER = "other"   # 同じ名前の、自分が作っていないショートカットがある


@dataclass(frozen=True)
class LaunchSpec:
    target: str
    args: str
    workdir: str
    icon: str


def default_folder() -> Path:
    home = os.environ.get("DESKKIT_HOME")
    if home:
        return Path(home) / "SendTo"
    from deskkit.modules.cliptrim._win32 import sendto_dir

    return sendto_dir()


def launch_spec() -> LaunchSpec:
    """exe なら `DeskKit.exe cliptrim open`、ソース実行なら `pythonw.exe run_deskkit.py cliptrim open`(契約 H-B)。"""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        return LaunchSpec(str(exe), CLI_ARGS, str(exe.parent), str(exe))
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    target = pyw if pyw.exists() else exe
    import deskkit

    root = Path(deskkit.__file__).resolve().parent.parent
    script = root / "run_deskkit.py"
    return LaunchSpec(str(target), f'"{script}" {CLI_ARGS}', str(root), str(target))


def is_ours(target: str, args: str) -> bool:
    name = Path(target.strip('"')).name.lower() if target else ""
    a = " ".join(args.lower().split())
    if CLI_ARGS not in a:
        return False
    if name == "deskkit.exe":
        return True
    return name in ("pythonw.exe", "python.exe") and ("run_deskkit.py" in a or "-m deskkit" in a)


def link_path(folder: Path) -> Path:
    return folder / LINK_NAME


def status(api: ShellLinkApi, folder: Path) -> str:
    p = link_path(folder)
    if not p.exists():
        return STATUS_ABSENT
    got = api.read(p)
    return STATUS_REGISTERED if got is not None and is_ours(*got) else STATUS_OTHER


def register(api: ShellLinkApi, folder: Path, spec: LaunchSpec | None = None) -> str | None:
    """作る(自分のものがあれば作り直す)。問題があれば画面に出す文を返す。"""
    st = status(api, folder)
    if st == STATUS_OTHER:
        return "「送る」に同じ名前のショートカットがあるため、追加しませんでした"
    s = spec or launch_spec()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        api.create(link_path(folder), s.target, s.args, s.workdir, s.icon, DESCRIPTION)
    except OSError:
        return "「送る」に追加できませんでした"
    return None


def unregister(api: ShellLinkApi, folder: Path) -> str:
    """自分が作ったものなら消す(VINV-2 の対象外: 自分が作ったショートカット)。戻り値: removed / absent / not_ours / failed。"""
    st = status(api, folder)
    if st == STATUS_ABSENT:
        return "absent"
    if st == STATUS_OTHER:
        return "not_ours"
    try:
        # 消すのは自分が作った「送る」のショートカットだけ(status が registered = リンク先が DeskKit の cliptrim open)。
        # 利用者のファイルではない(VINV-2・AC-15 の対象外。仕様書 Q-5 の回答「オフにしたら自分が作ったショートカットだけを消す」)
        link_path(folder).unlink()
    except OSError:
        return "failed"
    return "removed"
