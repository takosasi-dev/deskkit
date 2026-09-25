# power_plan: powercfg /list・/getactivescheme・/setactive <GUID> を引数リストで呼ぶ(CREATE_NO_WINDOW。INV-8)。
# 出力からは GUID を正規表現で取り出すだけ(D-9)。プラン名は画面表示用に添えるだけで照合には使わない。
# 設定後は /getactivescheme で読み戻して一致を確認する(FR-13)。
from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from typing import Any

from deskkit.modules.modeshift.actions.common import ExecEnv, PlanEnv
from deskkit.modules.modeshift.config import GUID_RE
from deskkit.modules.modeshift.model import FAILED, OK, SKIPPED, Step
from deskkit.modules.modeshift.system import PowerScheme

CREATE_NO_WINDOW = 0x08000000
GUID_FIND = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_NAME_AFTER = re.compile(r"\(([^()]*)\)")
UNEXPECTED = "出力書式が想定外"

Runner = Callable[[list[str]], tuple[int, str]]


def _decode(b: bytes) -> str:
    # 実機(日本語 Windows 11)ではパイプ経由の出力が UTF-8 だった。違えば ANSI コードページで読む
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("mbcs", errors="replace")


def powercfg_path() -> str:
    root = os.environ.get("SYSTEMROOT") or r"C:\Windows"
    p = os.path.join(root, "System32", "powercfg.exe")
    return p if os.path.isfile(p) else "powercfg.exe"


def run_powercfg(args: list[str]) -> tuple[int, str]:
    r = subprocess.run(  # noqa: S603 - 固定の exe を引数リストで呼ぶ
        [powercfg_path(), *args], capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW, check=False,
    )
    return r.returncode, _decode(r.stdout or b"") + _decode(r.stderr or b"")


def parse_list(text: str) -> list[PowerScheme]:
    """powercfg /list の出力 → GUID の並び。行末の '*' をアクティブの印として読む(表示用)。"""
    out: list[PowerScheme] = []
    seen: set[str] = set()
    for line in text.splitlines():
        m = GUID_FIND.search(line)
        if not m:
            continue
        guid = m.group(0).lower()
        if guid in seen:
            continue
        seen.add(guid)
        rest = line[m.end():]
        nm = _NAME_AFTER.search(rest)
        out.append(PowerScheme(guid, nm.group(1).strip() if nm else "", rest.rstrip().endswith("*")))
    return out


def parse_active(text: str) -> str | None:
    m = GUID_FIND.search(text)
    return m.group(0).lower() if m else None


class PowercfgPower:
    """PowerApi の実物。runner を差し替えればテストで出力書式を試せる。"""

    def __init__(self, runner: Runner | None = None) -> None:
        self._run = runner or run_powercfg

    def list_schemes(self) -> list[PowerScheme]:
        rc, text = self._run(["/list"])
        return parse_list(text) if rc == 0 else []

    def get_active(self) -> str | None:
        rc, text = self._run(["/getactivescheme"])
        return parse_active(text) if rc == 0 else None

    def set_active(self, guid: str) -> tuple[bool, str]:
        if not GUID_RE.match(guid):
            return False, "GUID の形式ではない"
        rc, _text = self._run(["/setactive", guid])
        if rc != 0:
            return False, f"powercfg /setactive が終了コード {rc}"
        back = self.get_active()
        if back is None:
            return False, f"読み戻せない({UNEXPECTED})"
        if back != guid.lower():
            return False, f"読み戻した GUID が違う({back})"
        return True, "読み戻して一致を確認"


def plan(a: dict[str, Any], env: PlanEnv) -> Step:
    guid: str = a["guid"]
    cur = env.power_active()
    step = Step(0, "power_plan", "電源プラン", env.scheme_name(cur), env.scheme_name(guid), True, params={"guid": guid})
    if cur is None:
        step.reason = f"現在値を読めない({UNEXPECTED})"
    elif cur == guid:
        step.planned, step.reason = False, "既に適用済み"
    elif env.power_schemes() and guid not in {s.guid for s in env.power_schemes()}:
        step.reason = "一覧に無い GUID"
    return step


def run(step: Step, env: ExecEnv) -> tuple[str, str]:
    guid: str = step.params["guid"]
    cur = env.backends.power.get_active()
    if cur is None:
        return FAILED, UNEXPECTED
    if cur == guid:
        return SKIPPED, "既に適用済み"
    ok, why = env.backends.power.set_active(guid)
    if not ok:
        if env.snapshot is not None:
            env.snapshot.setdefault("power", {})["written"] = None
        return FAILED, why
    if env.snapshot is not None and env.snapshot.get("power") is not None:
        env.snapshot["power"]["written"] = guid
    return OK, why
