# 実行計画(Plan)と手順(Step)のデータ型、種別の表示名、CLI 用のタブ区切り表示。
# プレビュー・CLI・executor はすべてこの同じ Plan オブジェクトを見る(D-2 / INV-3)。
# Qt に依存しない。
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

ACTION_TYPES: tuple[str, ...] = (
    "launch_app", "close_app", "power_plan", "master_volume",
    "app_volume", "open_path", "open_url", "layout_apply",
    "mic_volume", "theme",          # v0.2 で追加(docs/v0.2/modeshift.md)
)

TYPE_LABELS: dict[str, str] = {
    "launch_app": "アプリ起動",
    "close_app": "アプリ終了",
    "power_plan": "電源プラン",
    "master_volume": "マスター音量",
    "app_volume": "アプリ音量",
    "open_path": "フォルダを開く",
    "open_url": "URL を開く",
    "layout_apply": "配置を適用",
    "mic_volume": "マイク",
    "theme": "アプリのテーマ",
}

# フォーカスを奪う/ウィンドウに作用するため、ゲーム中はスキップする種別(D-10)
# theme は WM_SETTINGCHANGE を全ウィンドウ(前面のゲームを含む)へ送るため、ゲーム中は同じくスキップする(C-4)
FOCUS_TYPES: frozenset[str] = frozenset({"launch_app", "close_app", "open_path", "open_url", "theme"})
# 「元に戻す」の対象になる種別(D-5。v0.2 でマイクとテーマを追加。どちらも値として読んで書き戻せる)
UNDOABLE_TYPES: frozenset[str] = frozenset({"power_plan", "master_volume", "app_volume", "mic_volume", "theme"})

# Step の結果
OK, SKIPPED, FAILED, STILL_RUNNING, ABORTED = "ok", "skipped", "failed", "still_running", "aborted"
RESULT_LABELS: dict[str, str] = {
    OK: "成功", SKIPPED: "スキップ", FAILED: "失敗", STILL_RUNNING: "終了せず", ABORTED: "中断",
}
GAME_REASON = "ゲーム中"

INTERACTIVE_SOURCES: frozenset[str] = frozenset({"tray", "hotkey", "gui"})


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def pct(level: float | None) -> str:
    return "?" if level is None else f"{round(level * 100)}%"


@dataclass
class Step:
    index: int                 # 1 始まり
    type: str
    target: str                # 画面・ログに出してよい表記(URL はクエリを落とす。INV-11)
    current: str
    new: str
    planned: bool              # True = 実行予定 / False = スキップ
    reason: str = ""           # スキップ理由・注記
    params: dict[str, Any] = field(default_factory=dict)  # executor が使う値(Plan の一部)
    result: str | None = None
    result_reason: str = ""

    @property
    def type_label(self) -> str:
        return TYPE_LABELS.get(self.type, self.type)

    def plan_text(self) -> str:
        if self.planned:
            return "実行" + (f"({self.reason})" if self.reason else "")
        return f"スキップ: {self.reason}" if self.reason else "スキップ"

    def result_text(self) -> str:
        if self.result is None:
            return ""
        label = RESULT_LABELS.get(self.result, self.result)
        return f"{label}: {self.result_reason}" if self.result_reason else label

    def signature(self) -> tuple[str, str, str]:
        """入口ごとの Plan の一致確認用(AC-5): 種別・対象・変更後の値。"""
        return (self.type, self.target, self.new)


@dataclass
class Plan:
    run_id: str
    kind: str                  # "switch" / "undo"
    mode: str                  # 対象モード名(undo は戻す元の切替のモード名)
    label: str
    source: str                # tray / hotkey / cli / auto / gui
    dry_run: bool
    steps: list[Step]
    def_hash: str = ""
    needs_confirmation: bool = False   # FR-8: 未確認モード
    in_game: bool = False              # 計画時点でゲーム中だったか
    created_at: str = field(default_factory=now_iso)
    executed: bool = False
    finished: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        c = {OK: 0, SKIPPED: 0, FAILED: 0, STILL_RUNNING: 0, ABORTED: 0}
        for s in self.steps:
            if s.result in c:
                c[s.result] += 1
        return c

    def planned_count(self) -> int:
        return sum(1 for s in self.steps if s.planned)

    def game_skipped(self) -> int:
        """ゲーム中のためスキップした(する)件数(FR-20 の通知用)。"""
        n = 0
        for s in self.steps:
            if (not s.planned and s.reason == GAME_REASON) or (s.result == SKIPPED and s.result_reason == GAME_REASON):
                n += 1
        return n

    def exit_code(self) -> int:
        """§9.1: 0 = 全 Step ok(skipped 含む) / 4 = failed・still_running・aborted あり。"""
        c = self.counts()
        return 4 if (c[FAILED] or c[STILL_RUNNING] or c[ABORTED]) else 0

    def signature(self) -> list[tuple[str, str, str]]:
        return [s.signature() for s in self.steps]

    @property
    def title(self) -> str:
        if self.kind == "undo":
            return f"元に戻す(「{self.label}」適用前へ)"
        return f"「{self.label}」に切り替え"


PLAN_COLUMNS = ("#", "種別", "対象", "現在", "変更後", "予定")


def _cell(s: str) -> str:
    return s.replace("\t", " ").replace("\r", " ").replace("\n", " ")


def plan_to_tsv(plan: Plan, *, with_results: bool = False) -> str:
    """§9.5: プレビューと同じ列をタブ区切りで。with_results なら結果列を足す。"""
    head = list(PLAN_COLUMNS) + (["結果"] if with_results else [])
    lines = [f"# {plan.title}  run_id={plan.run_id}" + ("  [dry-run]" if plan.dry_run else "")]
    if plan.needs_confirmation:
        lines.append("# 初回のため確認が必要(トレイかホットキーから実行し、プレビューで「実行」を押してください)")
    lines.append("\t".join(head))
    for s in plan.steps:
        row = [str(s.index), s.type_label, s.target, s.current, s.new, s.plan_text()]
        if with_results:
            row.append(s.result_text())
        lines.append("\t".join(_cell(x) for x in row))
    if with_results:
        c = plan.counts()
        lines.append(f"# 成功 {c[OK]} / スキップ {c[SKIPPED]} / 失敗 {c[FAILED]} / 終了せず {c[STILL_RUNNING]}"
                     + (f" / 中断 {c[ABORTED]}" if c[ABORTED] else ""))
    return "\n".join(lines)
