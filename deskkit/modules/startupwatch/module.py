# StartupWatch モジュール本体。見張りのスレッド(watcher.py)が読んだ8か所の一覧を GUI スレッドで受け、記録(known.json)と
# 比べて、増えた物があれば件数だけを知らせる(FR-1〜FR-5)。「確かめた」・動いているかの印・Windows の画面を開く操作・CLI・
# usage・diagnostics を持つ。何も書き換えない。ログ・記録・diagnostics には場所の記号と件数だけを書く(INV-4)。
from __future__ import annotations

import logging
import ntpath
import os
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Signal

from deskkit.modules.startupwatch import sources
from deskkit.modules.startupwatch.cmdline import exe_name, norm_program
from deskkit.modules.startupwatch.diff import (
    FLAG_CHANGED,
    FLAG_KNOWN,
    FLAG_NEW,
    Diff,
    baseline,
    compare,
    item_key,
    next_record,
)
from deskkit.modules.startupwatch.sources import LOCATIONS, ScanResult, StartupItem
from deskkit.modules.startupwatch.store import EventLog, KnownStore, now_iso
from deskkit.modules.startupwatch.watcher import MODE_POLL, MODE_UNREADABLE, Watcher
from deskkit.usage import UsageSeries, count_jsonl

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

    from deskkit.modules.startupwatch._win32 import Api

DEFAULTS: dict[str, Any] = {"notify_runonce": False, "show_running": True, "poll_minutes": 15, "settle_ms": 2000}
RANGES: dict[str, tuple[int, int]] = {"poll_minutes": (5, 240), "settle_ms": (500, 10000)}
NOTIFY_TITLE = "自動で起動する物が増えました"
SETTINGS_URI = "ms-settings:startupapps"
TASKMGR = "taskmgr.exe"
TRAY_OPEN = "自動起動の一覧を見る"


class Notifier(QObject):
    changed = Signal()


@dataclass(frozen=True)
class ViewItem:
    item: StartupItem
    key: str
    flag: str
    first_seen: str
    running: bool | None   # None は「分からない・見ていない」(W-13: 合わなくても「動いていない」とは書かない)

    @property
    def marks(self) -> list[str]:
        out: list[str] = []
        if self.flag == FLAG_NEW:
            out.append("新しい")
        elif self.flag == FLAG_CHANGED:
            out.append("中身が変わりました")
        if self.item.runonce:
            out.append("1回だけ")
        if self.item.is_deskkit:
            out.append("DeskKit")
        return out


