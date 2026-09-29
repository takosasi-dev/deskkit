# PagePress モジュール本体。4つのタブ(まとめる・分ける・ページの整理・軽くする)の一覧と、ジョブのスレッド・描画スレッドを束ねる。
# 利用者が操作したときだけ動き、自動で動く処理は無い(§10: is_snoozed は見ない。完了は画面の行に出すだけで通知しない)。
# ログ・ops.jsonl・diagnostics・usage にファイル名・パス・PDF の中身を書かない(INV-5)。PDF のライブラリは最初に使うときに読む。
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Signal

from deskkit.catalog import info
from deskkit.modules.pagepress import config as cfgmod
from deskkit.modules.pagepress import jobs as J
from deskkit.modules.pagepress import naming, oplog, reader
from deskkit.modules.pagepress.build import ImageOpts
from deskkit.modules.pagepress.oplog import OpsLog
from deskkit.modules.pagepress.ranges import RangeError, Span
from deskkit.modules.pagepress.reader import Entry, Rejected
from deskkit.modules.pagepress.thumbs import ThumbCache
from deskkit.usage import UsageSeries

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

    from deskkit.modules.pagepress._win32 import ShellLinkApi
    from deskkit.modules.pagepress.organize_view import OrganizeState
    from deskkit.modules.pagepress.render import Renderer

LIST_TABS = ("merge", "split", "compress")
ROW_THUMB = 96
PAGE_THUMB = 160
AHEAD, BEHIND = 16, 8

MSG_FIELDS_SAME = "入力欄の名前が重なっています。片方に書くと、もう片方も同じ内容になります"
MSG_XFA = "この PDF の入力欄は特別な形式のため、新しいファイルでは使えないことがあります"
MSG_SIG = "新しいファイルでは電子署名が無効になります"
MSG_OVER_COUNT_OTHER = "一度に扱えるのは 200 件までです"
MSG_OVER_BYTES_ONE = "1GB を超える PDF は扱えません"
MSG_ORGANIZE_PDF = "ページの整理で開けるのは PDF だけです"


class Signals(QObject):
    lists_changed = Signal(str)       # タブ
    rejected_changed = Signal(str)
    probing_changed = Signal(str)
    job_changed = Signal()
    organize_changed = Signal()       # 整理の文書を開いた・閉じた
    row_thumb = Signal(object)        # キー
    page_thumb = Signal(int)          # 元のページ番号
    settings_changed = Signal()


@dataclass
class Notes:
    """投入エリアの下に出す、足さなかった物(FR-1・FR-2)。"""

    rejected: list[Rejected]
    unsupported: int = 0


RendererFactory = Callable[[Callable[[Callable[[], None]], None]], "Renderer"]


