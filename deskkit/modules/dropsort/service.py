# DropSort の本体(Qt に依存しない): スキャン → 完了判定 → 危険名 → MOTW → ルール → 試運転記録 / 移動 → 操作ログ。
# sort-existing・archive-now・undo・状態表示もここにあり、host のモジュールと単独 CLI の両方から使う。
# ファイルを動かす処理と state.json の更新は、必ず op.lock(プロセス間)とプロセス内ミューテックスを持って行う。
from __future__ import annotations

import logging
import ntpath
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from deskkit.modules.dropsort import archive as arch
from deskkit.modules.dropsort import guard
from deskkit.modules.dropsort import motw as motw_mod
from deskkit.modules.dropsort._win32 import ERROR_SUCCESS, FileInfo, Win32Api
from deskkit.modules.dropsort.completion import LOCKED, LOCKED_GIVEUP, PENDING, CompletionTracker
from deskkit.modules.dropsort.config import Config, RuleDef
from deskkit.modules.dropsort.motw import Motw
from deskkit.modules.dropsort.mover import Mover, Outcome
from deskkit.modules.dropsort.oplog import JsonlLog, LockBusyError, OpLock, iso
from deskkit.modules.dropsort.paths import DestCheck, check_dest, resolve_downloads
from deskkit.modules.dropsort.rules import first_match
from deskkit.modules.dropsort.scanner import scan_dir
from deskkit.modules.dropsort.state import DirState, StateStore

REASON_TEXT = {
    "motw_unsupported_fs": "MOTW 付きのファイルを代替データストリーム非対応のドライブへは移せません",
    "network_dest": "移動先がネットワーク上です",
    "double_extension": "二重拡張子(偽装の可能性)",
    "bidi_control_char": "双方向制御文字を含む名前",
    "spaced_extension": "拡張子の直前に連続した空白",
    "reparse_point": "リパースポイント(リンク)",
    "suffix_exhausted": "連番の上限に達しました",
    "verify_mismatch": "コピーの検証に失敗しました",
    "undo_changed": "移動後に変更されているため戻しません",
    "locked": "他のプログラムが使用中です",
    "dest_unavailable": "移動先を使えません(未接続)",
    "dest_missing": "移動先フォルダがありません",
    "dest_is_downloads": "移動先がダウンロードフォルダ自身です",
    "dest_in_archive": "移動先がアーカイブの中です",
    "source_delete_failed": "コピー後に元ファイルを削除できませんでした(両方に残っています)",
    "source_changed": "判定後にファイルが変わりました",
    "path_too_long": "パスが長すぎます",
    "access_denied": "アクセスが拒否されました",
    "move_error": "移動に失敗しました",
    "baseline": "基準線(sort-existing で扱います)",
    "not_stable": "ダウンロード中か、更新されたばかりです",
    "invalid_rule": "ルールが無効です",
}

OP_TEXT = {
    "move": "移動", "archive": "アーカイブ", "refused": "拒否", "flagged": "要確認", "undo": "元に戻した",
    "failed": "失敗", "would_move": "移動予定", "would_archive": "アーカイブ予定", "would_refuse": "拒否予定",
    "skip": "見送り", "no_match": "対象外",
}


def reason_text(code: str | None) -> str:
    if not code:
        return ""
    return REASON_TEXT.get(code, code)


@dataclass
class CycleResult:
    ok: bool
    skipped: str | None = None        # unresolved / locked / error
    downloads: str | None = None
    baseline_created: int | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    pending: int = 0
    next_check_s: float | None = None
    scan_ms: float = 0.0
    rule_problems_changed: bool = False


@dataclass
class BatchResult:
    items: list[dict[str, Any]] = field(default_factory=list)
    downloads: str | None = None
    error: str | None = None

    def count(self, *ops: str) -> int:
        return sum(1 for i in self.items if i.get("op") in ops)

    @property
    def partial_failure(self) -> bool:
        return self.count("refused", "failed", "would_refuse") > 0