def validate_detail(section: Mapping[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """§9: 合わない値・無いキーは既定値にする。(直した設定, 値が合わなかったキー, 無かったキー)。
    v0.4.1: 無いキー(初回など)は警告しないので、値が合わなかったキーと分けて返す。"""
    out = {k: v for k, v in section.items() if k != "enabled"}
    fixed: list[str] = []
    filled: list[str] = []
    for k, dv in DEFAULTS.items():
        if k not in out:
            out[k] = dv
            filled.append(k)
            continue
        v = out[k]
        if isinstance(dv, bool):
            ok = isinstance(v, bool)
        else:
            lo, hi = RANGES[k]
            ok = isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi
        if not ok:
            out[k] = dv
            fixed.append(k)
    return out, fixed, filled


def validate(section: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """§9: 合わない値・無いキーは既定値に戻す。(直した設定, 直したキー(無かったキーも含む))。"""
    out, fixed, filled = validate_detail(section)
    return out, [k for k in DEFAULTS if k in fixed or k in filled]


def deskkit_programs(executable: str | None = None) -> set[str]:
    """W-10: DeskKit を起動するプログラム。ソース実行では同じフォルダの pythonw.exe / python.exe も含める。"""
    exe = executable or sys.executable
    out = {norm_program(exe)}
    base = ntpath.basename(exe).lower()
    if base in ("python.exe", "pythonw.exe"):
        d = ntpath.dirname(exe)
        out.add(norm_program(ntpath.join(d, "pythonw.exe")))
        out.add(norm_program(ntpath.join(d, "python.exe")))
    return out


def _one_loc(locs: Iterable[str]) -> str | None:
    s = set(locs)
    return next(iter(s)) if len(s) == 1 else None


def _default_startfile(target: str) -> None:
    os.startfile(target)  # type: ignore[attr-defined,unused-ignore]  # 既定の動詞(開く)だけ


def _default_api() -> Api:
    from deskkit.modules.startupwatch._win32 import RealApi

    return RealApi()


class StartupWatchModule:
    def __init__(self, ctx: Any, *, api: Api | None = None, threaded: bool = True,
                 clock: Callable[[], float] = time.monotonic, startfile: Callable[[str], None] | None = None,
                 executable: str | None = None) -> None:
        self.ctx = ctx
        self.log: logging.Logger = ctx.log
        section, fixed, filled = validate_detail(dict(ctx.settings_dict()))
        if fixed or filled:
            for k in fixed:  # 警告は値が合わなかったキーだけ。無いキー(初回)は黙って既定値を入れる
                self.log.warning("settings_fixed key=%s", k)
            try:
                ctx.write_settings(section)
            except Exception as e:  # noqa: BLE001 - 書き戻せなくても既定値で動く
                self.log.warning("settings write-back failed: %s", type(e).__name__)
        self.cfg = section
        self.api: Api = api or _default_api()
        self._threaded = threaded
        self._startfile = startfile or _default_startfile
        self.programs = deskkit_programs(executable)
        self.known = KnownStore(ctx.data_dir / "known.json")
        self.events = EventLog(ctx.data_dir / "events.jsonl")
        self.notifier = Notifier()
        self.record: dict[str, Any] | None = None
        self.last_scan: ScanResult | None = None
        self.banner: tuple[str, int] | None = None     # ("baseline" | "rebaseline", 件数)
        self.running_names: set[str] | None = None
        self.scans = 0
        self._missed = False
        self._stopped = False
        self.watcher = Watcher(self.api, settle_ms=int(section["settle_ms"]), poll_minutes=int(section["poll_minutes"]),
                               scan=self._scan_from_watcher, log=self.log, clock=clock, on_state=self._on_watcher_state)

    # ================================================================ ライフサイクル
    def start(self) -> None:
        self._stopped = False
        on = getattr(self.ctx, "on", None)
        if on is not None:
            on("host.snooze_changed", self._on_snooze_changed)
        add = getattr(self.ctx, "add_tray_action", None)
        if add is not None:
            add(TRAY_OPEN, self.open_page)
        self._update_status()
        self.log.info("startupwatch start")
        self.watcher.start(threaded=self._threaded)

    def stop(self) -> None:
        self._stopped = True
        self.watcher.stop()
        self.log.info("startupwatch stop")

    def handle_cli(self, args: list[str]) -> tuple[int, str]:
        cmd = args[0] if args else ""
        if cmd in ("list", "rescan") and len(args) == 1:
            res = sources.read_all(self.api, self.programs)
            if res is None:
                return 1, "読めませんでした"
            if cmd == "list":
                return 0, "\n".join(f"{it.loc}\t{it.name}\t{it.command}" for it in res.items())
            d = self.apply(res, "cli", manual=True) or Diff()
            return 0, f"増えた {len(d.added)}・変わった {len(d.changed)}・消えた {len(d.removed)}"
        return 2, "使い方: startupwatch list | startupwatch rescan"

    def create_page(self) -> QWidget:
        from deskkit.modules.startupwatch.page import StartupWatchPage

        return StartupWatchPage(self)

    # ================================================================ 読み取り
    def _post(self, fn: Callable[[], None]) -> None:
        if self._threaded:
            self.ctx.call_soon(fn)
        else:
            fn()

    def snoozed(self) -> bool:
        try:
            return bool(self.ctx.is_snoozed())
        except Exception:  # noqa: BLE001
            return False

    def _scan_from_watcher(self, reason: str) -> None:
        """見張りのスレッドから呼ばれる。一時停止中は読まない(W-11。手で押した読み直しは別)。"""
        manual = reason == "manual"
        if not manual and self.snoozed():
            self._missed = True
            self._post(self._update_status)
            return
        try:
            res = sources.read_all(self.api, self.programs, cancelled=lambda: self._stopped or self.watcher.stopping)
        except Exception as e:  # noqa: BLE001 - 読めなくても見張りは続ける
            self.log.warning("scan failed: %s", type(e).__name__)
            return
        if res is None:
            return  # §10: 読み直しの途中で stop()。記録は書かない
        self._post(lambda: self._apply_quiet(res, reason, manual))

    def _apply_quiet(self, res: ScanResult, reason: str, manual: bool) -> None:
        self.apply(res, reason, manual=manual)

    def request_scan(self, reason: str = "manual") -> None:
        """読み直しを頼む。見張りのスレッドが動いていればそこで読み、いなければここで読む。"""
        t = self.watcher._thread  # noqa: SLF001 - 自分の部品
        if self._threaded and t is not None and t.is_alive():
            self.watcher.kick()  # 見張りのスレッドが「読み直す」として読む
            return
        self._scan_from_watcher(reason)

    def rescan(self) -> None:
        """Hero の「読み直す」(FR-9): 8か所を読み直し、動いているかの印も取り直す。"""
        self.request_scan("manual")
        self.request_running()

    def _flag_for_added(self, it: StartupItem) -> str:
        if it.is_deskkit:
            return FLAG_KNOWN  # W-10
        if it.runonce and not self.cfg["notify_runonce"]:
            return FLAG_KNOWN  # W-9
        return FLAG_NEW

    def apply(self, res: ScanResult, reason: str, *, manual: bool = False) -> Diff | None:
        """GUI スレッドで: 記録と比べ、記録を書き、増えた物があれば1回だけ知らせる。"""
        if self._stopped:
            return None
        if not manual and self.snoozed():
            self._missed = True
            self._update_status()
            return None
        self.scans += 1
        self.last_scan = res
        items = res.items()
        record, broken = self.known.load()
        now = now_iso()
        diff = Diff()
        if record is None:
            rec_items = baseline(items, now)
            self.record = {"version": 1, "baseline_at": now, "items": rec_items}
            self.known.save(self.record)
            kind = "rebaseline" if broken else "baseline"
            self.events.append(kind, None, len(rec_items))
            self.banner = (kind, len(rec_items))
            self.log.info("%s n=%d", kind, len(rec_items))
        else:
            diff = compare(record["items"], items)
            rec_items = next_record(record["items"], items, now, self._flag_for_added)
            self.record = {"version": 1, "baseline_at": record.get("baseline_at") or now, "items": rec_items}
            counted = [it for it in diff.added if self._flag_for_added(it) == FLAG_NEW]
            if not diff.empty:
                self.known.save(self.record)
                if diff.added:
                    self.events.append("added", _one_loc(it.loc for it in diff.added), len(diff.added), bool(counted))
                if diff.changed:
                    self.events.append("changed", _one_loc(it.loc for it in diff.changed), len(diff.changed))
                if diff.removed:
                    self.events.append("removed", _one_loc(loc for _k, loc in diff.removed), len(diff.removed))
                self.log.info("diff reason=%s added=%d changed=%d removed=%d notified=%d", reason, len(diff.added),
                              len(diff.changed), len(diff.removed), len(counted))
            if counted:
                self.ctx.notify(NOTIFY_TITLE, f"{len(counted)} 個増えました。押すと、何が増えたかを確かめられます。",
                                self.open_page, level="info")
        self._missed = False
        self._update_status()
        self.notifier.changed.emit()
        return diff

    # ================================================================ 動いているかの印(W-13・FR-13)
    def request_running(self) -> None:
        if not self.cfg["show_running"]:
            self.running_names = None
            return

        def work() -> None:
            try:
                names = self.api.process_names()
            except Exception as e:  # noqa: BLE001
                self.log.warning("process names failed: %s", type(e).__name__)
                names = None
            self._post(lambda: self._set_running(names))

        if self._threaded:
            threading.Thread(target=work, name="startupwatch-running", daemon=True).start()
        else:
            work()

    def _set_running(self, names: set[str] | None) -> None:
        if self._stopped:
            return
        self.running_names = names
        self.notifier.changed.emit()

    def is_running(self, it: StartupItem) -> bool | None:
        names = self.running_names
        if names is None or not self.cfg["show_running"] or not it.text_value or it.command == sources.LNK_UNKNOWN_TEXT:
            return None
        cmd = f'"{it.command}"' if it.folder_path else it.command
        exe = exe_name(cmd, self.api.expand).lower()
        return True if exe and exe in names else None

    # ================================================================ 画面に出す物
    def view_items(self) -> list[ViewItem]:
        res = self.last_scan
        rec = (self.record or {}).get("items", {})
        if res is None:
            return []
        out: list[ViewItem] = []
        for it in res.items():
            k = item_key(it.loc, it.name)
            e = rec.get(k, {})
            out.append(ViewItem(it, k, str(e.get("flag") or FLAG_KNOWN), str(e.get("first_seen") or ""), self.is_running(it)))
        order = {loc.key: i for i, loc in enumerate(LOCATIONS)}
        out.sort(key=lambda v: (0 if v.marks else 1, order.get(v.item.loc, 99), v.item.name.lower()))
        return out

    def new_items(self) -> list[ViewItem]:
        return [v for v in self.view_items() if v.flag in (FLAG_NEW, FLAG_CHANGED)]

    def new_count(self) -> int:
        return sum(1 for e in (self.record or {}).get("items", {}).values() if e.get("flag") == FLAG_NEW)

    def location_mode(self, key: str) -> str:
        r = self.last_scan.locs.get(key) if self.last_scan else None
        if r is not None and r.status == "unreadable":
            return MODE_UNREADABLE
        return self.watcher.modes.get(key, MODE_POLL)

    # ================================================================ 操作
    def ack(self, key: str) -> None:
        """「確かめた」(FR-7): 印を消し、普通の一覧へ移す。"""
        self._ack({key})

    def ack_all(self) -> None:
        self._ack({v.key for v in self.new_items()})

    def _ack(self, keys: set[str]) -> None:
        if self.record is None:
            return
        n = 0
        for k in keys:
            e = self.record["items"].get(k)
            if e is not None and e.get("flag") in (FLAG_NEW, FLAG_CHANGED):
                e["flag"] = FLAG_KNOWN
                n += 1
        if n:
            self.known.save(self.record)
            self.events.append("ack", None, n)
            self.log.info("ack n=%d", n)
        self._update_status()
        self.notifier.changed.emit()

    def open_page(self) -> None:
        show = getattr(self.ctx, "show_page", None)
        if show is not None:
            show()

    def page_opened(self) -> None:
        """画面を開いた(FR-13・FR-19)。"""
        try:
            self.events.append("open", None, 0)
        except OSError as e:
            self.log.warning("events write failed: %s", type(e).__name__)
        self.request_running()

    def open_settings(self) -> None:
        self._startfile(SETTINGS_URI)

    def open_taskmgr(self) -> None:
        self._startfile(TASKMGR)

    def open_folder(self, it: StartupItem) -> None:
        if it.folder_path and it.loc in ("startup_user", "startup_common"):
            self._startfile(it.folder_path)

    def copy_text(self, it: StartupItem) -> str:
        """「コマンドをコピー」(FR-6): 利用者のフォルダを %USERPROFILE% に置き換える(V-8)。大文字小文字は問わない。"""
        home = (os.environ.get("USERPROFILE") or "").rstrip("\\")
        text = it.command
        if len(home) <= 3:
            return text
        low, h = text.lower(), home.lower()
        parts: list[str] = []
        i = 0
        while True:
            j = low.find(h, i)
            if j < 0:
                parts.append(text[i:])
                return "".join(parts)
            parts.append(text[i:j] + "%USERPROFILE%")
            i = j + len(h)

    def set_option(self, key: str, value: Any) -> None:
        if key not in DEFAULTS:
            return
        sec, _ = validate({**self.cfg, key: value})
        self.cfg = sec
        try:
            self.ctx.write_settings(sec, restart=key in ("poll_minutes", "settle_ms"))
        except Exception as e:  # noqa: BLE001
            self.log.warning("settings write failed: %s", type(e).__name__)
        if key == "show_running":
            if value:
                self.request_running()
            else:
                self.running_names = None
        self.notifier.changed.emit()

    # ================================================================ 状態
    def _on_snooze_changed(self, payload: Mapping[str, Any]) -> None:
        if not payload.get("snoozed") and not self.snoozed():
            self.request_scan("resume")  # W-11: 解除されたら1回読み直す
        self._update_status()
        self.notifier.changed.emit()

    def _on_watcher_state(self) -> None:
        self._post(self.notifier.changed.emit)

    def status_text(self) -> str:
        if self.snoozed():
            return "一時停止中"
        n = self.new_count()
        return f"新しい物 {n} 件" if n else "見張っています"

    def _update_status(self) -> None:
        if self._stopped:
            return
        setter = getattr(self.ctx, "set_tray_status", None)
        if setter is not None:
            setter(self.status_text())

    # ================================================================ usage / diagnostics
    def usage(self, days: int) -> list[UsageSeries]:
        p = self.ctx.data_dir / "events.jsonl"
        notified = count_jsonl(p, days, lambda r: r.get("event") == "added" and bool(r.get("notified")))
        opened = count_jsonl(p, days, lambda r: r.get("event") == "open")
        hint = "90 日で知らせた数と画面を開いた回数がどちらも 0 なら、README の紹介から外す(R-3)"
        return [UsageSeries("notified", "知らせた数", notified, primary=True, hint=hint, good_when="neutral"),
                UsageSeries("opened", "画面を開いた回数", opened, good_when="neutral")]

    def diagnostics(self) -> dict[str, str | int | bool]:
        items = (self.record or {}).get("items", {})
        out: dict[str, str | int | bool] = {
            "record": self.known.exists(),
            "known": len(items),
            "new": self.new_count(),
            "changed": sum(1 for e in items.values() if e.get("flag") == FLAG_CHANGED),
            "last_scan_ms": self.last_scan.ms if self.last_scan else -1,
            "watch_degraded": self.watcher.degraded,
        }
        for loc in LOCATIONS:
            out[f"watch.{loc.key}"] = self.location_mode(loc.key)
            r = self.last_scan.locs.get(loc.key) if self.last_scan else None
            out[f"count.{loc.key}"] = len(r.items) if r is not None else -1
        return out
