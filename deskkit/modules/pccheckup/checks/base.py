# 診断の結果(Finding)と「次にやること」(Action)、1つのチェック(Check)の型と、数値の表示用の小道具。
# チェックは「probes で値を読む → しきい値で判定 → Finding を返す」だけの関数にする(§2)。
# ここには GUI も OS 呼び出しも書かない(テストで表をそのまま確かめるため)。
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from deskkit.modules.pccheckup.probes import Probes

Status = Literal["good", "warn", "bad", "info", "unknown"]
Category = Literal["perf", "net", "storage"]
CATEGORIES: tuple[Category, ...] = ("perf", "net", "storage")
CATEGORY_LABELS: dict[str, str] = {"perf": "重い", "net": "ネット", "storage": "容量"}
CATEGORY_TITLES: dict[str, str] = {"perf": "重い", "net": "ネットが遅い・つながらない", "storage": "容量が足りない"}

# FR-3: 画面の並び順(悪い順)。bad → warn → unknown → info → good
STATUS_ORDER: dict[str, int] = {"bad": 0, "warn": 1, "unknown": 2, "info": 3, "good": 4}
# P-1: 画面では色だけでなく、この文字を必ず出す
STATUS_TEXT: dict[str, str] = {"good": "問題なし", "warn": "注意", "bad": "対処が必要", "info": "参考", "unknown": "読めませんでした"}
# FR-7: 悪化の比較に使う重さ(unknown は比べない)
SEVERITY: dict[str, int] = {"good": 0, "info": 1, "warn": 2, "bad": 3}

# Action の種類。FR-10: 開けるのは本書の表の URI・ごみ箱・タスクマネージャー・フォルダだけ
ActionKind = Literal["uri", "taskmgr", "folder", "category", "cleanup"]


@dataclass(frozen=True)
class Action:
    """次にやること。text は文。button があれば、そのボタンで target を開く。"""

    text: str
    kind: ActionKind | None = None
    target: str | None = None      # uri: ms-settings:... / folder: フォルダのパス / category: perf|net|storage
    button: str | None = None      # ボタンの文言


@dataclass(frozen=True)
class Row:
    """一覧の1行(S1 のドライブごと、S5 のフォルダごと)。path があれば「フォルダを開く」を出す。"""

    label: str
    value: str
    status: Status | None = None
    path: str | None = None
    note: str | None = None        # 「一部読めませんでした」「途中まで」など


@dataclass(frozen=True)
class Finding:
    check_id: str
    status: Status
    title: str
    detail: str
    actions: tuple[Action, ...] = ()
    value: str | None = None
    rows: tuple[Row, ...] = field(default_factory=tuple)


class Cancel:
    """「中止」の旗。待ち時間は wait() で取ると、中止ですぐ抜けられる。"""

    def __init__(self) -> None:
        self._ev = threading.Event()

    def set(self) -> None:
        self._ev.set()

    def is_set(self) -> bool:
        return self._ev.is_set()

    def wait(self, seconds: float) -> bool:
        return self._ev.wait(seconds)


@dataclass(frozen=True)
class Check:
    check_id: str
    category: Category
    progress: str                                   # 進み具合の文(「CPU を測っています(5 秒)」)
    run: Callable[[Probes, Cancel], Finding]


def unknown(check_id: str, title: str, detail: str = "この項目の値を読めませんでした。ほかの項目は続けて調べています。") -> Finding:
    return Finding(check_id, "unknown", title, detail)


def worst(*statuses: Status) -> Status:
    """いちばん悪い status(bad > warn > unknown > info > good)。"""
    return min(statuses, key=lambda s: STATUS_ORDER[s])


GiB = 1024 ** 3
MiB = 1024 ** 2


def fmt_bytes(n: float) -> str:
    """1.2GB / 350MB / 12KB(Windows の表示と同じく 1024 単位)。"""
    n = float(max(0.0, n))
    if n >= GiB:
        v = n / GiB
        return f"{v:.1f}GB" if v < 100 else f"{v:.0f}GB"
    if n >= MiB:
        return f"{n / MiB:.0f}MB"
    if n >= 1024:
        return f"{n / 1024:.0f}KB"
    return f"{n:.0f}B"


def fmt_pct(v: float) -> str:
    return f"{v:.0f}%"


def fmt_days(seconds: float) -> str:
    d = seconds / 86400
    if d >= 1:
        return f"{int(d)}日"
    h = seconds / 3600
    return f"{int(h)}時間" if h >= 1 else f"{int(seconds // 60)}分"
