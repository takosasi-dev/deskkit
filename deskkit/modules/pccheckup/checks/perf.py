# 「重い」の診断 P1〜P7(§6.2)。値は probes から読み、しきい値(P-2: コードの定数)で判定して Finding を返す。
# プロセス名は Finding(画面・コピー用)にだけ入れる。ログ・履歴には status しか残らない(INV-3)。
# Windows の状態は変えない。次にやることは文と、設定画面・タスクマネージャーを開くボタンで示すだけ(INV-2)。
from __future__ import annotations

from deskkit.modules.pccheckup.checks.base import (
    Action,
    Cancel,
    Check,
    Finding,
    GiB,
    Row,
    Status,
    fmt_bytes,
    fmt_days,
    fmt_pct,
)
from deskkit.modules.pccheckup.probes import (
    OVERLAY_BEST_EFFICIENCY,
    CpuSample,
    DiskInfo,
    MemInfo,
    PowerInfo,
    Probes,
    StartupInfo,
)

# ---- しきい値(P-2)
CPU_SECONDS = 5            # P-3: 1 秒ごとに 5 回
CPU_BAD = 85.0             # 平均がこれ以上で bad
CPU_WARN = 60.0
MEM_BAD = 90.0
MEM_WARN = 80.0
DISK_BAD_PCT = 5.0         # 空きがこれ未満で bad
DISK_BAD_BYTES = 5 * GiB
DISK_WARN_PCT = 10.0
DISK_WARN_BYTES = 15 * GiB
UPTIME_WARN_S = 7 * 86400  # 7 日以上で warn
STARTUP_WARN = 15
STARTUP_BAD = 25

def _names(top: tuple[object, ...], n: int = 3) -> str:
    return "、".join(str(getattr(p, "name", "")) for p in top[:n])


# ------------------------------------------------------------------ P1 CPU
def judge_cpu(s: CpuSample) -> Finding:
    pct = s.percent
    status: Status = "bad" if pct >= CPU_BAD else "warn" if pct >= CPU_WARN else "good"
    rows = tuple(Row(p.name, fmt_pct(p.value)) for p in s.top)
    head = f"この {s.samples} 秒間の CPU の使用率は、平均 {pct:.0f}% でした。"
    if s.top:
        head += f"いちばん使っているのは {s.top[0].name}({s.top[0].value:.0f}%)です。"
    if status == "good":
        return Finding("P1", "good", "CPU には余裕があります", head, (), fmt_pct(pct), rows)
    title = "CPU がほぼ使い切られています" if status == "bad" else "CPU が混み合っています"
    acts = (
        Action(f"{_names(s.top)} を使っていなければ閉じてください。" if s.top else "使っていないアプリを閉じてください。",
               "taskmgr", "taskmgr.exe", "タスクマネージャーを開く"),
    )
    return Finding("P1", status, title, head, acts, fmt_pct(pct), rows)


# ------------------------------------------------------------------ P2 メモリ
def judge_memory(m: MemInfo) -> Finding:
    pct = m.percent
    status: Status = "bad" if pct >= MEM_BAD else "warn" if pct >= MEM_WARN else "good"
    rows = tuple(Row(p.name, fmt_bytes(p.value)) for p in m.top)
    head = f"メモリが {pct:.0f}% 使われています(全体 {fmt_bytes(m.total)})。"
    if m.top:
        head += f"いちばん使っているのは {m.top[0].name}({fmt_bytes(m.top[0].value)})です。"
    if status == "good":
        return Finding("P2", "good", "メモリには余裕があります", head, (), fmt_pct(pct), rows)
    title = "メモリが足りていません" if status == "bad" else "メモリが残り少なくなっています"
    acts = (
        Action("ブラウザのタブを減らしてください。"),
        Action(f"使っていないアプリを閉じてください(多く使っている順: {_names(m.top)})。" if m.top else "使っていないアプリを閉じてください。",
               "taskmgr", "taskmgr.exe", "タスクマネージャーを開く"),
    )
    return Finding("P2", status, title, head, acts, fmt_pct(pct), rows)


# ------------------------------------------------------------------ P3 システムドライブ(S1 と同じしきい値)
def disk_status(d: DiskInfo) -> Status:
    if d.total <= 0:
        return "unknown"
    pct = d.free / d.total * 100
    if pct < DISK_BAD_PCT or d.free < DISK_BAD_BYTES:
        return "bad"
    if pct < DISK_WARN_PCT or d.free < DISK_WARN_BYTES:
        return "warn"
    return "good"


def disk_value(d: DiskInfo) -> str:
    pct = d.free / d.total * 100 if d.total > 0 else 0.0
    return f"空き {fmt_bytes(d.free)}({pct:.0f}%)"


def judge_system_drive(d: DiskInfo) -> Finding:
    status = disk_status(d)
    head = f"システムドライブ({d.root.rstrip(chr(92))})の空きは {fmt_bytes(d.free)} です(全体 {fmt_bytes(d.total)})。"
    if status in ("good", "unknown"):
        return Finding("P3", status, "システムドライブの空きは十分です" if status == "good" else "システムドライブの空きを読めませんでした",
                       head, (), disk_value(d))
    title = "システムドライブがいっぱいです" if status == "bad" else "システムドライブの空きが少なくなっています"
    detail = head + "空きが少ないと、Windows の動きが遅くなったり、更新が失敗したりします。"
    acts = (Action("「容量が足りない」の診断で、空けられる場所を調べてください。", "category", "storage", "容量を調べる"),)
    return Finding("P3", status, title, detail, acts, disk_value(d))


