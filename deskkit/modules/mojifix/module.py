# MojiFix モジュール本体。3つの作業(文字化けしたファイル・zip の名前・名前の濁点)の状態を持ち、重い処理をワーカー1本に渡す。
# 利用者が操作したときだけ動く。結果は画面にだけ出し、ctx.notify は使わない(FR-3)。
# ログ・ops.jsonl・diagnostics・usage にはファイル名・パス・zip の中の名前・本文を書かない(INV-5)。
from __future__ import annotations

import logging
import os
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Signal

from deskkit.catalog import info
from deskkit.modules.mojifix import config as cfgmod
from deskkit.modules.mojifix import oplog
from deskkit.modules.mojifix import renamer as R
from deskkit.modules.mojifix import sniff as S
from deskkit.modules.mojifix import textfix as TF
from deskkit.modules.mojifix import zipextract as ZX
from deskkit.modules.mojifix import zipnames as Z
from deskkit.modules.mojifix.oplog import OpsLog
from deskkit.modules.mojifix.owned import Owned
from deskkit.modules.mojifix.worker import Task, Worker
from deskkit.usage import UsageSeries, count_jsonl

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

MAX_TEXT_QUEUE = 50
MAX_CLI = 200
MSG_QUEUE_FULL = "一度に積めるのは 50 件までです"
MSG_FAILED = "うまくいきませんでした"

AREA_TEXT, AREA_ZIP, AREA_RENAME = "text", "zip", "rename"


class Signals(QObject):
    changed = Signal(str)                    # 状態が変わった area("text" / "zip" / "rename" / "busy")
    progress = Signal(str, str, int, int)    # (kind, stage, done, total)
    area = Signal(str)                       # 画面を切り替えてほしい area(ドロップ・CLI)


@dataclass
class Notice:
    kind: str       # "ok" / "info" / "warn" / "error"
    text: str


def kind_of(p: Path) -> str:
    """FR-1: .zip → zip、フォルダ → 名前の濁点、それ以外のファイル → 文字化けしたファイル。"""
    if p.is_dir():
        return AREA_RENAME
    if p.suffix.lower() == ".zip":
        return AREA_ZIP
    return AREA_TEXT


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


