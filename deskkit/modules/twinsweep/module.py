# TwinSweep モジュール本体。設定(§9)・スキャンのスレッド・結果の状態・ごみ箱送り・キャッシュ・usage/diagnostics を束ねる。
# 利用者が押したときだけ動く(V-1)。重い処理はスレッドで行い、画面へは ctx.call_soon で返す(NFR-4)。
# ログ・ops.jsonl・diagnostics には件数・バイト数・時間だけを書く(INV-4)。消すのは fileops.recycle だけ(INV-1)。
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Signal

from deskkit.catalog import info
from deskkit.modules.twinsweep import cache as cachemod
from deskkit.modules.twinsweep.grouping import LEVELS
from deskkit.modules.twinsweep.job import STAGE_COUNT, ScanOutcome, ScanRequest, run_scan
from deskkit.modules.twinsweep.oplog import OpsLog
from deskkit.modules.twinsweep.results import ResultModel
from deskkit.modules.twinsweep.scanner import MAX_ROOTS, check_root, excluded_dirs, norm

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

    from deskkit.fileops import RecycleApi, RecycleResult
    from deskkit.modules.twinsweep.thumbs import ThumbLoader
    from deskkit.usage import UsageSeries

DEFAULTS: dict[str, Any] = {"folders": [], "recursive": True, "level": "normal", "exact_only": False}
STOP_WAIT_S = 5.0

# 状態
IDLE = "idle"
SCANNING = "scanning"
GROUPING = "grouping"      # 似ている度合いを変えたときの作り直し
RECYCLING = "recycling"

# 画面の文言(FR-3・FR-5・FR-15)
MSG_FORBIDDEN = "このフォルダは調べられません"
MSG_TOO_MANY = "写真が多すぎます(10 万枚まで)。フォルダを分けてください"
MSG_RESTORE = "元に戻すには、ごみ箱で写真を選んで『元に戻す』を押します"


@dataclass(frozen=True)
class Notice:
    kind: str   # info / warn / error / ok
    text: str


class _Signals(QObject):
    state = Signal()       # 状態・進捗
    results = Signal()     # 結果のモデルが替わった・グループが減った
    selection = Signal()   # 「残す」「ごみ箱へ」が変わった
    notices = Signal()     # お知らせが変わった
    settings = Signal()    # 設定が変わった


def merge_defaults(section: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """足りないキーを既定値で補い、型・範囲の合わない値は既定値に戻す。(結果, 変えたか)。"""
    out = dict(section)
    changed = False
    for k, v in DEFAULTS.items():
        if k not in out:
            out[k] = list(v) if isinstance(v, list) else v
            changed = True
    folders = out.get("folders")
    if not isinstance(folders, list) or not all(isinstance(x, str) for x in folders):
        out["folders"] = []
        changed = True
    elif len(folders) > MAX_ROOTS:
        out["folders"] = folders[:MAX_ROOTS]
        changed = True
    for k in ("recursive", "exact_only"):
        if not isinstance(out.get(k), bool):
            out[k] = DEFAULTS[k]
            changed = True
    if out.get("level") not in LEVELS:
        out["level"] = "normal"
        changed = True
    return out, changed


def human_bytes(n: int) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{int(v)} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} TB"  # pragma: no cover


def open_recycle_bin_default() -> None:
    os.startfile("shell:RecycleBinFolder")  # type: ignore[attr-defined,unused-ignore]  # Windows 専用


