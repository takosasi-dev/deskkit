# --selftest(FR-17 / AC-1)。偽の Win32Api に M-1〜M-6 を再現する固定データを入れ、計画が期待表と一致するか検査する。
# あわせて INV-2(曖昧は動かさない)・INV-6(undo 書き込み失敗で中止)・INV-7(dry_run で SetWindowPlacement 0回)を確かめる。
# データは一時フォルダに置き、実機のウィンドウにも %LOCALAPPDATA% にも触れない。
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import matcher, monitors, planner, windows
from .applier import Applier
from .config import compile_target
from .fake_win32 import FakeWin32, fake_monitor, fake_window
from .model import Layout, RawMonitor, SavedEntry
from .store import Store, StoreWriteError

NOTEPAD = "C:\\Windows\\notepad.exe"
MAIL = "C:\\Apps\\mail.exe"
TERM = "C:\\Apps\\term.exe"
EDIT = "C:\\Apps\\ed.exe"
CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
VIEWER = "C:\\Apps\\viewer.exe"
NOTES = "C:\\Apps\\notes.exe"

# (添字, 期待する規則, 期待する計画キー, 説明)
EXPECTED: list[tuple[int, str, str, str]] = [
    (0, "M-1", "move", "強い一致(タイトルが変わっても適用)"),
    (1, "M-2", "not_running", "起動していない"),
    (2, "M-3", "move", "残り1対1・regex 無し"),
    (3, "M-4", "move", "regex で1対1(alpha)"),
    (4, "M-4", "move", "regex で1対1(beta)"),
    (5, "M-5", "ambiguous", "Chrome 2枚 regex 無し"),
    (6, "M-5", "ambiguous", "Chrome 2枚 regex 無し"),
    (7, "M-6", "excluded:minimized", "強い一致の相手が最小化中"),
    (8, "M-6", "excluded:cloaked", "同じグループのウィンドウが cloaked だけ"),
    (9, "M-3", "unchanged", "既に保存位置"),
]


def _entry(exe: str, cls: str, hwnd: int, pid: int, rect: tuple[int, int, int, int], *, regex: str | None = None,
           proc_start: int | None = 1000, show: str = "normal") -> SavedEntry:
    return SavedEntry(exe=exe, cls=cls, title_regex=regex, title_at_save="", hwnd=hwnd, pid=pid, proc_start=proc_start,
                      show=show, normal_rect=rect, screen_rect=rect)


@dataclass
class Scenario:
    api: FakeWin32
    layout: Layout
    monitors: list[RawMonitor]
    targets: list[str]


def build_scenario() -> Scenario:
    mons = [fake_monitor("\\\\.\\DISPLAY1", (0, 0, 1920, 1080), primary=True, dpi=96),
            fake_monitor("\\\\.\\DISPLAY2", (1920, 0, 4480, 1440), dpi=144)]
    wins = [
        fake_window(101, 11, NOTEPAD, "Notepad", "変わったタイトル", rect=(50, 50, 600, 500), proc_start=500),
        fake_window(201, 21, TERM, "TermWnd", "shell", rect=(10, 10, 300, 300)),
        fake_window(301, 31, EDIT, "EdWnd", "alpha doc", rect=(0, 0, 100, 100)),
        fake_window(302, 31, EDIT, "EdWnd", "beta doc", rect=(0, 0, 100, 100)),
        fake_window(401, 41, CHROME, "Chrome_WidgetWin_1", "page A", rect=(0, 0, 800, 600)),
        fake_window(402, 41, CHROME, "Chrome_WidgetWin_1", "page B", rect=(0, 0, 800, 600)),
        fake_window(501, 51, VIEWER, "ViewWnd", "v", rect=(0, 0, 300, 300), iconic=True, proc_start=777),
        fake_window(601, 61, NOTES, "NotesWnd", "n", rect=(0, 0, 300, 300), cloaked=True),
        fake_window(701, 71, "C:\\Apps\\clock.exe", "ClockWnd", "c", rect=(2000, 100, 2400, 400)),
        fake_window(801, 4242, "C:\\DeskKit\\deskkit.exe", "Qt", "self"),  # 自分自身(除外される)
    ]
    entries = [
        _entry(NOTEPAD, "Notepad", 101, 11, (100, 100, 700, 600), proc_start=500),
        _entry(MAIL, "MailWnd", 111, 12, (0, 0, 500, 500)),
        _entry(TERM, "TermWnd", 999, 99, (1950, 50, 2600, 700)),
        _entry(EDIT, "EdWnd", 998, 98, (0, 0, 900, 1000), regex="^alpha"),
        _entry(EDIT, "EdWnd", 997, 97, (1920, 0, 2900, 1000), regex="^beta"),
        _entry(CHROME, "Chrome_WidgetWin_1", 996, 96, (0, 0, 960, 1040)),
        _entry(CHROME, "Chrome_WidgetWin_1", 995, 95, (1920, 0, 3200, 1400), show="maximized"),
        _entry(VIEWER, "ViewWnd", 501, 51, (200, 200, 600, 600), proc_start=777),
        _entry(NOTES, "NotesWnd", 994, 94, (0, 0, 400, 400)),
        _entry("C:\\Apps\\clock.exe", "ClockWnd", 993, 93, (2000, 100, 2400, 400)),
    ]
    sr = monitors.compute(mons, "device_interface")
    assert sr.signature is not None
    layout = Layout(signature=sr.signature, monitors=sr.normalized, saved_at="2026-09-24T21:00:00+09:00", windows=entries)
    targets = ["notepad.exe", "mail.exe", "term.exe", "ed.exe", "chrome.exe", "viewer.exe", "notes.exe", "clock.exe",
               "deskkit.exe"]
    return Scenario(FakeWin32(monitors=mons, windows=wins, pid=4242, foreground=701), layout, mons, targets)


