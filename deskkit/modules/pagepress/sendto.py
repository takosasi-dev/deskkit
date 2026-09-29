# エクスプローラーの「送る」への登録/解除(Q-2 の回答: 登録できるようにする。作りは SendPrep と同じ)。
# %APPDATA%\Microsoft\Windows\SendTo\PagePress でまとめる.lnk を、画面の設定でオンにしたときだけ作る。
# 消すのは自分が作った物(リンク先が DeskKit で、引数が pagepress open)だけ。DESKKIT_HOME があるとき(テスト・selftest)は、その下を使う。
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from deskkit.modules.pagepress import naming
from deskkit.modules.pagepress._win32 import ShellLinkApi

LINK_NAME = "PagePress でまとめる.lnk"
DESCRIPTION = "DeskKit の PagePress で、PDF や写真を1つの PDF にまとめます"
CLI_ARGS = "pagepress open"

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
    from deskkit.modules.pagepress._win32 import sendto_dir

    return sendto_dir()


def launch_spec() -> LaunchSpec:
    """exe なら `DeskKit.exe pagepress open`、ソース実行なら `pythonw.exe run_deskkit.py pagepress open`(契約 H-B)。"""
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
    """作る(自分の物があれば作り直す)。問題があれば画面に出す文を返す。"""
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
    """自分が作った物なら消す。戻り値: removed / absent / not_ours / failed。"""
    st = status(api, folder)
    if st == STATUS_ABSENT:
        return "absent"
    if st == STATUS_OTHER:
        return "not_ours"
    p = link_path(folder)
    naming.remove_own(p)  # 自分が作ったショートカット(消す処理は naming の1か所に集める。AC-18)
    return "failed" if p.exists() else "removed"