class PagePressModule:
    def __init__(self, ctx: Any, *, renderer_factory: RendererFactory | None = None,
                 fallback_dir: Callable[[], Path] | None = None, disk_free: Callable[[Path], int] | None = None,
                 shell_link: ShellLinkApi | None = None, sendto_folder: Path | None = None) -> None:
        self.ctx = ctx
        self._shell_link = shell_link
        self._sendto_folder = sendto_folder
        self.log: logging.Logger = ctx.log
        reader.quiet_pypdf_logging()  # ロガー pypdf の文を DeskKit のログへ流さない(INV-5。pypdf は import しない)
        section = dict(ctx.settings_dict())
        section.pop("enabled", None)
        merged, changed = cfgmod.normalize(section)
        self.config = cfgmod.parse(merged)
        if changed:
            try:
                ctx.write_settings(merged)
            except Exception as e:  # noqa: BLE001 - 書き戻せなくても既定値で動く
                self.log.warning("settings write-back failed: %s", type(e).__name__)
        self.signals = Signals()
        data = Path(ctx.data_dir)
        self.ops = OpsLog(data / "ops.jsonl")
        self.pending = naming.Pending(data / "pending.json")
        env_kw: dict[str, Any] = {}
        if fallback_dir is not None:
            env_kw["fallback_dir"] = fallback_dir
        if disk_free is not None:
            env_kw["disk_free"] = disk_free
        self.env = J.Env(self.ops, self.pending, self.log, on_update=self._on_job_update, **env_kw)
        self.worker = J.Worker(self.log)
        self._renderer_factory = renderer_factory
        self._renderer: Renderer | None = None
        self.thumbs = ThumbCache()
        self._row_wanted: set[Any] = set()
        self.lists: dict[str, list[Entry]] = {t: [] for t in LIST_TABS}
        self.notes: dict[str, Notes] = {t: Notes([]) for t in (*LIST_TABS, "organize")}
        self.probing: dict[str, int] = {t: 0 for t in (*LIST_TABS, "organize")}
        self.organize_entry: Entry | None = None
        self.organize_state: OrganizeState | None = None
        self.organize_doc = 0
        self.job: J.JobStatus | None = None
        self.job_tab = ""
        self.cancel_slow = False
        self.results: dict[str, list[J.Row]] = {t: [] for t in (*LIST_TABS, "organize")}
        self.job_counts: dict[str, int] = {}
        self._counted: J.JobStatus | None = None
        self._org_saving: tuple[int, list[tuple[int, int]]] | None = None
        self._next_id = 1
        self._stopped = False

    # ================================================================ ライフサイクル
    @property
    def accent(self) -> str:
        return info("pagepress").accent

    def start(self) -> None:
        swept = self.pending.sweep()  # FR-21: 前回の書きかけ(~pagepress-*.tmp)だけを消す
        self.ctx.add_tray_action("PDF をまとめる・分ける", self.ctx.show_page)
        self._update_status()
        self.log.info("pagepress started swept=%d", swept)

    def stop(self) -> None:
        self._stopped = True
        st = self.job
        if st is not None and not st.finished:
            st.cancel.set()  # 書いている途中なら、一時ファイルはジョブの後始末が消す
        self.worker.stop(5.0)
        if self._renderer is not None:
            self._renderer.stop(5.0)
        self.log.info("pagepress stopped")

    def handle_cli(self, args: list[str]) -> tuple[int, str]:
        if args[:1] == ["open"]:
            n = self.add_paths("merge", args[1:])
            self.ctx.show_page()
            return 0, f"queued {n}"
        return 2, "unsupported"

    def create_page(self) -> QWidget:
        from deskkit.modules.pagepress.page import PagePressPage

        return PagePressPage(self)

    # ================================================================ 描画スレッド
    @property
    def renderer(self) -> Renderer:
        if self._renderer is None:
            if self._renderer_factory is not None:
                self._renderer = self._renderer_factory(self.ctx.call_soon)
            else:
                from deskkit.modules.pagepress.render import Renderer

                self._renderer = Renderer(self.ctx.call_soon)
        return self._renderer

    def row_thumb(self, e: Entry) -> tuple[bool, Any]:
        """一覧の行のサムネイル。無ければ描画スレッドに頼み、できたら signals.row_thumb を出す。"""
        key = ("row", str(e.path), e.mtime_ns)
        hit, img = self.thumbs.get(key)
        if hit:
            return True, img
        if key not in self._row_wanted and not self._stopped:
            self._row_wanted.add(key)

            def done(img: Any, key: Any = key) -> None:
                self._row_wanted.discard(key)
                self.thumbs.put(key, img)
                self.signals.row_thumb.emit(key)

            self.renderer.request_first(key, e.path, e.kind, ROW_THUMB, done)
        return False, None

    def page_thumb(self, orig: int) -> tuple[bool, Any]:
        return self.thumbs.get(("page", self.organize_doc, orig))

    def request_pages(self, first: int, last: int) -> list[int]:
        """見えている行 first..last と後ろ 16・前 8 を、見えている分から順に頼む。頼んだ元のページ番号を返す。"""
        st = self.organize_state
        if st is None or self.organize_entry is None or self._stopped:
            return []
        n = len(st.items)
        order = list(range(first, last + 1)) + list(range(last + 1, min(n, last + 1 + AHEAD))) + \
            list(range(first - 1, max(-1, first - 1 - BEHIND), -1))
        doc = self.organize_doc
        want: list[int] = []
        seen: set[int] = set()
        for row in order:
            if 0 <= row < n:
                orig = st.items[row][0]
                if orig not in seen and ("page", doc, orig) not in self.thumbs:
                    seen.add(orig)
                    want.append(orig)

        def done(orig: int, img: Any, doc: int = doc) -> None:
            if doc != self.organize_doc:
                return
            self.thumbs.put(("page", doc, orig), img)
            self.signals.page_thumb.emit(orig)

        self.renderer.want_pages(doc, want, PAGE_THUMB, done)
        return want

    # ================================================================ 投入(FR-1・FR-2・FR-23)
    def add_paths(self, tab: str, paths: Sequence[Any]) -> int:
        """ファイル・フォルダを確かめる列に積む。積んだ数を返す(結果はあとで一覧と notes に出る)。"""
        files, unsupported = reader.expand(list(paths))
        if tab != "merge":
            pdfs = [f for f in files if reader.kind_of(f) == "pdf"]
            unsupported += len(files) - len(pdfs)
            files = pdfs[:1] if tab == "organize" else pdfs
        over: list[Rejected] = []
        if tab in LIST_TABS:
            room = max(0, J.MAX_ITEMS - len(self.lists[tab]) - self.probing[tab])
            msg = J.MSG_OVER_COUNT if tab == "merge" else MSG_OVER_COUNT_OTHER
            over = [Rejected(f, "too_many", msg) for f in files[room:]]
            files = files[:room]
        self.notes[tab] = Notes(over, unsupported)
        self.signals.rejected_changed.emit(tab)
        if not files:
            return 0
        ids = list(range(self._next_id, self._next_id + len(files)))
        self._next_id += len(files)
        pairs = list(zip(ids, files, strict=True))

        def task() -> None:
            out: list[Entry | Rejected] = []
            for eid, f in pairs:
                if self._stopped:
                    break
                try:
                    out.append(reader.probe(eid, f))
                except reader.ProbeError as ex:
                    out.append(Rejected(f, ex.code, ex.message))
                except Exception as ex:  # noqa: BLE001 - 想定外は「読めない」
                    self.log.error("probe failed: %s", type(ex).__name__)
                    out.append(Rejected(f, "unreadable", reader.MSG_UNREADABLE))
            self.ctx.call_soon(lambda: self._apply_probe(tab, len(pairs), out))

        self.probing[tab] += len(files)
        if not self.worker.submit(task):
            self.probing[tab] -= len(files)
            return 0
        self.signals.probing_changed.emit(tab)
        self.log.info("add tab=%s files=%d unsupported=%d over=%d", tab, len(files), unsupported, len(over))
        return len(files)

    def _apply_probe(self, tab: str, count: int, results: list[Entry | Rejected]) -> None:
        if self._stopped:
            return
        self.probing[tab] = max(0, self.probing[tab] - count)
        notes = self.notes[tab]
        accepted = 0
        for r in results:
            if isinstance(r, Rejected):
                notes.rejected.append(r)
            elif tab == "organize":
                if r.size > J.MAX_TOTAL_BYTES:
                    notes.rejected.append(Rejected(r.path, "too_large", MSG_OVER_BYTES_ONE))
                    continue
                self._open_organize(r)
                accepted += 1
            else:
                lst = self.lists[tab]
                if len(lst) >= J.MAX_ITEMS:
                    notes.rejected.append(Rejected(r.path, "too_many",
                                                   J.MSG_OVER_COUNT if tab == "merge" else MSG_OVER_COUNT_OTHER))
                elif tab == "merge" and sum(e.size for e in lst) + r.size > J.MAX_TOTAL_BYTES:
                    notes.rejected.append(Rejected(r.path, "too_large", J.MSG_OVER_BYTES))
                elif tab != "merge" and r.size > J.MAX_TOTAL_BYTES:
                    notes.rejected.append(Rejected(r.path, "too_large", MSG_OVER_BYTES_ONE))
                else:
                    lst.append(r)
                    accepted += 1
        codes: dict[str, int] = {}
        for rj in notes.rejected:
            codes[rj.code] = codes.get(rj.code, 0) + 1
        self.log.info("probe tab=%s accepted=%d rejected=%s", tab, accepted,
                      ",".join(f"{k}:{v}" for k, v in sorted(codes.items())) or "0")
        if tab in LIST_TABS and accepted and not self.busy():
            self.results[tab] = []
        self.signals.probing_changed.emit(tab)
        self.signals.rejected_changed.emit(tab)
        if tab in LIST_TABS:
            self.signals.lists_changed.emit(tab)
        self._update_status()

    # ================================================================ 一覧の操作(FR-3)
    def remove(self, tab: str, index: int) -> None:
        lst = self.lists[tab]
        if 0 <= index < len(lst):
            del lst[index]  # 一覧から外すだけ(ファイルは消さない)
            self.signals.lists_changed.emit(tab)

    def move(self, tab: str, index: int, delta: int) -> None:
        lst = self.lists[tab]
        j = index + delta
        if 0 <= index < len(lst) and 0 <= j < len(lst):
            lst[index], lst[j] = lst[j], lst[index]
            self.signals.lists_changed.emit(tab)

    def reorder(self, tab: str, ids: Sequence[int]) -> None:
        """画面のドラッグの結果(行の id の並び)をそのまま一覧の順にする。"""
        cur = self.lists[tab]
        if sorted(e.id for e in cur) == sorted(ids):
            by_id = {e.id: e for e in cur}
            self.lists[tab] = [by_id[i] for i in ids]
            self.signals.lists_changed.emit(tab)

    def sort_by_name(self, tab: str) -> None:
        self.lists[tab].sort(key=lambda e: reader.natural_key(e.path.name))
        self.signals.lists_changed.emit(tab)

    def clear(self, tab: str) -> None:
        self.lists[tab] = []
        self.results[tab] = []
        self.notes[tab] = Notes([])
        self.signals.lists_changed.emit(tab)
        self.signals.rejected_changed.emit(tab)

    def total_pages(self, tab: str) -> int:
        return sum(e.pages for e in self.lists[tab])

    def has_images(self) -> bool:
        return any(e.kind == "image" for e in self.lists["merge"])

    # ================================================================ 整理(FR-7〜FR-9)
    def _open_organize(self, e: Entry) -> None:
        from deskkit.modules.pagepress.organize_view import OrganizeState

        old = self.organize_doc
        if self.organize_entry is not None and self._renderer is not None:
            self._renderer.close_doc(old)
        self.thumbs.drop(lambda k: isinstance(k, tuple) and k[:2] == ("page", old))
        self.organize_doc = old + 1
        self.organize_entry = e
        self.organize_state = OrganizeState(e.pages)
        self.results["organize"] = []
        self.renderer.open_doc(self.organize_doc, e.path)
        self.signals.organize_changed.emit()

    def close_organize(self) -> None:
        if self.organize_entry is None:
            return
        old = self.organize_doc
        if self._renderer is not None:
            self._renderer.close_doc(old)
        self.thumbs.drop(lambda k: isinstance(k, tuple) and k[:2] == ("page", old))
        self.organize_entry = None
        self.organize_state = None
        self.results["organize"] = []
        self.signals.organize_changed.emit()

    def organize_dirty(self) -> bool:
        return self.organize_state is not None and self.organize_state.dirty

    # ================================================================ ジョブ(FR-4・FR-5・FR-9・FR-10・FR-13〜FR-15)
    def busy(self) -> bool:
        return self.job is not None and not self.job.finished

    def warnings(self, tab: str) -> list[str]:
        """P-6 の確認に出す文(書く前)。"""
        entries: list[Entry]
        if tab == "organize":
            entries = [self.organize_entry] if self.organize_entry is not None else []
        else:
            entries = list(self.lists[tab])
        out: list[str] = []
        if tab == "merge":
            owner: dict[str, int] = {}
            dup = False
            for k, e in enumerate(entries):
                for n in e.field_names:
                    if owner.setdefault(n, k) != k:
                        dup = True
            if dup:
                out.append(MSG_FIELDS_SAME)
        if any(e.has_xfa for e in entries):
            out.append(MSG_XFA)
        if any(e.has_signed_sig for e in entries):
            out.append(MSG_SIG)
        return out

    def split_count(self, spans: list[Span] | None) -> int:
        """「N 個のファイルができます」の N(範囲がページ数を超える PDF は数えない)。"""
        from deskkit.modules.pagepress import ranges

        n = 0
        for e in self.lists["split"]:
            if self.config.split_mode == "single":
                n += e.pages
            elif self.config.split_mode == "every":
                n += ranges.count_every(e.pages, self.config.split_every)
            elif spans:
                try:
                    for s in spans:
                        s.resolve(e.pages)
                except RangeError:
                    continue
                n += len(spans)
        return n

    def start_job(self, tab: str, *, spans: list[Span] | None = None) -> str | None:
        """ジョブを始める。始められなければ画面に出す文。確認(P-6)は画面が先に済ませる。"""
        if self.busy():
            return J.MSG_BUSY
        spec: J.JobSpec
        if tab == "organize":
            if self.organize_entry is None or self.organize_state is None or not self.organize_state.items:
                return "保存するページがありません"
            spec = J.JobSpec("organize", [self.organize_entry], items=list(self.organize_state.items))
            self._org_saving = (self.organize_doc, list(spec.items))
        else:
            entries = list(self.lists[tab])
            if not entries:
                return "ファイルを入れてください"
            if tab == "merge":
                if sum(e.pages for e in entries) > J.MAX_PAGES:
                    return J.MSG_TOO_MANY_PAGES
                c = self.config
                spec = J.JobSpec("merge", entries, opts=ImageOpts(c.image_fit, c.paper, c.margin_mm))
            elif tab == "split":
                if self.config.split_mode == "ranges" and not spans:
                    return "範囲を書いてください"
                spec = J.JobSpec("split", entries, split_mode=self.config.split_mode, spans=list(spans or []),
                                 every=self.config.split_every)
            else:
                spec = J.JobSpec("compress", entries, level=self.config.compress_level)
        st = J.JobStatus(spec.op)
        self.job, self.job_tab, self.cancel_slow = st, tab, False
        self.results[tab] = []

        def work() -> None:
            J.run(spec, st, self.env)

        if not self.worker.submit(work):
            self.job = None
            return J.MSG_FAILED
        self.log.info("job start op=%s inputs=%d", spec.op, len(spec.entries))
        self.signals.job_changed.emit()
        self._update_status()
        return None

    def cancel_job(self) -> None:
        st = self.job
        if st is None or st.finished:
            return
        st.cancel.set()
        self.log.info("job cancel requested op=%s", st.op)
        self.ctx.start_timer(1000, self._cancel_timeout, single_shot=True)

    def _cancel_timeout(self) -> None:
        st = self.job
        if st is not None and not st.finished and st.cancel.is_set():
            self.cancel_slow = True  # 1 秒で戻らない: 「中止しています」(FR-15)
            self.signals.job_changed.emit()

    def _on_job_update(self, _st: J.JobStatus) -> None:  # ジョブのスレッドから
        self.ctx.call_soon(self._job_update)

    def _job_update(self) -> None:
        if self._stopped:
            return
        st = self.job
        if st is not None and st.finished and self._counted is not st:
            self._counted = st
            self.results[self.job_tab] = list(st.rows)
            self.job_counts[st.state] = self.job_counts.get(st.state, 0) + 1
            saving = self._org_saving
            if (self.job_tab == "organize" and st.state == J.STATE_DONE and self.organize_state is not None
                    and saving is not None and saving[0] == self.organize_doc):
                self.organize_state.mark_saved(saving[1])
            self.cancel_slow = False
        self.signals.job_changed.emit()
        self._update_status()

    def job_text(self) -> str:
        st = self.job
        if st is None:
            return ""
        if not st.finished and self.cancel_slow:
            return J.MSG_CANCELLING
        if st.finished:
            return st.message
        return st.progress_text()

    def _update_status(self) -> None:
        try:
            self.ctx.set_tray_status("作業中" if self.busy() else "待機中")
        except Exception:  # noqa: BLE001 - 状態表示の失敗で処理を止めない
            pass

    # ================================================================ 設定(FR-11)
    def set_option(self, key: str, value: Any) -> str | None:
        sec = dict(self.ctx.settings_dict())
        sec.pop("enabled", None)
        merged, _ = cfgmod.normalize(sec)
        merged[key] = value
        merged, _ = cfgmod.normalize(merged)
        if merged == cfgmod.normalize(sec)[0]:
            self.config = cfgmod.parse(merged)
            return None
        try:
            self.ctx.write_settings(merged)
        except Exception as e:  # noqa: BLE001
            self.log.warning("settings write failed: %s", type(e).__name__)
            return "設定を保存できませんでした"
        self.config = cfgmod.parse(merged)
        self.signals.settings_changed.emit()
        return None

    # ---- 「送る」(Q-2 の回答)
    def _link_api(self) -> ShellLinkApi:
        if self._shell_link is None:
            from deskkit.modules.pagepress._win32 import RealShellLink

            self._shell_link = RealShellLink()
        return self._shell_link

    def sendto_folder(self) -> Path:
        from deskkit.modules.pagepress import sendto

        return self._sendto_folder or sendto.default_folder()

    def sendto_status(self) -> str:
        from deskkit.modules.pagepress import sendto

        try:
            return sendto.status(self._link_api(), self.sendto_folder())
        except OSError:
            return sendto.STATUS_ABSENT

    def set_sendto(self, enabled: bool) -> str | None:
        """オンでショートカットを作り、オフで自分が作った物だけを消す。問題があれば画面に出す文。"""
        from deskkit.modules.pagepress import sendto

        api, folder = self._link_api(), self.sendto_folder()
        if enabled:
            err = sendto.register(api, folder)
            if err:
                self.log.info("sendto register failed")
                return err
        else:
            r = sendto.unregister(api, folder)
            self.log.info("sendto unregister result=%s", r)
            if r == "failed":
                return "「送る」から外せませんでした"
        self.log.info("sendto enabled=%s", enabled)
        return self.set_option("sendto_enabled", bool(enabled))

    # ================================================================ 開く(FR-20)
    def open_path(self, p: Path | None) -> bool:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        if p is None or not p.exists():
            return False
        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(p))))

    # ================================================================ 利用状況・診断(FR-25・FR-26)
    def usage(self, days: int) -> list[UsageSeries]:
        made = self.ops.per_day(days, oplog.made_pdfs)
        light = self.ops.per_day(days, oplog.compressed)
        return [
            UsageSeries("made", "作った PDF", made, unit="件", primary=True,
                        hint="公開から 90 日で 0 件なら紹介から外す(R-2)"),
            UsageSeries("compressed", "軽くした件数", light, unit="件", good_when="neutral"),
        ]

    def diagnostics(self) -> dict[str, str | int | bool]:
        r = self._renderer
        out: dict[str, str | int | bool] = {
            "pypdf": reader.pypdf_version(),
            "pdfium": r.pdfium_version if r is not None else "not_loaded",
            "renderer": r.state if r is not None else "idle",
            "thumbs": len(self.thumbs),
            "jobs": sum(self.job_counts.values()),
            "busy": self.busy(),
            "pypdf_warnings": reader.pypdf_warning_count(),
            "pending": self.pending.count(),
            "merge_items": len(self.lists["merge"]),
            "split_items": len(self.lists["split"]),
            "compress_items": len(self.lists["compress"]),
            "organize_open": self.organize_entry is not None,
            "sendto": self.sendto_status(),
        }
        for k, v in sorted(self.job_counts.items()):
            out[f"jobs_{k}"] = v
        return out
