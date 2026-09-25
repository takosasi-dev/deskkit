# PcCheckup モジュール本体。診断の実行(GUI スレッドの外)・状態・履歴・操作記録・見張り・クイックアクション・
# 「結果をコピー」・設定画面やフォルダを開く操作・古い一時ファイルのごみ箱送り(FR-9)を束ね、画面(page.py)に窓口を出す。
# 利用者が押したときだけ動く(見張りは既定オフ)。ログ・履歴・diagnostics にはプロセス名・パス・SSID を書かない(INV-3)。
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Signal

from deskkit.catalog import info
from deskkit.modules.pccheckup import cleanup
from deskkit.modules.pccheckup.checks.base import CATEGORIES, STATUS_ORDER, Action, Cancel, Finding, fmt_bytes
from deskkit.modules.pccheckup.history import History, OpsLog, worsened
from deskkit.modules.pccheckup.probes import DiskInfo, Probes, RealProbes
from deskkit.modules.pccheckup.report import Secrets, build
from deskkit.modules.pccheckup.runner import ALL_IDS, BY_CATEGORY, CategoryResult, run_category
from deskkit.modules.pccheckup.watcher import DiskWatcher
from deskkit.usage import UsageSeries, count_jsonl

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

    from deskkit.fileops import RecycleResult

DEFAULTS: dict[str, Any] = {"watch_disk": False}
STOP_WAIT_S = 60.0  # §10: ごみ箱送りの最中に終了したら、recycle の呼び出しが終わるまで待つ
# FR-10: action のボタンが開いてよいもの(本書の表の URI とごみ箱)。これ以外は開かない
ALLOWED_URIS: frozenset[str] = frozenset({
    "ms-settings:startupapps", "ms-settings:powersleep", "ms-settings:network-status", "ms-settings:network-wifi",
    "ms-settings:network-proxy", "ms-settings:storagesense", "shell:RecycleBinFolder",
})
TASKMGR = "taskmgr.exe"
CATEGORY_GLYPHS: dict[str, str] = {"perf": "", "net": "", "storage": ""}
QUICK_ACTIONS: tuple[tuple[str, str, str], ...] = (
    ("perf", "PC が重い原因を調べる", "pccheckup 重い 遅い cpu メモリ 診断"),
    ("net", "ネットの不調を調べる", "pccheckup ネット wifi インターネット つながらない 診断"),
    ("storage", "容量を調べる", "pccheckup 容量 空き ディスク ストレージ 診断"),
)


class Notifier(QObject):
    changed = Signal()               # 実行中か・進み具合が変わった
    run_started = Signal(object)     # カテゴリの一覧
    finding = Signal(str, object)    # (カテゴリ, Finding)
    category_done = Signal(str)
    run_finished = Signal()
    history_changed = Signal()
    settings_changed = Signal()


@dataclass
class RunState:
    running: bool = False
    categories: list[str] = field(default_factory=list)
    results: dict[str, list[Finding]] = field(default_factory=dict)
    ms: dict[str, int] = field(default_factory=dict)
    cancelled: set[str] = field(default_factory=set)
    done: set[str] = field(default_factory=set)
    failed: list[str] = field(default_factory=list)
    worse: set[str] = field(default_factory=set)
    progress_text: str = ""
    progress_category: str = ""
    step: int = 0
    total: int = 0
    started: datetime | None = None
    finished: datetime | None = None
    cancel_requested: bool = False

    def counts(self) -> dict[str, int]:
        c = dict.fromkeys(STATUS_ORDER, 0)
        for fs in self.results.values():
            for f in fs:
                c[f.status] += 1
        return c