class TwinSweepModule:
    def __init__(self, ctx: Any, *, recycle_api: RecycleApi | None = None,
                 compute: Callable[..., Any] | None = None, file_key: Callable[..., Any] | None = None,
                 open_recycle_bin: Callable[[], None] | None = None, env: dict[str, str] | None = None) -> None:
        self.ctx = ctx
        self.log: logging.Logger = ctx.log
        self.accent = info("twinsweep").accent
        section = dict(ctx.settings_dict())
        section.pop("enabled", None)
        merged, changed = merge_defaults(section)
        self.config: dict[str, Any] = merged
        if changed:
            try:
                ctx.write_settings(merged)
            except Exception as e:  # noqa: BLE001 - 書き戻せなくても既定値で動く
                self.log.warning("既定値の書き戻しに失敗: %s", type(e).__name__)
        self.data_dir = Path(ctx.data_dir)
        self.cache_path = self.data_dir / "cache.db"
        self.ops = OpsLog(self.data_dir / "ops.jsonl")
        self.recycle_api = recycle_api
        self._compute = compute
        self._file_key = file_key
        self._open_recycle_bin = open_recycle_bin or open_recycle_bin_default
        self._env = env
        self.signals = _Signals()
        self.state = IDLE
        self.stage = STAGE_COUNT
        self.progress_done = 0
        self.progress_total = 0
        self.model: ResultModel | None = None
        self.last: ScanOutcome | None = None
        self.scan_notices: list[Notice] = []
        self.recycle_notices: list[Notice] = []
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._token = 0
        self._thumbs: ThumbLoader | None = None

    # ================================================================ ライフサイクル
    def start(self) -> None:
        t = threading.Thread(target=self._purge_old_rows, name="twinsweep-purge", daemon=True)
        t.start()
        self._set_status()
        self.log.info("twinsweep started folders=%d level=%s", len(self.config["folders"]), self.config["level"])

    def stop(self) -> None:
        self._token += 1
        self._cancel.set()
        th = self._thread
        if th is not None and th.is_alive():
            th.join(STOP_WAIT_S)  # §10: 書き込み中のキャッシュをコミットしてから止まる(最大 5 秒)
        self._thread = None
        self.state = IDLE
        if self._thumbs is not None:
            self._thumbs.shutdown()
            self._thumbs = None
        self.log.info("twinsweep stopped")

    def handle_cli(self, args: list[str]) -> tuple[int, str]:
        return 2, "unsupported"

    def _post(self, fn: Callable[[], None]) -> None:
        """別スレッド → 画面のスレッドで fn を呼ぶ。例外はログに型名だけを書く(例外の文にパスが入り得るため。INV-4)。"""

        def run() -> None:
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self.log.error("callback failed: %s", type(e).__name__)

        self.ctx.call_soon(run)

    def create_page(self) -> QWidget:
        from deskkit.modules.twinsweep.page import TwinSweepPage

        return TwinSweepPage(self)

    def _purge_old_rows(self) -> None:
        # G-7: 30 日使われなかった行を起動時に消す(このスレッドで開いた接続だけを使う)
        if not self.cache_path.exists():
            return
        try:
            c = cachemod.FeatureCache(self.cache_path)
            try:
                n = c.purge_unused()
            finally:
                c.close()
            if n:
                self.log.info("cache purged rows=%d", n)
        except Exception as e:  # noqa: BLE001 - 掃除できなくても動作に支障はない
            self.log.warning("cache purge failed: %s", type(e).__name__)

    # ================================================================ 設定
    def folders(self) -> list[str]:
        return list(self.config["folders"])

    def _save(self) -> str | None:
        try:
            self.ctx.write_settings(dict(self.config))
        except Exception as e:  # noqa: BLE001 - 保存できなかったことを画面に返す
            self.log.warning("settings write failed: %s", type(e).__name__)
            return f"設定を保存できませんでした({type(e).__name__})"
        self.signals.settings.emit()
        return None

    def excluded(self) -> list[str]:
        # FR-3: Windows・プログラム・アプリのデータのフォルダと、DeskKit のデータフォルダ
        return excluded_dirs(self._env, [self.data_dir.parent, self.data_dir])

    def check_folder(self, path: str) -> str:
        """"ok" / "forbidden" / "drive_root" / "missing" / "duplicate" / "full"。"""
        res = check_root(path, self.excluded())
        if res in ("forbidden", "missing"):
            return res
        n = norm(path)
        if any(norm(f) == n for f in self.config["folders"]):
            return "duplicate"
        if len(self.config["folders"]) >= MAX_ROOTS:
            return "full"
        return res

    def add_folder(self, path: str) -> str | None:
        """ドライブの直下の確認は画面が済ませてから呼ぶ。保存に失敗したら文言を返す。"""
        if self.check_folder(path) not in ("ok", "drive_root"):
            return MSG_FORBIDDEN
        self.config["folders"] = [*self.config["folders"], os.path.normpath(path)][:MAX_ROOTS]
        return self._save()

    def remove_folder(self, path: str) -> str | None:
        self.config["folders"] = [f for f in self.config["folders"] if f != path]
        return self._save()

    def set_recursive(self, v: bool) -> str | None:
        self.config["recursive"] = bool(v)
        return self._save()

    def set_level(self, level: str) -> str | None:
        if level not in LEVELS:
            return "不明な設定です"
        self.config["level"] = level
        err = self._save()
        self.regroup()
        return err

    def set_exact_only(self, v: bool) -> str | None:
        self.config["exact_only"] = bool(v)
        err = self._save()
        self.regroup()
        return err

    # ================================================================ スキャン
    @property
    def busy(self) -> bool:
        return self.state != IDLE

    def start_scan(self) -> str | None:
        """スキャンを始める。始められないときは理由の文言を返す。"""
        if self.busy:
            return "処理中です"
        roots_all = self.folders()
        if not roots_all:
            return "調べるフォルダを追加してください"
        excluded = self.excluded()
        roots = [r for r in roots_all if check_root(r, excluded) in ("ok", "drive_root")]
        self.scan_notices = []
        self.recycle_notices = []
        skipped = len(roots_all) - len(roots)
        if skipped:
            self.scan_notices.append(Notice("warn", f"{MSG_FORBIDDEN}(見つからないか、調べられないフォルダ: {skipped} 個)"))
        if not roots:
            self.signals.notices.emit()
            return MSG_FORBIDDEN
        req = ScanRequest(roots=roots, recursive=bool(self.config["recursive"]), level=str(self.config["level"]),
                          exact_only=bool(self.config["exact_only"]), excluded=excluded, cache_path=self.cache_path)
        self._cancel = threading.Event()
        self._token += 1
        token = self._token
        cancel = self._cancel
        self.state = SCANNING
        self.stage, self.progress_done, self.progress_total = STAGE_COUNT, 0, 0
        self.signals.state.emit()
        self.signals.notices.emit()
        self._set_status()
        self.log.info("scan start roots=%d recursive=%s level=%s exact_only=%s", len(roots), req.recursive, req.level,
                      req.exact_only)

        def progress(stage: str, done: int, total: int) -> None:
            self._post(lambda: self._on_progress(token, stage, done, total))

        def work() -> None:
            kw: dict[str, Any] = {}
            if self._compute is not None:
                kw["compute"] = self._compute
            if self._file_key is not None:
                kw["file_key"] = self._file_key
            try:
                out = run_scan(req, cancel, progress, **kw)
            except Exception as e:  # noqa: BLE001 - スレッドの例外は画面に返す
                self.log.error("scan failed: %s", type(e).__name__)
                out = ScanOutcome(status="error")
            self._post(lambda: self._on_scan_done(token, out))

        self._thread = threading.Thread(target=work, name="twinsweep-scan", daemon=True)
        self._thread.start()
        return None

    def cancel_scan(self) -> None:
        if self.state in (SCANNING, GROUPING):
            self._cancel.set()

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """テスト・自己検査用: スレッドの終わりを待つ(画面のスレッドへの返しは呼ぶ側が流す)。"""
        th = self._thread
        if th is None:
            return True
        th.join(timeout)
        return not th.is_alive()

    def _on_progress(self, token: int, stage: str, done: int, total: int) -> None:
        if token != self._token or self.state != SCANNING:
            return
        self.stage, self.progress_done, self.progress_total = stage, done, total
        self.signals.state.emit()

    def _on_scan_done(self, token: int, out: ScanOutcome) -> None:
        if token != self._token:
            return
        self._thread = None
        self.state = IDLE
        self.last = out
        n: list[Notice] = list(self.scan_notices)
        if out.status == "too_many":
            n.append(Notice("error", MSG_TOO_MANY))
        elif out.status == "cancelled":
            n.append(Notice("info", "中止しました。ここまでに調べた分は次回に使います。"))
        elif out.status == "error":
            n.append(Notice("error", "調べている途中で問題が起きました。もう一度お試しください。"))
        if out.cache_rebuilt:
            n.append(Notice("info", "キャッシュを作り直しました"))
        if out.status == "cache_error":
            n.append(Notice("warn", "キャッシュが使えなかったため、次回も最初から調べます"))
        if out.unreadable:
            n.append(Notice("warn", f"読めなかった画像: {out.unreadable:,} 枚"))
        if out.cloud_only:
            n.append(Notice("info", f"クラウドにだけある写真: {out.cloud_only:,} 枚(調べていません)"))
        self.scan_notices = n
        if out.model is not None:
            self.model = out.model
            groups = len(out.model.groups)
            self.ops.write("scan", files=out.files, groups=groups, skipped=out.unreadable,
                           bytes=out.model.selected_bytes(), ms=out.elapsed_ms)
            self.log.info("scan done files=%d opened=%d groups=%d unreadable=%d cloud_only=%d ms=%d", out.files, out.opened,
                          groups, out.unreadable, out.cloud_only, out.elapsed_ms)
            # 調べている間に似ている度合いが変わっていたら作り直す
            self.regroup()
        else:
            self.log.info("scan ended status=%s files=%d ms=%d", out.status, out.files, out.elapsed_ms)
        self.signals.results.emit()
        self.signals.notices.emit()
        self.signals.state.emit()
        self.signals.selection.emit()
        self._set_status()

    def regroup(self) -> None:
        """FR-16・FR-17: 特徴を調べ直さずに作り直す(スレッドで)。"""
        m = self.model
        if m is None or self.state != IDLE:
            return
        level, exact_only = str(self.config["level"]), bool(self.config["exact_only"])
        if m.level == level and m.exact_only == exact_only:
            return
        self._cancel = threading.Event()
        cancel = self._cancel
        self._token += 1
        token = self._token
        self.state = GROUPING
        self.signals.state.emit()

        def work() -> None:
            try:
                new: ResultModel | None = m.rebuild(level, exact_only, cancel)
            except Exception as e:  # noqa: BLE001
                self.log.error("regroup failed: %s", type(e).__name__)
                new = None
            self._post(lambda: self._on_regrouped(token, new, cancel.is_set()))

        self._thread = threading.Thread(target=work, name="twinsweep-group", daemon=True)
        self._thread.start()

    def _on_regrouped(self, token: int, new: ResultModel | None, cancelled: bool) -> None:
        if token != self._token:
            return
        self._thread = None
        self.state = IDLE
        if new is not None and not cancelled:
            self.model = new
            self.recycle_notices = []
            self.log.info("regroup done level=%s exact_only=%s groups=%d", new.level, new.exact_only, len(new.groups))
            self.signals.results.emit()
            self.signals.notices.emit()
        self.signals.state.emit()
        self.signals.selection.emit()
        self._set_status()
        if new is not None and not cancelled:
            self.regroup()  # 作り直しの間に設定が変わっていたら、もう一度(同じなら何もしない)

    # ================================================================ 選択(G-5)
    def set_keep(self, pid: int, keep: bool) -> bool:
        if self.model is None or self.state != IDLE:
            return False
        ok = self.model.set_keep(pid, keep)
        if ok:
            self.signals.selection.emit()
        return ok

    def can_trash(self, pid: int) -> bool:
        return self.model is not None and self.state == IDLE and self.model.can_trash(pid)

    # ================================================================ ごみ箱へ(FR-12〜FR-15)
    def recycle_selected(self, parent_hwnd: int | None = None) -> str | None:
        """確認ダイアログの承認のあとに呼ぶ(INV-1)。直前に INV-2 を確かめ、破れていれば送らない。"""
        m = self.model
        if m is None or self.busy:
            return "処理中です" if self.busy else "結果がありません"
        if not m.invariant_ok():
            self.log.error("recycle refused: group without keep")
            return "「残す」写真の無いグループがあるため送りませんでした"
        chosen = m.selected()
        if not chosen:
            return "ごみ箱へ送る写真が選ばれていません"
        items = [(p.pid, p.path, p.size, p.mtime_ns) for p in chosen]
        groups = sum(1 for g in m.groups if any(not m.is_keep(p.pid) for p in g.photos))
        self.state = RECYCLING
        self.recycle_notices = []
        self.signals.state.emit()
        self.signals.notices.emit()
        self._set_status()
        self._token += 1
        token = self._token
        api = self.recycle_api

        def work() -> None:
            t0 = time.perf_counter()
            changed: list[int] = []
            ok: list[tuple[int, str, int]] = []
            for pid, path, size, mtime_ns in items:
                try:
                    st = os.stat(path)
                except OSError:
                    changed.append(pid)
                    continue
                if st.st_size != size or st.st_mtime_ns != mtime_ns:  # FR-13
                    changed.append(pid)
                    continue
                ok.append((pid, path, size))
            res: RecycleResult | None = None
            err: str | None = None
            if ok:
                try:
                    from deskkit.fileops import recycle

                    res = recycle([Path(p) for _, p, _ in ok], parent_hwnd, api=api)
                except Exception as e:  # noqa: BLE001 - 送れなかったことを画面に返す
                    err = type(e).__name__
            ms = int((time.perf_counter() - t0) * 1000)
            self._post(lambda: self._on_recycled(token, ok, changed, res, err, groups, ms))

        self._thread = threading.Thread(target=work, name="twinsweep-recycle", daemon=True)
        self._thread.start()
        return None

    def _on_recycled(self, token: int, ok: Sequence[tuple[int, str, int]], changed: list[int], res: RecycleResult | None,
                     err: str | None, groups: int, ms: int) -> None:
        if token != self._token:
            return
        self._thread = None
        self.state = IDLE
        by_path = {os.path.normcase(str(Path(p))): (pid, size) for pid, p, size in ok}
        sent_pids: list[int] = []
        sent_bytes = 0
        if res is not None:
            for p in res.sent:
                hit = by_path.get(os.path.normcase(str(p)))
                if hit is not None:
                    sent_pids.append(hit[0])
                    sent_bytes += hit[1]
        n: list[Notice] = []
        if sent_pids:
            n.append(Notice("ok", f"{len(sent_pids):,} 枚をごみ箱へ送りました({human_bytes(sent_bytes)})"))
            n.append(Notice("info", MSG_RESTORE))
        if changed:
            n.append(Notice("warn", f"スキャンのあとに変わった写真: {len(changed):,} 枚(送りませんでした)"))
        if res is not None:
            nb = res.count("skipped_no_recycle_bin")
            if nb:
                n.append(Notice("warn", f"このドライブにはごみ箱が無いため送りませんでした: {nb:,} 枚"))
            ab = res.count("aborted")
            if ab:
                n.append(Notice("info", f"送りませんでした: {ab:,} 枚(取り消しました)"))
            iu = res.count("in_use")
            if iu:
                n.append(Notice("warn", f"ほかのアプリが使っているため送れなかった写真: {iu:,} 枚"))
            nf = res.count("not_found")
            if nf:
                n.append(Notice("warn", f"見つからなかった写真: {nf:,} 枚(送りませんでした)"))
            fl = res.count("failed")
            if fl:
                n.append(Notice("error", f"送れなかった写真: {fl:,} 枚"))
        if err:
            n.append(Notice("error", f"ごみ箱へ送れませんでした({err})"))
        self.recycle_notices = n
        skipped = len(changed) + (len(res.skipped) if res is not None else len(ok))
        self.ops.write("recycle", files=len(ok) + len(changed), groups=groups, sent=len(sent_pids), skipped=skipped,
                       bytes=sent_bytes, ms=ms)
        self.log.info("recycle sent=%d skipped=%d changed=%d ms=%d", len(sent_pids), skipped, len(changed), ms)
        if self.model is not None and sent_pids:
            self.model.remove_photos(sent_pids)  # FR-15
        self.signals.results.emit()
        self.signals.notices.emit()
        self.signals.state.emit()
        self.signals.selection.emit()
        self._set_status()

    def open_recycle_bin(self) -> None:
        try:
            self._open_recycle_bin()
        except OSError as e:
            self.log.warning("open recycle bin failed: %s", type(e).__name__)

    # ================================================================ キャッシュ(FR-18)
    def clear_cache(self) -> bool:
        if self.busy:
            return False
        ok = cachemod.clear_cache(self.cache_path)
        self.log.info("cache cleared ok=%s", ok)
        return ok

    def cache_rows(self) -> int:
        return cachemod.row_count(self.cache_path)

    # ================================================================ サムネイル
    def thumbs(self) -> ThumbLoader:
        if self._thumbs is None:
            from deskkit.modules.twinsweep.thumbs import ThumbLoader

            self._thumbs = ThumbLoader(self._post)
        return self._thumbs

    # ================================================================ 状態表示・usage・diagnostics
    def _set_status(self) -> None:
        try:
            if self.state == SCANNING:
                text = "調べています"
            elif self.state == RECYCLING:
                text = "ごみ箱へ送っています"
            elif self.model is not None:
                text = f"似た写真のグループ {len(self.model.groups):,}"
            else:
                text = "待機中"
            self.ctx.set_tray_status(text)
        except Exception:  # noqa: BLE001 - 状態表示は失敗しても続ける
            pass

    def usage(self, days: int) -> list[UsageSeries]:
        """FR-19: 日ごとの「ごみ箱へ送った枚数」(primary)とスキャンの回数。ops.jsonl の数値だけを数える。"""
        from deskkit.usage import UsageSeries

        sent = self.ops.per_day(days, "recycle", "sent")
        scans = self.ops.per_day(days, "scan", None)
        return [
            UsageSeries("recycled", "ごみ箱へ送った写真", sent, unit="枚", primary=True,
                        hint="公開から 90 日で 0 枚のままなら紹介から外す(R-2)"),
            UsageSeries("scans", "スキャンの回数", scans, unit="回", good_when="neutral"),
        ]

    def diagnostics(self) -> dict[str, str | int | bool]:
        """FR-20: 件数・秒数・設定だけ(パス・ファイル名は入れない。INV-4)。"""
        last = self.last
        return {
            "state": self.state,
            "cache_rows": self.cache_rows(),
            "last_scan_files": last.files if last else 0,
            "last_scan_groups": len(last.model.groups) if last and last.model else 0,
            "last_scan_seconds": round(last.elapsed_ms / 1000) if last else 0,
            "last_scan_unreadable": last.unreadable if last else 0,
            "last_scan_status": last.status if last else "none",
            "level": str(self.config["level"]),
            "exact_only": bool(self.config["exact_only"]),
            "recursive": bool(self.config["recursive"]),
            "folders": len(self.config["folders"]),
        }