class MojiFixModule:
    def __init__(self, ctx: Any, *, fallback_dir: Callable[[], Path] | None = None,
                 free_bytes: Callable[[Path], int] | None = None, env: dict[str, str] | None = None,
                 rename: Callable[[Path, Path], None] | None = None, now: Callable[[], datetime] | None = None) -> None:
        self.ctx = ctx
        self.log: logging.Logger = ctx.log
        section = dict(ctx.settings_dict())
        section.pop("enabled", None)
        merged, changed = cfgmod.normalize(section)
        self.config = cfgmod.parse(merged)
        if changed:
            try:
                ctx.write_settings(merged)
            except Exception as e:  # noqa: BLE001 - 書き戻せなくても既定値で動く
                self.log.warning("settings write-back failed: %s", type(e).__name__)
        self._fallback = fallback_dir or self._default_fallback
        self._free = free_bytes
        self._env = env
        self._rename = rename or os.rename
        self._now: Callable[[], datetime] = now or datetime.now
        self.signals = Signals()
        self.ops = OpsLog(Path(ctx.data_dir) / "ops.jsonl", now=lambda: self._now())   # 記録の時刻も渡された時計で(usage の区切りと揃える)
        self.worker = Worker(self._on_progress)
        self._stopped = False
        self.counts: Counter[str] = Counter()
        self.last_ms = 0
        self.progress_state: tuple[str, str, int, int] | None = None
        # 文字化けしたファイル
        self.text_queue: list[Path] = []
        self.text_current: Path | None = None
        self.sniff: S.Sniff | None = None
        self.text_notice: Notice | None = None
        self.text_is_zip = False
        self.chosen: str | None = None
        self.text_check: TF.CheckResult | None = None
        self.text_ask: str | None = None          # "undecodable" / "unencodable"(画面が聞く)
        self.text_out: TF.WriteResult | None = None
        self.queue_full = False
        # zip の名前
        self.zip_path: Path | None = None
        self.zip_scan: Z.ZipScan | None = None
        self.zip_notice: Notice | None = None
        self.zip_cands: list[Z.NameCandidate] = []
        self.zip_key: str | None = None
        self.zip_plan: list[Z.PlanItem] = []
        self.zip_result: ZX.ExtractResult | None = None
        # 名前の濁点
        self.rn_folder: Path | None = None
        self.rn_notice: Notice | None = None
        self.rn_scan: R.ScanResult | None = None
        self.rn_selected: set[int] = set()
        self.rn_done: list[R.Done] = []
        self.rn_last: R.RenameResult | None = None
        self.rn_undo_result: R.RenameResult | None = None

    # ================================================================ ライフサイクル
    @property
    def accent(self) -> str:
        return info("mojifix").accent

    @staticmethod
    def _default_fallback() -> Path:
        from deskkit.modules.mojifix._win32 import documents_mojifix

        return documents_mojifix()

    def start(self) -> None:
        self.ctx.add_tray_action("MojiFix を開く", self.ctx.show_page)
        self._status()
        self.log.info("mojifix started")

    def stop(self) -> None:
        self._stopped = True
        stopped = self.worker.stop(5.0)
        self.rn_done = []  # M-15: 取り消しの記録はモジュールの停止で消える
        self.log.info("mojifix stopped worker_stopped=%s", stopped)

    def handle_cli(self, args: list[str]) -> tuple[int, str]:
        if args[:1] == ["open"]:
            n = self.add_paths([Path(a) for a in args[1:1 + MAX_CLI]])
            self.ctx.show_page()
            return 0, f"queued {n}"
        return 2, "unsupported"

    def create_page(self) -> QWidget:
        from deskkit.modules.mojifix.page import MojiFixPage

        return MojiFixPage(self)

    def busy(self) -> bool:
        return self.worker.busy()

    def cancel(self) -> None:
        self.worker.cancel()

    def _status(self) -> None:
        k = self.worker.kind()
        try:
            self.ctx.set_tray_status("処理中" if k else "待機中")
        except Exception:  # noqa: BLE001 - 状態表示の失敗で処理を止めない
            pass

    def _emit(self, area: str) -> None:
        if not self._stopped:
            self.signals.changed.emit(area)

    def _on_progress(self, kind: str, stage: str, done: int, total: int) -> None:  # ワーカーのスレッドから
        def go() -> None:
            if self._stopped:
                return
            self.progress_state = (kind, stage, done, total)
            self.signals.progress.emit(kind, stage, done, total)

        self.ctx.call_soon(go)

    def _run(self, kind: str, fn: Callable[[Task], Any], done: Callable[[Any, BaseException | None], None]) -> bool:
        t0 = time.monotonic()

        def finished(res: Any, err: BaseException | None) -> None:  # ワーカーのスレッドから
            def go() -> None:
                self.last_ms = _ms(t0)
                self.progress_state = None
                if self._stopped:
                    return
                try:
                    done(res, err)
                finally:
                    self._status()
                    self._emit("busy")
                    self._kick()

            self.ctx.call_soon(go)

        ok = self.worker.submit(kind, fn, finished)
        if ok:
            self.progress_state = (kind, "start", 0, 0)
            self._status()
            self._emit("busy")
        return ok

    def _record(self, op: str, result: str, reason: str | None = None, **kw: Any) -> None:
        self.counts[f"{op}.{result}"] += 1
        if reason:
            self.counts[f"reason.{reason}"] += 1
        self.ops.write(op=op, result=result, reason=reason, ms=self.last_ms, **kw)
        self.log.info("%s result=%s reason=%s", op, result, reason or "-")

    def _unexpected(self, where: str, err: BaseException) -> None:
        self.log.error("%s failed: %s", where, type(err).__name__)

    # ================================================================ 受け取り(FR-1・FR-2)
    def add_paths(self, paths: Iterable[Path | str]) -> int:
        """ドロップ・CLI のパスを振り分けて積む。積んだ数を返す。"""
        n = 0
        last_area: str | None = None
        texts: list[Path] = []
        for raw in list(paths)[:MAX_CLI]:
            p = Path(os.path.abspath(str(raw)))
            try:
                k = kind_of(p)
            except OSError:
                continue
            if k == AREA_TEXT and not p.is_file():
                continue
            last_area = k
            if k == AREA_TEXT:
                texts.append(p)
            elif k == AREA_ZIP:
                self.set_zip(p)
                n += 1
            else:
                self.set_folder(p)
                n += 1
        if texts:
            n += self.add_texts(texts)
        if last_area is not None:
            self.signals.area.emit(last_area)
        self.log.info("paths added count=%d", n)
        return n

    def _kick(self) -> None:
        """待っている読み込みがあれば始める(処理中に積まれた物)。"""
        if self._stopped or self.busy():
            return
        if self.text_current is None and self.text_queue:
            self.select_text(self.text_queue[0])
        elif self.zip_path is not None and self.zip_scan is None and self.zip_notice is None:
            self.load_zip()

    # ================================================================ 文字化けしたファイル(FR-5〜FR-16)
    def add_texts(self, paths: Sequence[Path]) -> int:
        keys = {os.path.normcase(str(p)) for p in self.text_queue}
        added = 0
        self.queue_full = False
        first_new: Path | None = None
        for p in paths:
            k = os.path.normcase(str(p))
            if k in keys:
                continue
            if len(self.text_queue) >= MAX_TEXT_QUEUE:
                self.queue_full = True
                continue
            self.text_queue.append(p)
            keys.add(k)
            added += 1
            first_new = first_new or p
        if first_new is not None and self.text_current is None and not self.busy():
            self.select_text(first_new)
        self._emit(AREA_TEXT)
        return added

    def remove_text(self, p: Path) -> None:
        if self.busy() and p == self.text_current:
            return
        self.text_queue = [q for q in self.text_queue if q != p]
        if p == self.text_current:
            self._clear_text()
        self.queue_full = False
        self._emit(AREA_TEXT)

    def _clear_text(self) -> None:
        self.text_current = None
        self.sniff = None
        self.text_notice = None
        self.text_is_zip = False
        self.chosen = None
        self.text_check = None
        self.text_ask = None
        self.text_out = None

    def select_text(self, p: Path) -> bool:
        """FR-5: 1件を選ぶと読み込む(先頭 256KB)。"""
        if self.busy():
            return False
        self._clear_text()
        self.text_current = p

        def work(_t: Task) -> S.Sniff:
            return S.sniff_file(p)

        def done(res: Any, err: BaseException | None) -> None:
            if p != self.text_current:
                return
            if err is not None:
                if not isinstance(err, OSError):
                    self._unexpected("sniff", err)
                self.text_notice = Notice("error", S.MSG_UNREADABLE)
                self._record("text", "refused", "unreadable")
            else:
                sn: S.Sniff = res
                self.sniff = sn
                if sn.kind == "empty":
                    self.text_notice = Notice("warn", S.MSG_EMPTY)
                    self._record("text", "refused", "empty", in_bytes=0)
                elif sn.kind == "binary":
                    self.text_notice = Notice("warn", S.MSG_BINARY)
                    self.text_is_zip = sn.is_zip
                    self._record("text", "refused", "binary", in_bytes=sn.size)
                elif sn.kind == "ascii":
                    self.text_notice = Notice("info", S.MSG_ASCII)
                    self.chosen = "utf8"  # FR-8: UTF-8 を選んだ状態にする
                self.log.info("sniff kind=%s bytes=%d top=%s", sn.kind, sn.size,
                              sn.candidates[0].codec if sn.candidates else "-")
            self._emit(AREA_TEXT)

        ok = self._run("sniff", work, done)
        self._emit(AREA_TEXT)
        return ok

    def reload_text(self) -> bool:
        return self.select_text(self.text_current) if self.text_current is not None else False

    def choose(self, key: str) -> None:
        """FR-7: 候補を選ぶ(1度選ぶまで「書き出す」は押せない。M-1)。"""
        if self.sniff is None or self.sniff.candidate(key) is None or self.busy():
            return
        self.chosen = key
        self.text_ask = None
        self.text_check = None
        self.text_out = None
        if self.text_notice is not None and self.text_notice.kind != "info":
            self.text_notice = None
        self._emit(AREA_TEXT)

    def can_export(self) -> bool:
        return (not self.busy() and self.text_current is not None and self.sniff is not None
                and self.sniff.kind in ("text", "ascii") and self.chosen is not None)

    def text_options(self) -> TF.Options:
        assert self.chosen is not None
        c = self.config
        return TF.Options(self.chosen, c.text_output, c.newline, c.text_compose)

    def export(self) -> bool:
        """FR-10: 確認の読み(M-5)をして、問題が無ければそのまま書き出す。"""
        if not self.can_export():
            return False
        return self._export(self.text_options(), None)

    def continue_export(self, *, geta_undecodable: bool = False, geta_unencodable: bool = False,
                        out: str | None = None) -> bool:
        """FR-11・FR-12 の選択のあとに続ける。確認の結果はそのまま使う(読み直さない)。"""
        chk = self.text_check
        if chk is None or self.busy():
            return False
        o = chk.options
        opts = replace(o, geta_undecodable=o.geta_undecodable or geta_undecodable,
                       geta_unencodable=o.geta_unencodable or geta_unencodable, out=out or o.out)
        if out is not None and out != self.config.text_output:
            self.update_settings(lambda s: s.__setitem__("text_output", out))
        return self._export(opts, chk)

    def refuse_export(self) -> None:
        """「候補を選び直す」「やめる」。"""
        ask = self.text_ask
        chk = self.text_check
        self.text_ask = None
        self.text_check = None
        if ask is not None and chk is not None:
            o = chk.options
            self._record("text", "refused", ask, codec_in=o.in_codec, codec_out=o.out_codec, in_bytes=chk.in_bytes,
                         lines=chk.lines)
        if ask == "undecodable":
            self.chosen = None  # 候補を選び直す
        self._emit(AREA_TEXT)

    def _export(self, opts: TF.Options, prior: TF.CheckResult | None) -> bool:
        p = self.text_current
        assert p is not None
        self.text_ask = None
        self.text_out = None
        if self.text_notice is not None and self.text_notice.kind != "info":
            self.text_notice = None

        def work(t: Task) -> tuple[str, Any]:
            chk = prior
            if chk is None:
                chk = TF.check(p, opts, t.cancelled, lambda d, tot: t.progress("check", d, tot))
            if chk.undecodable and not opts.geta_undecodable:
                return "undecodable", chk
            if chk.unencodable_for(opts) and not opts.geta_unencodable:
                return "unencodable", chk
            if chk.unchanged(opts):
                return "unchanged", chk
            w = TF.write(p, chk, opts, Owned(), self._fallback, t.cancelled, t.progress, free_bytes=self._free)
            return "ok", (chk, w)

        def done(res: Any, err: BaseException | None) -> None:
            if p != self.text_current:
                return
            if err is not None:
                self._text_failed(opts, err)
                self._emit(AREA_TEXT)
                return
            what, val = res
            if what in ("undecodable", "unencodable"):
                self.text_check = val
                self.text_ask = what
            elif what == "unchanged":
                self.text_check = None
                self.text_notice = Notice("info", TF.MSG_UNCHANGED)
                self._record("text", "unchanged", None, codec_in=opts.in_codec, codec_out=opts.out_codec,
                             in_bytes=val.in_bytes, lines=val.lines)
            else:
                chk, w = val
                self.text_check = None
                self.text_out = w
                self.text_notice = Notice("ok", done_text(w, opts))
                self._record("text", "ok", None, codec_in=opts.in_codec, codec_out=opts.out_codec, in_bytes=chk.in_bytes,
                             out_bytes=w.out_bytes, lines=w.lines, replaced=w.replaced, composed=w.composed, files=1)
            self._emit(AREA_TEXT)

        ok = self._run("text", work, done)
        self._emit(AREA_TEXT)
        return ok

    def _text_failed(self, opts: TF.Options, err: BaseException) -> None:
        self.text_check = None
        kw: dict[str, Any] = {"codec_in": opts.in_codec, "codec_out": opts.out_codec}
        if isinstance(err, TF.CancelledError):
            self.text_notice = Notice("info", "中止しました。書きかけのファイルは消しました")
            self._record("text", "cancelled", None, **kw)
            return
        if isinstance(err, TF.TextError):
            self.text_notice = Notice("error", err.message)
            self._record("text", "error", err.reason, **kw)
            if err.reason == "changed":  # FR-15: 読み込み直す
                keep = self.text_notice
                self.ctx.call_soon(lambda: self._reload_after_change(keep))
            return
        self._unexpected("text export", err)
        self.text_notice = Notice("error", TF.MSG_FAILED)
        self._record("text", "error", None, **kw)

    def _reload_after_change(self, notice: Notice) -> None:
        if self.reload_text():
            self.text_notice = notice
            self._emit(AREA_TEXT)

    # ================================================================ zip の名前(FR-17〜FR-24)
    def set_zip(self, p: Path) -> None:
        if self.busy() and self.worker.kind() in ("zip", "zipload", "plan"):
            return
        self.zip_path = p
        self.zip_scan = None
        self.zip_notice = None
        self.zip_cands = []
        self.zip_key = None
        self.zip_plan = []
        self.zip_result = None
        if not self.busy():
            self.load_zip()
        self._emit(AREA_ZIP)

    def zip_dest_preview(self) -> Path | None:
        return self.zip_path.parent / (self.zip_path.stem or "zip") if self.zip_path is not None else None

    def load_zip(self) -> bool:
        p = self.zip_path
        if p is None or self.busy():
            return False

        def work(_t: Task) -> tuple[Z.ZipScan, list[Z.NameCandidate]]:
            scan = Z.load(p)
            return scan, (Z.candidates(scan) if scan.needs_candidates else [])

        def done(res: Any, err: BaseException | None) -> None:
            if p != self.zip_path:
                return
            if err is not None:
                if isinstance(err, Z.ZipLoadError):
                    self.zip_notice = Notice("error", err.message)
                    self._record("zip", "refused", err.reason)
                else:
                    self._unexpected("zip load", err)
                    self.zip_notice = Notice("error", Z.MSG_BROKEN)
                    self._record("zip", "refused", "broken_zip")
                self._emit(AREA_ZIP)
                return
            self.zip_scan, self.zip_cands = res
            self.log.info("zip loaded entries=%d candidates=%d", len(self.zip_scan.entries), len(self.zip_cands))
            if not self.zip_cands:
                self.zip_notice = Notice("info", Z.MSG_NO_MOJIBAKE)
                self.ctx.call_soon(lambda: self.choose_zip("utf8"))
            self._emit(AREA_ZIP)

        ok = self._run("zipload", work, done)
        self._emit(AREA_ZIP)
        return ok

    def choose_zip(self, key: str) -> bool:
        """FR-19: 候補を選ぶと、全項目の最終的な名前と状態を一覧にする。"""
        scan = self.zip_scan
        dest = self.zip_dest_preview()
        if scan is None or dest is None or key not in Z.NAME_CODEC or self.busy():
            return False
        c = self.config
        self.zip_key = key
        self.zip_result = None
        self.zip_plan = []

        def work(_t: Task) -> list[Z.PlanItem]:
            return Z.plan(scan, key, dest, compose_names=c.zip_compose, skip_mac=c.zip_skip_mac_files)

        def done(res: Any, err: BaseException | None) -> None:
            if scan is not self.zip_scan or key != self.zip_key:
                return
            if err is not None:
                self._unexpected("zip plan", err)
                self.zip_plan = []
            else:
                self.zip_plan = res
            self._emit(AREA_ZIP)

        ok = self._run("plan", work, done)
        self._emit(AREA_ZIP)
        return ok

    def can_extract(self) -> bool:
        return not self.busy() and self.zip_scan is not None and self.zip_key is not None and bool(self.zip_plan)

    def extract_zip(self) -> bool:
        scan, key, p = self.zip_scan, self.zip_key, self.zip_path
        if not self.can_extract() or scan is None or key is None or p is None:
            return False
        c = self.config
        self.zip_result = None
        if self.zip_notice is not None and self.zip_notice.kind != "info":
            self.zip_notice = None

        def work(t: Task) -> ZX.ExtractResult:
            return ZX.unpack(p, scan, key, Owned(), self._fallback, compose_names=c.zip_compose,
                              skip_mac=c.zip_skip_mac_files, max_total_gb=c.zip_max_total_gb, cancel=t.cancelled,
                              progress=t.progress, free_bytes=self._free)

        def done(res: Any, err: BaseException | None) -> None:
            codec = Z.NAME_CODEC.get(key) if scan.needs_candidates else None
            kw: dict[str, Any] = {"codec_in": codec, "in_bytes": scan.size}
            if err is not None:
                if isinstance(err, ZX.CancelledError):
                    self.zip_notice = Notice("info", "中止しました。展開したものは消しました")
                    self._record("zip", "cancelled", None, **kw)
                elif isinstance(err, ZX.ExtractError):
                    self.zip_notice = Notice("error", err.message)
                    self._record("zip", "error", err.reason if err.reason in oplog.REASONS else None, **kw)
                else:
                    self._unexpected("zip extract", err)
                    self.zip_notice = Notice("error", ZX.MSG_FAILED)
                    self._record("zip", "error", None, **kw)
                self._emit(AREA_ZIP)
                return
            r: ZX.ExtractResult = res
            self.zip_result = r
            text = r.summary()
            if r.fallback:
                text += "(ドキュメント\\MojiFix に置きました)"
            if r.zone_failed:
                text += "\n" + ZX.MSG_ZONE
            self.zip_notice = Notice("ok", text)
            self._record("zip", "ok", None, files=r.extracted, skipped=r.skipped_total, replaced=r.renamed,
                         composed=r.composed, **kw)
            self._emit(AREA_ZIP)

        ok = self._run("zip", work, done)
        self._emit(AREA_ZIP)
        return ok

    # ================================================================ 名前の濁点(FR-25〜FR-30)
    def set_folder(self, p: Path) -> None:
        if self.busy() and self.worker.kind() in ("scan", "rename", "undo"):
            return
        self.rn_folder = p
        self.rn_scan = None
        self.rn_selected = set()
        self.rn_last = None
        self.rn_undo_result = None
        self.rn_notice = None
        if R.is_forbidden(p, self._env):
            self.rn_notice = Notice("error", R.MSG_FORBIDDEN)
            self._record("rename", "refused", "forbidden_folder")
        self._emit(AREA_RENAME)

    def folder_needs_confirm(self) -> bool:
        return self.rn_folder is not None and R.is_drive_root(self.rn_folder)

    def scan_folder(self) -> bool:
        """FR-25〜FR-27。ドライブの直下の確認は画面が先に聞く。"""
        folder = self.rn_folder
        if folder is None or self.busy() or R.is_forbidden(folder, self._env):
            return False
        recursive = self.config.rename_recursive
        self.rn_scan = None
        self.rn_notice = None
        self.rn_last = None
        self.rn_undo_result = None

        def work(t: Task) -> R.ScanResult:
            return R.scan(folder, recursive, t.cancelled, lambda n: t.progress("scan", n, 0))

        def done(res: Any, err: BaseException | None) -> None:
            if folder != self.rn_folder:
                return
            if err is not None:
                if isinstance(err, R.ScanError):
                    self.rn_notice = Notice("error", err.message)
                    self._record("rename", "refused", err.reason)
                elif isinstance(err, R.CancelledError):
                    self.rn_notice = Notice("info", "中止しました")
                else:
                    self._unexpected("scan", err)
                    self.rn_notice = Notice("error", MSG_FAILED)
                self._emit(AREA_RENAME)
                return
            sc: R.ScanResult = res
            self.rn_scan = sc
            self.rn_selected = {i for i, x in enumerate(sc.items) if x.fixable}
            if not sc.items:
                self.rn_notice = Notice("info", f"分かれた濁点のある名前はありませんでした({sc.checked:,} 項目を調べました)")
            self.log.info("scan checked=%d found=%d", sc.checked, len(sc.items))
            self._emit(AREA_RENAME)

        ok = self._run("scan", work, done)
        self._emit(AREA_RENAME)
        return ok

    def set_selected(self, idx: int, on: bool) -> None:
        sc = self.rn_scan
        if sc is None or not (0 <= idx < len(sc.items)) or not sc.items[idx].fixable or sc.items[idx].result:
            return
        if on:
            self.rn_selected.add(idx)
        else:
            self.rn_selected.discard(idx)
        self._emit(AREA_RENAME)

    def selected_items(self) -> list[R.Item]:
        sc = self.rn_scan
        if sc is None:
            return []
        return [sc.items[i] for i in sorted(self.rn_selected) if sc.items[i].fixable and not sc.items[i].result]

    def rename_selected(self) -> bool:
        """FR-28: 画面の確認のあとに呼ぶ。前回の取り消しの記録はここで消える(M-15)。"""
        items = self.selected_items()
        if not items or self.busy():
            return False
        self.rn_done = []
        self.rn_undo_result = None

        def work(t: Task) -> R.RenameResult:
            return R.rename_items(items, t.cancelled, lambda d, tot: t.progress("rename", d, tot), rename=self._rename)

        def done(res: Any, err: BaseException | None) -> None:
            if err is not None:
                self._unexpected("rename", err)
                self.rn_notice = Notice("error", MSG_FAILED)
                self._record("rename", "error", None)
                self._emit(AREA_RENAME)
                return
            r: R.RenameResult = res
            self.rn_last = r
            self.rn_done = list(r.done)
            self.rn_selected = set()
            msg = f"{r.renamed:,} 件の名前を直しました"
            if r.failed or r.skipped:
                msg += f"(変えられなかった {r.failed + r.skipped:,} 件)"
            self.rn_notice = Notice("ok" if not (r.failed or r.skipped) else "warn", msg)
            self._record("rename", "ok" if r.renamed else "error", None, files=r.renamed,
                         skipped=r.failed + r.skipped, composed=r.renamed)
            self._emit(AREA_RENAME)

        ok = self._run("rename", work, done)
        self._emit(AREA_RENAME)
        return ok

    def can_undo(self) -> bool:
        return bool(self.rn_done) and not self.busy()

    def undo(self) -> bool:
        """FR-30: 直前の「直す」だけを逆の順に戻す。"""
        done_list = list(self.rn_done)
        if not done_list or self.busy():
            return False

        def work(t: Task) -> R.RenameResult:
            return R.undo(done_list, t.cancelled, rename=self._rename)

        def done(res: Any, err: BaseException | None) -> None:
            self.rn_done = []
            if err is not None:
                self._unexpected("undo", err)
                self.rn_notice = Notice("error", MSG_FAILED)
                self._record("undo", "error", None)
            else:
                r: R.RenameResult = res
                self.rn_undo_result = r
                msg = f"{r.renamed:,} 件を元の名前に戻しました"
                if r.failed or r.skipped:
                    msg += f"(戻せなかった {r.failed + r.skipped:,} 件)"
                self.rn_notice = Notice("ok" if not (r.failed or r.skipped) else "warn", msg)
                self._record("undo", "ok" if r.renamed else "error", None, files=r.renamed, skipped=r.failed + r.skipped)
                if self.rn_scan is not None:
                    for x in self.rn_scan.items:
                        if x.result == "直しました":
                            x.result = "元に戻しました"
            self._emit(AREA_RENAME)

        ok = self._run("undo", work, done)
        self._emit(AREA_RENAME)
        return ok

    # ================================================================ 設定
    def update_settings(self, mutate: Callable[[dict[str, Any]], None]) -> str | None:
        sec = dict(self.ctx.settings_dict())
        sec.pop("enabled", None)
        merged, _ = cfgmod.normalize(sec)
        mutate(merged)
        merged, _ = cfgmod.normalize(merged)
        try:
            self.ctx.write_settings(merged)
        except Exception as e:  # noqa: BLE001 - SettingsError などを画面に返す
            self.log.warning("settings write failed: %s", type(e).__name__)
            return "設定を保存できませんでした"
        old = self.config
        self.config = cfgmod.parse(merged)
        if (old.zip_compose, old.zip_skip_mac_files) != (self.config.zip_compose, self.config.zip_skip_mac_files) \
                and self.zip_key is not None and not self.busy():
            self.choose_zip(self.zip_key)  # 名前の規則が変わったら一覧を作り直す
        return None

    def set_option(self, key: str, value: Any) -> str | None:
        if key not in cfgmod.DEFAULTS:
            return None
        return self.update_settings(lambda s: s.__setitem__(key, value))

    # ================================================================ 利用状況・診断(FR-31・FR-32)
    def _count(self, days: int, pick: Callable[[dict[str, Any]], bool], today: date | None = None) -> list[int]:
        return count_jsonl(self.ops.path, days, pick, today=today or self._now().date())

    def usage(self, days: int) -> list[UsageSeries]:
        return [
            UsageSeries("fixed", "直した回数", self._count(days, oplog.is_fixed), unit="回", primary=True,
                        hint="公開から 90 日で 0 回なら紹介から外す(R-2)"),
            UsageSeries("text", "文字化けしたファイル", self._count(days, oplog.is_op("text")), unit="回", good_when="neutral"),
            UsageSeries("zip", "zip の展開", self._count(days, oplog.is_op("zip")), unit="回", good_when="neutral"),
            UsageSeries("rename", "名前の濁点", self._count(days, oplog.is_op("rename")), unit="回", good_when="neutral"),
        ]

    def today_fixed(self) -> int:
        return self._count(1, oplog.is_fixed)[0]

    def diagnostics(self) -> dict[str, str | int | bool]:
        c = self.config
        d: dict[str, str | int | bool] = {
            "busy": self.busy(),
            "last_ms": self.last_ms,
            "text_queue": len(self.text_queue),
            "undo_available": bool(self.rn_done),
            "text_output": c.text_output,
            "newline": c.newline,
            "text_compose": c.text_compose,
            "zip_compose": c.zip_compose,
            "zip_skip_mac_files": c.zip_skip_mac_files,
            "zip_max_total_gb": c.zip_max_total_gb,
            "rename_recursive": c.rename_recursive,
        }
        for k, v in sorted(self.counts.items()):
            d[f"count.{k}"] = v
        return d


def done_text(w: TF.WriteResult, opts: TF.Options) -> str:
    """FR-16:「書き出しました: <名前>(Excel で開ける形・CRLF・12,345 行)」。"""
    label = TF.OUTPUTS[opts.out][1].split("(")[0]
    nl = "" if opts.newline == "keep" else f"・{TF.NEWLINE_LABEL[opts.newline]}"
    where = "(ドキュメント\\MojiFix に置きました)" if w.fallback else ""
    return f"書き出しました: {w.path.name}({label}{nl}・{w.lines:,} 行){where}"