@dataclass
class UndoResult:
    restored: list[dict[str, Any]] = field(default_factory=list)
    problems: list[dict[str, Any]] = field(default_factory=list)
    nothing: bool = False


class DropSortService:
    def __init__(self, api: Win32Api, data_dir: Path, cfg: Config, *, clock: Callable[[], float] = time.time,
                 log: logging.Logger | None = None) -> None:
        self.api = api
        self.data_dir = Path(data_dir)
        self.cfg = cfg
        self.clock = clock
        self.log = log or logging.getLogger("deskkit.dropsort")
        self.oplog = JsonlLog(self.data_dir / "oplog.jsonl")
        self.dryrun = JsonlLog(self.data_dir / "dryrun.jsonl", "d")
        self.store = StateStore(self.data_dir / "state.json")
        self.lock = OpLock(self.data_dir / "op.lock")
        self.tracker = CompletionTracker()
        self.mover = Mover(api, cfg.max_suffix, self.log)
        self._mu = threading.Lock()
        self._snap_mu = threading.Lock()
        self.downloads: str | None = None
        self.rule_checks: dict[int, DestCheck] = {}
        self._problem_sig: tuple[tuple[int, str], ...] = ()
        self._today_cache: tuple[tuple[Any, ...], dict[str, int]] | None = None
        self.snapshot: dict[str, Any] = {"pending": 0, "flagged": 0, "dryrun_present": 0, "last_scan": None,
                                         "scan_ms": 0.0, "files": 0}

    # ------------------------------------------------------------ 共通
    def set_config(self, cfg: Config) -> None:
        self.cfg = cfg
        self.mover.max_suffix = cfg.max_suffix

    @contextmanager
    def exclusive(self, timeout: float = 0.0) -> Iterator[None]:
        got = self._mu.acquire(timeout=timeout) if timeout > 0 else self._mu.acquire(blocking=False)
        if not got:
            raise LockBusyError("DropSort が別の処理を実行中です")
        try:
            with self.lock.hold(timeout):
                yield
        finally:
            self._mu.release()

    def resolve(self) -> str | None:
        self.downloads = resolve_downloads(self.api, self.cfg.downloads_dir_override)
        return self.downloads

    def validate_rules(self, downloads: str | None) -> list[tuple[RuleDef, str]]:
        """全ルールを検査し、無効(と一時的に使えない)ルールの (ルール, 理由文) を返す。"""
        problems: list[tuple[RuleDef, str]] = []
        checks: dict[int, DestCheck] = {}
        for r in self.cfg.rules:
            if r.error is not None:
                checks[r.index] = DestCheck(False, "invalid_rule", r.error)
                problems.append((r, r.error))
                continue
            c = check_dest(self.api, r.dest, downloads, self.cfg.archive.dir_name)
            checks[r.index] = c
            if not c.ok:
                problems.append((r, c.message or reason_text(c.code)))
        self.rule_checks = checks
        return problems

    def disabled_rules(self) -> set[int]:
        return {i for i, c in self.rule_checks.items() if not c.ok and not c.unavailable}

    def _base_rec(self, f: FileInfo, m: Motw, rule: RuleDef | None, source: str) -> dict[str, Any]:
        rec: dict[str, Any] = {"src": f.path, "size": f.size, "mtime": iso(f.mtime), "mtime_epoch": f.mtime,
                               "rule": rule.name if rule else None, "motw": m.present, "zone_id": m.zone_id,
                               "source": source}
        if rule is not None and rule.match.host_domain and m.domain:
            rec["domain"] = m.domain  # FR-7 有効時のみドメイン名(URL は書かない)
        return rec

    def _flag(self, ds: DirState, f: FileInfo, reason: str, source: str, events: list[dict[str, Any]] | None,
              log_it: bool = True) -> dict[str, Any]:
        rec = {"op": "flagged", "src": f.path, "size": f.size, "mtime": iso(f.mtime), "mtime_epoch": f.mtime,
               "reason": reason, "source": source}
        if log_it:
            rec = self.oplog.append(rec, self.clock())
            ds.set_handled(f.name, f.size, f.mtime, "flagged", reason=reason, ts=self.clock())
            self.tracker.forget(f.name)
        if events is not None:
            events.append(dict(rec, name=f.name))
        return rec

    # ------------------------------------------------------------ 判定 → 記録 / 実行
    def _decide(self, ds: DirState | None, f: FileInfo, m: Motw, dest_dir: str, rule: RuleDef | None, *,
                kind: str, apply: bool, source: str, events: list[dict[str, Any]] | None, dedupe: bool,
                log_dry: bool = True) -> dict[str, Any]:
        """kind は move / archive。戻り値は表示用の1件。"""
        rec = self._base_rec(f, m, rule, source)
        prev = ds.handled_for(f.name, f.size, f.mtime) if ds is not None else None
        if not apply:
            plan_dir = dest_dir
            if kind == "archive" and not self.api.is_dir(dest_dir):
                plan_dir = ntpath.dirname(ntpath.dirname(dest_dir))  # まだ無い月フォルダ: ダウンロード自身で FS を判定
            p = self.mover.plan(f.path, plan_dir, m)
            op = ("would_archive" if kind == "archive" else "would_move") if p.ok else "would_refuse"
            dst = ntpath.join(dest_dir, ntpath.basename(p.dst)) if (p.ok and p.dst) else dest_dir
            rec.update(op=op, dst=dst, dst_fs=p.dst_fs, reason=p.reason)
            sig = f"{op}|{rec['rule']}|{dest_dir}|{p.reason}"
            if ds is not None:
                ds.set_handled(f.name, f.size, f.mtime, "decision", sig=sig, op=op, rule=rec["rule"], dst=dst)
            if log_dry and (not dedupe or prev is None or prev.get("sig") != sig):
                rec = self.dryrun.append(rec, self.clock())
                if events is not None:
                    events.append(dict(rec, name=f.name))
            return dict(rec, name=f.name)
        if kind == "archive" and not arch.ensure_dir(self.api, dest_dir):
            out = Outcome("failed", None, "move_error")
        else:
            out = self.mover.move(f.path, dest_dir, m, f.size, f.mtime)
        if out.kind == "skipped":
            return dict(rec, op="skip", reason=out.reason, name=f.name)
        if out.kind == "moved":
            rec.update(op=kind, dst=out.dst, dst_fs=out.dst_fs)
            rec = self.oplog.append(rec, self.clock())
            if ds is not None:
                ds.handled.pop(f.name.lower(), None)
                ds.baseline.pop(f.name.lower(), None)
                ds.first_seen.pop(f.name.lower(), None)
            self.tracker.forget(f.name)
            self.log.info("%s: %s → %s (rule=%s)", kind, f.path, out.dst, rec.get("rule"))
            if events is not None:
                events.append(dict(rec, name=f.name))
            return dict(rec, name=f.name)
        op = out.kind  # refused / failed
        rec.update(op=op, dst=out.dst or dest_dir, dst_fs=out.dst_fs, reason=out.reason)
        if out.win_error:
            rec["win_error"] = out.win_error
        sig = f"{op}|{rec['rule']}|{dest_dir}|{out.reason}"
        if ds is not None:
            ds.set_handled(f.name, f.size, f.mtime, "decision", sig=sig, op=op, rule=rec["rule"], dst=rec["dst"])
        if (not dedupe or prev is None or prev.get("sig") != sig) and out.reason != "dest_unavailable":
            rec = self.oplog.append(rec, self.clock())
            self.log.warning("%s: %s (%s)", op, f.path, out.reason)
            if events is not None:
                events.append(dict(rec, name=f.name))
        return dict(rec, name=f.name)

    def _dest_for(self, rule: RuleDef) -> str:
        c = self.rule_checks.get(rule.index)
        return (c.final if c and c.final else rule.dest)

    # ------------------------------------------------------------ 自動処理(1回のフルスキャン)
    def run_cycle(self) -> CycleResult:
        now = self.clock()
        dl = self.resolve()
        if dl is None:
            return CycleResult(False, "unresolved")
        try:
            snap = scan_dir(self.api, dl)
        except OSError as e:
            self.log.warning("ダウンロードフォルダを列挙できません: %s", e)
            return CycleResult(False, "unresolved")
        problems = self.validate_rules(dl)
        sig = tuple(sorted((r.index, msg) for r, msg in problems))
        changed = sig != self._problem_sig
        self._problem_sig = sig
        res = CycleResult(True, downloads=dl, scan_ms=snap.elapsed_ms, rule_problems_changed=changed)
        try:
            with self.exclusive(0):
                self._cycle_locked(snap, now, res)
        except LockBusyError:
            res.ok = False
            res.skipped = "locked"
        return res

    def _cycle_locked(self, snap: Any, now: float, res: CycleResult) -> None:
        cfg = self.cfg
        st = self.store.load()
        ds = st.dir(snap.path)
        if ds is None:
            ds = st.new_dir(snap.path)
            ds.created = now
            for f in snap.files:
                ds.baseline[f.name.lower()] = {"name": f.name, "size": f.size, "mtime": f.mtime}
            self.store.save(st)
            res.baseline_created = len(snap.files)
            self.log.info("基準線を記録しました: %d 件 (%s)", len(snap.files), snap.path)
            self._update_snapshot(ds, 0, now, res.scan_ms, len(snap.files))
            return
        present = snap.names()
        ds.prune(present)
        self.tracker.prune(present)
        disabled = self.disabled_rules()
        pending = 0
        next_check: float | None = None
        events = res.events
        for f in snap.files:
            key = f.name.lower()
            if ds.in_baseline(f.name, f.size, f.mtime):
                continue
            ds.baseline.pop(key, None)  # 同じ名前でも中身が変わったものは基準線から外す
            ds.first_seen.setdefault(key, now)
            h = ds.handled_for(f.name, f.size, f.mtime)
            if h is not None and h.get("status") in ("flagged", "excluded"):
                continue
            if f.is_reparse:
                self._flag(ds, f, "reparse_point", "auto", events)
                continue
            if CompletionTracker.is_temp(f.name, cfg.temp_extensions):
                pending += 1
                continue
            stab, remain = self.tracker.stability(f, now, cfg.stable_seconds)
            if stab == PENDING:
                pending += 1
                next_check = remain if next_check is None else min(next_check, remain)
                continue
            g = guard.check_name(f.name, cfg.exec_extensions)
            if g is not None:
                self._flag(ds, f, g, "auto", events)
                continue
            ex = self.tracker.exclusive(self.api, f, cfg.locked_retry_max)
            if ex == LOCKED:
                pending += 1
                retry = max(1.0, min(cfg.stable_seconds, 5.0))
                next_check = retry if next_check is None else min(next_check, retry)
                continue
            if ex == LOCKED_GIVEUP:
                self._flag(ds, f, "locked", "auto", events)
                continue
            m = motw_mod.read(self.api, f.path)
            rule = first_match(cfg.rules, f.name, f.size, m, disabled)
            if rule is not None:
                c = self.rule_checks.get(rule.index)
                if c is not None and c.unavailable:
                    continue  # 移動先が未接続: 今回は見送り、次回再評価
                r = self._decide(ds, f, m, self._dest_for(rule), rule, kind="move", apply=rule.apply,
                                 source="auto", events=events, dedupe=True)
                if r.get("op") == "move":
                    continue
            if cfg.archive.enabled and arch.is_idle(f, ds.first_seen.get(key), cfg.archive, now):
                touched = arch.last_touched(f, ds.first_seen.get(key), cfg.archive.use_atime)
                dest = arch.month_dir(snap.path, cfg.archive, touched)
                self._decide(ds, f, m, dest, None, kind="archive", apply=cfg.archive.mode == "apply",
                             source="auto-archive", events=events, dedupe=True)
        self.store.save(st)
        res.pending = pending
        res.next_check_s = next_check
        self._update_snapshot(ds, pending, now, res.scan_ms, len(snap.files))

    def _update_snapshot(self, ds: DirState, pending: int, now: float, scan_ms: float, files: int) -> None:
        flagged = sum(1 for h in ds.handled.values() if h.get("status") == "flagged")
        dry = sum(1 for h in ds.handled.values() if h.get("op") in ("would_move", "would_archive"))
        with self._snap_mu:
            self.snapshot = {"pending": pending, "flagged": flagged, "dryrun_present": dry, "last_scan": now,
                             "scan_ms": scan_ms, "files": files, "baseline": len(ds.baseline)}

    def get_snapshot(self) -> dict[str, Any]:
        with self._snap_mu:
            return dict(self.snapshot)

    # ------------------------------------------------------------ sort-existing(FR-20)
    def sort_existing(self, apply: bool, *, timeout: float = 0.0) -> BatchResult:
        """基準線のファイルにルールを当てる。既定は結果の表示だけ(dryrun.jsonl にも書かない)。
        ルールに当たらないファイルは、アーカイブが有効で idle_days を過ぎていればアーカイブの対象にする。"""
        dl = self.resolve()
        if dl is None:
            return BatchResult(error="unresolved")
        snap = scan_dir(self.api, dl)
        self.validate_rules(dl)
        disabled = self.disabled_rules()
        now = self.clock()
        out = BatchResult(downloads=dl)
        with self.exclusive(timeout):
            st = self.store.load()
            ds = st.dir(dl)
            created = ds is None
            if ds is None:
                ds = st.new_dir(dl)
                ds.created = now
                for f in snap.files:
                    ds.baseline[f.name.lower()] = {"name": f.name, "size": f.size, "mtime": f.mtime}
            cfg = self.cfg
            for f in snap.files:
                if not ds.in_baseline(f.name, f.size, f.mtime):
                    continue
                base = {"name": f.name, "src": f.path, "size": f.size, "mtime": iso(f.mtime)}
                reason = guard.check(f.name, f.attrs, cfg.exec_extensions)
                if reason is not None:
                    if apply:
                        self._flag(ds, f, reason, "sort-existing", None)
                        ds.baseline.pop(f.name.lower(), None)
                    out.items.append(dict(base, op="flagged", reason=reason))
                    continue
                if CompletionTracker.is_temp(f.name, cfg.temp_extensions) or now - f.mtime < cfg.stable_seconds:
                    out.items.append(dict(base, op="skip", reason="not_stable"))
                    continue
                if self.api.try_exclusive_open(f.path) != ERROR_SUCCESS:
                    out.items.append(dict(base, op="skip", reason="locked"))
                    continue
                m = motw_mod.read(self.api, f.path)
                rule = first_match(cfg.rules, f.name, f.size, m, disabled)
                if rule is not None:
                    c = self.rule_checks.get(rule.index)
                    if c is not None and c.unavailable:
                        out.items.append(dict(base, op="skip", reason="dest_unavailable", rule=rule.name))
                        continue
                    r = self._decide(ds if apply else None, f, m, self._dest_for(rule), rule, kind="move", apply=apply,
                                     source="sort-existing", events=None, dedupe=False, log_dry=False)
                    r["rule_mode"] = rule.mode
                    out.items.append(r)
                    continue
                if cfg.archive.enabled and arch.is_idle(f, None, cfg.archive, now):
                    dest = arch.month_dir(dl, cfg.archive, arch.last_touched(f, None, cfg.archive.use_atime))
                    out.items.append(self._decide(ds if apply else None, f, m, dest, None, kind="archive", apply=apply,
                                                  source="sort-existing", events=None, dedupe=False, log_dry=False))
                    continue
                out.items.append(dict(base, op="no_match", motw=m.present, zone_id=m.zone_id))
            if apply or created:
                self.store.save(st)
        return out

    # ------------------------------------------------------------ archive-now(FR-19)
    def archive_now(self, apply: bool, *, timeout: float = 0.0) -> BatchResult:
        """idle_days を過ぎたファイルを今すぐ評価する。基準線のファイルは表示だけで動かさない(INV-10)。"""
        dl = self.resolve()
        if dl is None:
            return BatchResult(error="unresolved")
        snap = scan_dir(self.api, dl)
        now = self.clock()
        cfg = self.cfg
        out = BatchResult(downloads=dl)
        with self.exclusive(timeout):
            st = self.store.load()
            ds = st.dir(dl)
            for f in snap.files:
                key = f.name.lower()
                in_base = ds is None or ds.in_baseline(f.name, f.size, f.mtime)
                first = None if (ds is None or in_base) else ds.first_seen.get(key)
                if not arch.is_idle(f, first, cfg.archive, now):
                    continue
                touched = arch.last_touched(f, first, cfg.archive.use_atime)
                dest = arch.month_dir(dl, cfg.archive, touched)
                base = {"name": f.name, "src": f.path, "dst": ntpath.join(dest, f.name), "size": f.size,
                        "mtime": iso(f.mtime)}
                if ds is not None and not in_base:
                    h = ds.handled_for(f.name, f.size, f.mtime)
                    if h is not None and h.get("status") in ("flagged", "excluded"):
                        out.items.append(dict(base, op="skip", reason=h.get("reason") or "excluded"))
                        continue
                reason = guard.check(f.name, f.attrs, cfg.exec_extensions)
                if reason is not None:
                    out.items.append(dict(base, op="flagged", reason=reason))
                    continue
                if CompletionTracker.is_temp(f.name, cfg.temp_extensions):
                    continue
                if in_base:
                    out.items.append(dict(base, op="would_archive" if not apply else "skip", reason="baseline"))
                    continue
                if self.api.try_exclusive_open(f.path) != ERROR_SUCCESS:
                    out.items.append(dict(base, op="skip", reason="locked"))
                    continue
                m = motw_mod.read(self.api, f.path)
                out.items.append(self._decide(ds, f, m, dest, None, kind="archive", apply=apply, source="archive-now",
                                              events=None, dedupe=False))
            if ds is not None:
                self.store.save(st)
        return out

    # ------------------------------------------------------------ undo(FR-15 / FR-16)
    def undo(self, count: int, *, timeout: float = 0.0) -> UndoResult:
        res = UndoResult()
        with self.exclusive(timeout):
            st = self.store.load()
            ops = self.oplog.all()
            done = set(st.undone) | {str(r.get("undo_of")) for r in ops if r.get("op") == "undo" and r.get("undo_of")}
            skipped = set(st.undo_skipped)
            cands = [r for r in reversed(ops) if r.get("op") in ("move", "archive")
                     and r.get("id") not in done and r.get("id") not in skipped]
            if not cands:
                res.nothing = True
                return res
            for r in cands[: max(1, count)]:
                rid = str(r.get("id"))
                dst = str(r.get("dst") or "")
                src = str(r.get("src") or "")
                info = self.api.stat(dst) if dst else None
                exp_m = r.get("mtime_epoch")
                if (info is None or info.is_dir or info.size != r.get("size")
                        or (isinstance(exp_m, int | float) and abs(info.mtime - float(exp_m)) > 2.0)):
                    rec = self.oplog.append({"op": "failed", "src": dst, "dst": src, "reason": "undo_changed",
                                             "undo_of": rid, "rule": r.get("rule"), "source": "undo"}, self.clock())
                    st.undo_skipped.append(rid)
                    res.problems.append(dict(rec, name=ntpath.basename(dst)))
                    continue
                m = motw_mod.read(self.api, dst)
                out = self.mover.move(dst, ntpath.dirname(src), m, info.size, info.mtime, name=ntpath.basename(src))
                if out.kind != "moved" or out.dst is None:
                    rec = self.oplog.append({"op": "failed", "src": dst, "dst": src, "reason": out.reason,
                                             "undo_of": rid, "rule": r.get("rule"), "source": "undo",
                                             "win_error": out.win_error}, self.clock())
                    res.problems.append(dict(rec, name=ntpath.basename(dst)))
                    continue
                rec = self.oplog.append({"op": "undo", "src": dst, "dst": out.dst, "size": info.size,
                                         "mtime": iso(info.mtime), "mtime_epoch": info.mtime, "rule": r.get("rule"),
                                         "motw": m.present, "zone_id": m.zone_id, "dst_fs": out.dst_fs,
                                         "undo_of": rid, "source": "undo"}, self.clock())
                st.undone.append(rid)
                # 戻したファイルを次のスキャンで再び振り分けないよう、除外として記録する
                ds = st.dir(ntpath.dirname(out.dst))
                if ds is not None:
                    ds.set_handled(ntpath.basename(out.dst), info.size, info.mtime, "excluded", reason="undo",
                                   ts=self.clock())
                self.tracker.forget(ntpath.basename(out.dst))
                res.restored.append(dict(rec, name=ntpath.basename(out.dst)))
            self.store.save(st)
        return res

    def undoable_count(self) -> int:
        st = self.store.load()
        ops = self.oplog.all()
        done = set(st.undone) | set(st.undo_skipped) | {str(r.get("undo_of")) for r in ops if r.get("op") == "undo"}
        return sum(1 for r in ops if r.get("op") in ("move", "archive") and r.get("id") not in done)

    # ------------------------------------------------------------ 要確認
    def flagged_list(self) -> list[dict[str, Any]]:
        dl = self.downloads or self.resolve()
        if dl is None:
            return []
        ds = self.store.load().dir(dl)
        if ds is None:
            return []
        out = []
        for h in ds.handled.values():
            if h.get("status") == "flagged":
                out.append({"name": h.get("name"), "reason": h.get("reason"), "size": h.get("size"), "ts": h.get("ts"),
                            "path": ntpath.join(dl, str(h.get("name")))})
        return sorted(out, key=lambda x: -(x.get("ts") or 0))

    def acknowledge(self, name: str, *, timeout: float = 3.0) -> bool:
        """要確認を「確認済み」にする。ファイルは動かさず、以後も自動では扱わない。"""
        dl = self.downloads or self.resolve()
        if dl is None:
            return False
        with self.exclusive(timeout):
            st = self.store.load()
            ds = st.dir(dl)
            h = ds.handled.get(name.lower()) if ds else None
            if ds is None or h is None:
                return False
            h["status"] = "excluded"
            h["acknowledged"] = True
            self.store.save(st)
        return True

    # ------------------------------------------------------------ 状態
    def today_counts(self) -> dict[str, int]:
        """今日の操作の件数。画面が数秒ごとに呼ぶので、oplog が変わっていなければ前回の値を返す。"""
        today = datetime.fromtimestamp(self.clock()).strftime("%Y-%m-%d")
        try:
            st = self.oplog.path.stat()
            key: tuple[Any, ...] = (today, st.st_size, st.st_mtime_ns)
        except OSError:
            key = (today, 0, 0)
        if self._today_cache is not None and self._today_cache[0] == key:
            return dict(self._today_cache[1])
        c = {"move": 0, "archive": 0, "refused": 0, "flagged": 0, "undo": 0, "failed": 0}
        for r in self.oplog.tail(2000):
            if str(r.get("ts", "")).startswith(today) and r.get("op") in c:
                c[str(r["op"])] += 1
        self._today_cache = (key, dict(c))
        return c

    def status(self) -> dict[str, Any]:
        dl = self.resolve()
        problems = self.validate_rules(dl)
        return {
            "downloads": dl,
            "paused": self.cfg.paused,
            "watch_mode": self.cfg.watch_mode,
            "recent": self.oplog.tail(10),
            "invalid_rules": [(r.name, msg) for r, msg in problems],
            "rules": [(r.name, r.mode) for r in self.cfg.rules],
            "snapshot": self.get_snapshot(),
            "state_exists": self.store.exists(),
        }