def make_plan(sc: Scenario) -> planner.Plan:
    tg = [compile_target({"exe": t}) for t in sc.targets]
    wins = windows.snapshot(sc.api, tg, frozenset(), frozenset())
    ms = matcher.match(sc.layout.windows, windows.target_windows(wins))
    return planner.make_plan(sc.layout.signature, ms, sc.monitors)


class _FailingStore(Store):
    def write_undo(self, data: dict[str, object]) -> None:
        raise StoreWriteError("undo.json を書けません: 読み取り専用(selftest)")


def run() -> int:
    ok = True
    lines: list[str] = []

    def check(cond: bool, label: str) -> None:
        nonlocal ok
        ok = ok and cond
        lines.append(f"  [{'OK' if cond else 'NG'}] {label}")

    sc = build_scenario()
    plan = make_plan(sc)
    by_index = {i.index: i for i in plan.items}
    seen_rules: dict[str, int] = {}
    lines.append("決定表(M-1〜M-6)の検査:")
    for idx, rule, key, desc in EXPECTED:
        it = by_index.get(idx)
        good = it is not None and it.rule == rule and it.key == key
        if it is not None:
            seen_rules[it.rule] = seen_rules.get(it.rule, 0) + (1 if good else 0)
        got = f"{it.rule}/{it.key}" if it else "なし"
        check(good, f"#{idx} {desc}: 期待 {rule}/{key} → 実際 {got}")
    for r in ("M-1", "M-2", "M-3", "M-4", "M-5", "M-6"):
        n = seen_rules.get(r, 0)
        check(n >= 1, f"{r} を {n} 件検査")

    with tempfile.TemporaryDirectory(prefix="lk-selftest-") as tmp:
        root = Path(tmp)
        # INV-7: dry_run では SetWindowPlacement を1回も呼ばない
        sc_dry = build_scenario()
        res = Applier(sc_dry.api, Store(root / "dry")).apply(make_plan(sc_dry), dry_run=True)
        check(res.status == "dry_run" and not sc_dry.api.set_calls, "dry_run で SetWindowPlacement 0 回(INV-7)")
        # INV-6: undo.json を書けなければ1件も動かさない
        sc_fail = build_scenario()
        res = Applier(sc_fail.api, _FailingStore(root / "fail")).apply(make_plan(sc_fail), dry_run=False)
        check(res.status == "undo_failed" and not sc_fail.api.set_calls, "undo.json 書き込み失敗で適用中止(INV-6)")
        # live: 動かすのは計画の move だけ。曖昧(M-5)のウィンドウは動かさない(INV-2)
        sc_live = build_scenario()
        store = Store(root / "live")
        p = make_plan(sc_live)
        res = Applier(sc_live.api, store).apply(p, dry_run=False)
        called = {h for h, _ in sc_live.api.set_calls}
        check(called == {m.hwnd for m in p.moves} and not called & {401, 402},
              f"live 適用は move だけ・曖昧なウィンドウは動かさない(INV-2) moved={res.moved}")
        undo = store.read_undo()
        check(undo is not None and len(undo["windows"]) == len(p.moves), "適用前に undo.json へ全件を退避(C-8)")
        check(res.focus_kept is True, "SW_SHOWNOACTIVATE / 最大化で foreground が変わらない(FR-16、偽物上)")
        # 取り消し
        n_before = len(sc_live.api.set_calls)
        ures = Applier(sc_live.api, store).undo(p.signature, dry_run=False)
        check(ures.moved == len(p.moves) and store.read_undo() is None and len(sc_live.api.set_calls) > n_before,
              "直前の適用を元に戻し、undo.json を空にする(FR-9)")
        check(Applier(sc_live.api, store).undo("000000000000", dry_run=False).status in ("nothing", "sig_mismatch"),
              "構成が違えば取り消さない")

    # シグネチャ: 同じ構成なら同じ値 / ID が取れなければ None
    sigs = {monitors.compute(sc.monitors, "device_interface").signature for _ in range(3)}
    check(len(sigs) == 1, "同じ構成のシグネチャは毎回同じ(FR-2)")
    swapped = list(reversed(sc.monitors))
    check(monitors.compute(swapped, "device_interface").signature in sigs, "列挙順が変わってもシグネチャは同じ")
    broken = [fake_monitor("\\\\.\\DISPLAY1", (0, 0, 1920, 1080), primary=True, mid=None)]
    check(monitors.compute(broken, "device_interface").signature is None, "ID が取れないモニタがあればシグネチャは null")

    _out("LayoutKeep selftest")
    for ln in lines:
        _out(ln)
    _out("結果: " + ("合格" if ok else "不合格"))
    return 0 if ok else 1


def _out(text: str) -> None:
    """標準出力が無い・文字コードが合わない環境(exe 化など)でも落ちないように出す。"""
    try:
        print(text, flush=True)
    except (UnicodeEncodeError, AttributeError, OSError, ValueError):
        pass