# ------------------------------------------------------------------ P4 起動からの時間
def judge_uptime(seconds: float) -> Finding:
    v = fmt_days(seconds)
    if seconds >= UPTIME_WARN_S:
        return Finding("P4", "warn", "長い間、再起動していません",
                       f"最後に起動してから {v} たっています。長く動かし続けると、少しずつ動きが重くなることがあります。",
                       (Action("スタートメニューの電源ボタンから『再起動』を選んでください。"
                               "『シャットダウン』では高速スタートアップのためリセットされません。"),), v)
    return Finding("P4", "good", "最近、再起動しています", f"最後に起動してから {v} です。", (), v)


# ------------------------------------------------------------------ P5 スタートアップ
def judge_startup(s: StartupInfo) -> Finding:
    n = s.enabled
    status: Status = "bad" if n >= STARTUP_BAD else "warn" if n >= STARTUP_WARN else "good"
    v = f"{n} 個"
    detail = f"PC の起動と同時に動き出すアプリが {n} 個あります(無効にしてあるものが別に {s.disabled} 個)。"
    act = Action("使っていないアプリのスタートアップをオフにしてください。", "uri", "ms-settings:startupapps", "スタートアップの設定を開く")
    if status == "good":
        return Finding("P5", "good", "起動時に動くアプリは多くありません", detail, (act,), v)
    title = "起動時に動くアプリがとても多いです" if status == "bad" else "起動時に動くアプリが多めです"
    return Finding("P5", status, title, detail + "多いと、起動が遅くなり、ふだんの動きも重くなります。", (act,), v)


# ------------------------------------------------------------------ P6 電源
def judge_power(p: PowerInfo) -> Finding:
    saver = p.has_battery and p.saver  # §10: バッテリーの無い PC は電源モードだけで判定する
    efficiency = p.overlay == OVERLAY_BEST_EFFICIENCY
    if not p.has_battery:
        v = "電源につながっています"
    elif p.on_battery:
        v = "バッテリー駆動" + (f"({p.battery_percent}%)" if p.battery_percent is not None else "")
    else:
        v = "電源につながっています"
    settings = Action("", "uri", "ms-settings:powersleep", "電源の設定を開く")
    if saver or efficiency:
        parts = []
        if saver:
            parts.append("バッテリー節約機能がオンになっています")
        if efficiency:
            parts.append("電源モードが『最適な電力効率』になっています")
        acts: list[Action] = []
        if p.on_battery:
            acts.append(Action("電源につないでください。"))
        acts.append(Action("電源モードを『バランス』にしてください。", settings.kind, settings.target, settings.button))
        return Finding("P6", "warn", "省電力のために動きを抑えています",
                       "。".join(parts) + "。電池を長持ちさせる代わりに、動きが遅くなります。", tuple(acts), v)
    detail = "省電力のために動きを抑える設定にはなっていません。"
    if p.overlay is None:
        detail += "(電源モードは読めませんでした)"
    return Finding("P6", "good", "電源の設定は問題ありません", detail, (), v)


# ------------------------------------------------------------------ P7 更新の再起動待ち
def judge_reboot(pending: bool) -> Finding:
    if pending:
        return Finding("P7", "warn", "Windows の更新が再起動を待っています",
                       "更新が途中のまま止まっています。仕上げるまで、動きが重くなることがあります。",
                       (Action("更新を仕上げるため、再起動してください。"),), "再起動待ち")
    return Finding("P7", "good", "再起動を待っている更新はありません", "Windows の更新は再起動を待っていません。", (), "なし")


def _run_cpu(p: Probes, c: Cancel) -> Finding:
    return judge_cpu(p.cpu(CPU_SECONDS, c))


CHECKS: tuple[Check, ...] = (
    Check("P1", "perf", f"CPU を測っています({CPU_SECONDS} 秒)", _run_cpu),
    Check("P2", "perf", "メモリを調べています", lambda p, _c: judge_memory(p.memory())),
    Check("P3", "perf", "システムドライブの空きを調べています", lambda p, _c: judge_system_drive(p.system_drive())),
    Check("P4", "perf", "起動からの時間を調べています", lambda p, _c: judge_uptime(p.uptime_seconds())),
    Check("P5", "perf", "スタートアップのアプリを数えています", lambda p, _c: judge_startup(p.startup())),
    Check("P6", "perf", "電源の設定を調べています", lambda p, _c: judge_power(p.power())),
    Check("P7", "perf", "Windows の更新を調べています", lambda p, _c: judge_reboot(p.reboot_pending())),
)
TITLES: dict[str, str] = {"P1": "CPU", "P2": "メモリ", "P3": "システムドライブの空き", "P4": "起動からの時間",
                          "P5": "スタートアップのアプリ", "P6": "電源の設定", "P7": "Windows の更新"}