def _merge_defaults(section: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    out = dict(section)
    changed = False
    for k, v in DEFAULTS.items():
        if not isinstance(out.get(k), type(v)):
            out[k] = v
            changed = True
    return out, changed


def _default_recycle(paths: Sequence[Path], parent_hwnd: int | None) -> RecycleResult:
    from deskkit.fileops import recycle

    return recycle(paths, parent_hwnd)


def _default_startfile(target: str) -> None:
    os.startfile(target)  # type: ignore[attr-defined,unused-ignore]  # 既定の動詞(開く)だけ


class PcCheckupModule:
    def __init__(self, ctx: Any, *, probes_factory: Callable[[], Probes] | None = None,
                 recycle_fn: cleanup.RecycleFn | None = None, threaded: bool = True,
                 now: Callable[[], float] = time.time, startfile: Callable[[str], None] | None = None) -> None:
        self.ctx = ctx
        self.log: logging.Logger = ctx.log
        self.accent = info("pccheckup").accent
        section = dict(ctx.settings_dict())
        section.pop("enabled", None)
        merged, changed = _merge_defaults(section)
        if changed:
            try:
                ctx.write_settings(merged)
            except Exception as e:  # noqa: BLE001 - 書き戻せなくても既定値で動く
                self.log.warning("既定値の書き戻しに失敗: %s", type(e).__name__)
        self.watch_disk: bool = bool(merged["watch_disk"])
        self._probes_factory: Callable[[], Probes] = probes_factory or (lambda: RealProbes(now))
        self._recycle: cleanup.RecycleFn = recycle_fn or _default_recycle
        self._threaded = threaded
        self._now = now
        self._startfile: Callable[[str], None] = startfile or _default_startfile
        self.notifier = Notifier()
        self.history = History(ctx.data_dir / "history.jsonl", ALL_IDS)
        self.ops = OpsLog(ctx.data_dir / "ops.jsonl")
        self.state = RunState()
        self._prev: dict[str, dict[str, str] | None] = {}
        self._cancel: Cancel | None = None
        self._run_thread: threading.Thread | None = None
        self._cleanup_thread: threading.Thread | None = None
        self._cleanup_stop = threading.Event()
        self._ssids: set[str] = set()
        self._stopping = False
        self.watcher = DiskWatcher(ctx, ctx.data_dir / "watch.json", self._read_system_drive, self._on_watch_alert,
                                   self.log, now)

    # ================================================================ ライフサイクル
    def start(self) -> None:
        self._stopping = False
        add = getattr(self.ctx, "add_quick_action", None)
        if add is not None:  # FR-13
            for cat, label, kw in QUICK_ACTIONS:
                add(label, partial(self.open_and_run, cat), keywords=kw, glyph=CATEGORY_GLYPHS[cat],
                    enabled=lambda: not self.state.running)
        if self.watch_disk:
            self.watcher.start()
        self._update_status()
        self.log.info("pccheckup started watch_disk=%s", self.watch_disk)

    def stop(self) -> None:
        self._stopping = True
        if self._cancel is not None:
            self._cancel.set()
        self.watcher.stop()
        self._cleanup_stop.set()
        t = self._cleanup_thread
        if t is not None and t.is_alive():
            t.join(STOP_WAIT_S)  # 呼び出し中の recycle が終わるまで待つ(§10)
        r = self._run_thread
        if r is not None and r.is_alive():
            r.join(2.0)  # 測定の待ち・フォルダの走査は中止の旗を見てすぐ抜ける
        self.log.info("pccheckup stopped")

    def handle_cli(self, args: list[str]) -> tuple[int, str]:
        return 2, "unsupported"

    def create_page(self) -> QWidget:
        from deskkit.modules.pccheckup.page import PcCheckupPage

        return PcCheckupPage(self)

    # ================================================================ 診断
    def open_and_run(self, category: str) -> None:
        """クイックアクション・通知のクリック(FR-12・FR-13): 画面を開いて、そのカテゴリを診断する。"""
        show = getattr(self.ctx, "show_page", None)
        if show is not None:
            show()
        self.run([category])

    def run(self, categories: Sequence[str]) -> bool:
        """診断を始める。実行中なら何もしない(FR-2: ボタンは無効)。"""
        cats = [c for c in categories if c in BY_CATEGORY]
        if self.state.running or not cats or self._stopping:
            return False
        self._cancel = Cancel()
        self.state = RunState(running=True, categories=cats, results={c: [] for c in cats},
                              started=datetime.fromtimestamp(self._now()).astimezone(), total=len(BY_CATEGORY[cats[0]]),
                              progress_category=cats[0])
        self._prev = {c: self.history.last(c) for c in cats}
        self.log.info("diagnosis start categories=%s", ",".join(cats))
        self.notifier.run_started.emit(list(cats))
        self.notifier.changed.emit()
        cancel = self._cancel
        if self._threaded:
            self._run_thread = threading.Thread(target=self._worker, args=(cats, cancel), name="pccheckup-run", daemon=True)
            self._run_thread.start()
        else:
            self._worker(cats, cancel)
        return True

    def cancel(self) -> None:
        if self.state.running and self._cancel is not None:
            self._cancel.set()
            self.state.cancel_requested = True
            self.state.progress_text = "中止しています(いま調べている項目が終わるまで)"
            self.notifier.changed.emit()

    def _post(self, fn: Callable[[], None]) -> None:
        if self._threaded:
            self.ctx.call_soon(fn)
        else:
            fn()

    def _worker(self, cats: list[str], cancel: Cancel) -> None:
        try:
            probes = self._probes_factory()
            for cat in cats:
                if cancel.is_set():
                    self._post(partial(self._on_category_done, CategoryResult(cat, cancelled=True)))
                    continue
                res = run_category(
                    cat, probes, cancel, self.log,
                    on_progress=lambda c, i, n, t: self._post(partial(self._on_progress, c, i, n, t)),
                    on_finding=lambda c, f: self._post(partial(self._on_finding, c, f)),
                )
                self._post(partial(self._on_category_done, res))
            ssid = self._peek_ssid(probes)
            if ssid:
                self._post(partial(self._ssids.add, ssid))
        except Exception as e:  # noqa: BLE001 - 途中で落ちても「終わった」ことは画面へ返す
            self.log.warning("diagnosis worker failed: %s", type(e).__name__)
        finally:
            self._post(self._on_run_done)

    @staticmethod
    def _peek_ssid(probes: Probes) -> str | None:
        """コピー文字列から伏せるためだけに、N1 で読めた SSID を覚える(読み直しはしない。画面・ログには出さない)。"""
        memo = getattr(probes, "_memo", None)
        if isinstance(memo, dict) and "conn" in memo and memo["conn"][0] == "ok":
            v = getattr(memo["conn"][1], "ssid", None)
            return v if isinstance(v, str) else None
        return None

    def _on_progress(self, category: str, i: int, n: int, text: str) -> None:
        if self.state.cancel_requested:
            return
        self.state.progress_category = category
        self.state.step = i
        self.state.total = n
        self.state.progress_text = text
        self.notifier.changed.emit()

    def _on_finding(self, category: str, f: Finding) -> None:
        self.state.results.setdefault(category, []).append(f)
        if worsened(self._prev.get(category), f.check_id, f.status):
            self.state.worse.add(f.check_id)
        self.notifier.finding.emit(category, f)

    def _on_category_done(self, res: CategoryResult) -> None:
        st = self.state
        st.ms[res.category] = res.ms
        st.done.add(res.category)
        st.failed.extend(res.failed)
        if res.cancelled:
            st.cancelled.add(res.category)
        else:
            try:
                self.history.append(res.category, {f.check_id: f.status for f in res.findings}, res.ms)
                self.notifier.history_changed.emit()
            except (OSError, ValueError) as e:
                self.log.warning("history write failed: %s", type(e).__name__)
        c = {s: sum(1 for f in res.findings if f.status == s) for s in STATUS_ORDER}
        self.log.info("diagnosis category=%s ms=%d cancelled=%s bad=%d warn=%d unknown=%d info=%d good=%d failed=%s",
                      res.category, res.ms, res.cancelled, c["bad"], c["warn"], c["unknown"], c["info"], c["good"],
                      ",".join(res.failed) or "-")
        self.notifier.category_done.emit(res.category)
        self.notifier.changed.emit()

    def _on_run_done(self) -> None:
        self.state.running = False
        self.state.finished = datetime.fromtimestamp(self._now()).astimezone()
        self.state.progress_text = ""
        self._update_status()
        self.notifier.run_finished.emit()
        self.notifier.changed.emit()

    # ================================================================ 結果をコピー(FR-6)
    def secrets(self) -> Secrets:
        return Secrets(os.environ.get("USERPROFILE"), os.environ.get("USERNAME"), os.environ.get("COMPUTERNAME"),
                       tuple(sorted(self._ssids)))

    def copy_text(self) -> str:
        st = self.state
        sections = [(c, st.results.get(c, []), st.ms.get(c)) for c in st.categories if st.results.get(c)]
        when = st.started or datetime.fromtimestamp(self._now()).astimezone()
        return build(sections, when, self.secrets(), st.worse)

    # ================================================================ 開く操作(FR-10)
    def open_action(self, a: Action) -> bool:
        """action のボタン。開いてよいものだけ os.startfile で開く。category は診断を始める。"""
        if a.kind == "category" and a.target in BY_CATEGORY:
            return self.run([a.target])
        if a.kind == "uri" and a.target in ALLOWED_URIS:
            return self._open(a.target)
        if a.kind == "taskmgr":
            return self._open(TASKMGR)
        if a.kind == "folder" and a.target:
            return self.open_folder(a.target)
        self.log.warning("action refused kind=%s", a.kind)
        return False

    def open_folder(self, path: str) -> bool:
        if not os.path.isdir(path):
            return False
        return self._open(path)

    def _open(self, target: str) -> bool:
        try:
            self._startfile(target)
            return True
        except OSError as e:
            self.log.warning("open failed: %s", type(e).__name__)
            return False

    # ================================================================ 古い一時ファイル(FR-9)
    @property
    def cleaning(self) -> bool:
        return self._cleanup_thread is not None and self._cleanup_thread.is_alive()

    def _spawn(self, target: Callable[[], None], name: str) -> None:
        if self._threaded:
            t = threading.Thread(target=target, name=name, daemon=True)
            self._cleanup_thread = t
            t.start()
        else:
            target()

    def scan_temp(self, done: Callable[[cleanup.TempScan | None], None]) -> None:
        """一覧を作る(GUI スレッドの外)。done はメインスレッドで呼ぶ。読めなければ None。"""
        self._cleanup_stop.clear()

        def work() -> None:
            scan: cleanup.TempScan | None
            try:
                deadline = time.monotonic() + cleanup.SCAN_LIMIT_S
                scan = cleanup.scan_old_temp(cleanup.temp_root(), self._now(),
                                             lambda: self._cleanup_stop.is_set() or time.monotonic() >= deadline)
                self.log.info("temp scan files=%d bytes=%d partial=%s denied=%s", scan.count, scan.bytes, scan.partial,
                              scan.denied)
            except OSError as e:
                self.log.warning("temp scan failed: %s", type(e).__name__)
                scan = None
            self._post(partial(done, scan))

        self._spawn(work, "pccheckup-scan")

    def recycle_temp(self, scan: cleanup.TempScan, parent_hwnd: int | None,
                     done: Callable[[cleanup.CleanupResult], None]) -> None:
        """承認済みの一覧をごみ箱へ送る(GUI スレッドの外)。ops.jsonl には数だけを書く。"""
        self._cleanup_stop.clear()

        def work() -> None:
            try:
                res = cleanup.recycle_old_temp(scan, self._now(), self._recycle, parent_hwnd, self._cleanup_stop.is_set)
            except Exception as e:  # noqa: BLE001 - 送れなかったことを画面へ返す
                self.log.warning("recycle failed: %s", type(e).__name__)
                res = cleanup.CleanupResult(skipped=scan.count, aborted=True)
            try:
                self.ops.recycle_temp(res.sent, res.skipped, res.bytes)
            except OSError as e:
                self.log.warning("ops write failed: %s", type(e).__name__)
            self.log.info("recycle_temp sent=%d skipped=%d bytes=%d aborted=%s reasons=%s", res.sent, res.skipped,
                          res.bytes, res.aborted, ",".join(f"{k}:{v}" for k, v in sorted(res.reasons.items())) or "-")
            self._post(partial(done, res))

        self._spawn(work, "pccheckup-recycle")

    # ================================================================ 見張り(FR-11・FR-12)
    def _read_system_drive(self) -> DiskInfo:
        return self._probes_factory().system_drive()

    def _on_watch_alert(self, status: str, d: DiskInfo) -> None:
        pct = d.free / d.total * 100 if d.total > 0 else 0.0
        drive = d.root.rstrip("\\")
        head = "空きがほとんどありません" if status == "bad" else "空きが少なくなっています"
        self.ctx.notify("PcCheckup", f"{drive} ドライブの{head}(空き {fmt_bytes(d.free)}・{pct:.0f}%)。"
                        "クリックすると、空けられる場所を調べます。",
                        on_click=partial(self.open_and_run, "storage"), level="warn")

    def check_disk_now(self) -> str | None:
        """テスト・selftest 用: 見張りの1回分を今すぐ行う。"""
        return self.watcher.tick()

    def set_watch(self, on: bool) -> str | None:
        """設定を保存して見張りを切り替える。失敗したら理由の文を返す。"""
        sec = dict(self.ctx.settings_dict())
        sec.pop("enabled", None)
        sec["watch_disk"] = bool(on)
        try:
            self.ctx.write_settings(sec)
        except Exception as e:  # noqa: BLE001 - 設定ファイルが壊れているときなど
            return f"設定を保存できませんでした({type(e).__name__})"
        self.watch_disk = bool(on)
        self.watcher.stop()
        if on:
            self.watcher.start()
        self.log.info("watch_disk=%s", self.watch_disk)
        self._update_status()
        self.notifier.settings_changed.emit()
        return None

    # ================================================================ 状態表示・利用状況・診断
    def summary_text(self) -> str:
        st = self.state
        if st.running:
            return "診断中"
        if not st.done:
            return "見張り: オン" if self.watch_disk else "待機中"
        c = st.counts()
        parts = [f"対処が必要 {c['bad']}" if c["bad"] else "", f"注意 {c['warn']}" if c["warn"] else ""]
        text = "・".join(p for p in parts if p) or "問題は見つかりませんでした"
        return f"前回の診断: {text}"

    def _update_status(self) -> None:
        try:
            self.ctx.set_tray_status(self.summary_text())
        except Exception as e:  # noqa: BLE001 - 状態表示の失敗で診断を止めない
            self.log.warning("status update failed: %s", type(e).__name__)

    def usage(self, days: int) -> list[UsageSeries]:
        diag = count_jsonl(self.history.path, days, lambda r: r.get("category") in CATEGORIES)
        rec = count_jsonl(self.ops.path, days, lambda r: r.get("op") == "recycle_temp")
        return [
            UsageSeries("diagnoses", "診断(カテゴリごと)", diag, "回", primary=True,
                        hint="v0.3.0 の公開から 90 日で 0 回なら README の紹介から外す(R-2)"),
            UsageSeries("recycle_temp", "古い一時ファイルをごみ箱へ", rec, "回", good_when="neutral"),
        ]

    def diagnostics(self) -> dict[str, str | int | bool]:
        out: dict[str, str | int | bool] = {"watch_disk": self.watch_disk, "running": self.state.running}
        st = self.state
        statuses: list[str]
        if st.done:
            cats = [c for c in st.categories if c in st.done]
            statuses = [f.status for c in cats for f in st.results.get(c, [])]
            unknown_ids = [f.check_id for c in cats for f in st.results.get(c, []) if f.status == "unknown"]
        else:
            rows = self.history.entries()
            if not rows:
                out["last_category"] = "none"
                return out
            last = rows[-1]
            cats = [str(last["category"])]
            res = last["results"]
            statuses = [str(v) for v in res.values()]
            unknown_ids = [str(k) for k, v in res.items() if v == "unknown"]
        out["last_category"] = ",".join(cats)
        for s in STATUS_ORDER:
            out[f"last_{s}"] = sum(1 for x in statuses if x == s)
        out["unknown_checks"] = ",".join(i for i in unknown_ids if i in ALL_IDS) or "none"
        return out
