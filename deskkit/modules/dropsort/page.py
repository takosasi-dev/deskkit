# Control Center の DropSort 画面: 状態(ヒーロー・数値タイル)、ルール編集(実績・本番化の提案・試し当て)、
# 既存ファイルの整理、試運転の結果、操作履歴と元に戻す、要確認の一覧、アーカイブ設定、詳細設定(モード連携など)。
# ファイルを開く・実行する・エクスプローラーで表示する操作は置かない(INV-1)。パスは文字で見せるだけ。
from __future__ import annotations

import ntpath
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from deskkit import catalog
from deskkit.modules.dropsort import paths as dpaths
from deskkit.modules.dropsort import stats as dstats
from deskkit.modules.dropsort import template as tpl
from deskkit.modules.dropsort.config import TEMPLATES, ZONE_NAMES, ConfigError, new_rule, norm_ext, parse_rule
from deskkit.modules.dropsort.rules import describe, fmt_size, fmt_size_exact, parse_size
from deskkit.modules.dropsort.service import OP_TEXT, BatchResult, reason_text
from deskkit.modules.dropsort.stats import RuleStats
from deskkit.modules.dropsort.tester import make_motw, simulate
from deskkit.ui import theme as T
from deskkit.ui import widgets as W
from deskkit.ui.theme import G

if TYPE_CHECKING:
    from deskkit.modules.dropsort.module import DropSortModule



def _acc() -> str:
    """DropSort のアクセント色。テーマ切替後の値を使うため、毎回 catalog から読む(§8)。"""
    return catalog.info("dropsort").accent


def _op_color(op: str) -> str | None:
    """判定ごとの文字色(実行時に theme から読む)。"""
    return {
        "move": T.SUCCESS, "would_move": T.SUCCESS, "archive": T.INFO, "would_archive": T.INFO,
        "refused": T.DANGER, "would_refuse": T.DANGER, "failed": T.DANGER, "flagged": T.WARN,
        "undo": T.ACCENT_2, "skip": T.TEXT_MUTE, "no_match": T.TEXT_MUTE,
    }.get(op)


# ------------------------------------------------------------------ 小物
def _short_ts(ts: Any) -> str:
    s = str(ts or "")
    try:
        return datetime.fromisoformat(s).strftime("%m/%d %H:%M:%S")
    except ValueError:
        return s


def _motw_text(r: dict[str, Any]) -> str:
    if r.get("motw") is None:
        return ""
    if not r.get("motw"):
        return "なし"
    z = r.get("zone_id")
    return f"あり({ZONE_NAMES.get(z, z) if z is not None else '不明'})"


def make_table(headers: Sequence[str], stretch: int = -1, min_height: int = 300) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(list(headers))
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setAlternatingRowColors(True)
    t.setShowGrid(False)
    t.setWordWrap(False)
    t.setMinimumHeight(min_height)
    h = t.horizontalHeader()
    h.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    h.setSectionResizeMode(len(headers) - 1, QHeaderView.ResizeMode.Stretch)
    if 0 <= stretch < len(headers) - 1:
        # パスの列は長くなりがちなので、幅を固定して(ドラッグで変更可)全文はツールチップで見せる
        h.setSectionResizeMode(stretch, QHeaderView.ResizeMode.Interactive)
        h.resizeSection(stretch, 280)
    h.setHighlightSections(False)
    t.verticalHeader().setDefaultSectionSize(30)
    return t


def fill_table(t: QTableWidget, rows: Sequence[Sequence[str]], colors: Sequence[str | None] = (),
               tips: Sequence[str | None] = ()) -> None:
    t.setUpdatesEnabled(False)
    t.setRowCount(len(rows))
    for i, row in enumerate(rows):
        for j, text in enumerate(row):
            it = QTableWidgetItem(str(text))
            if j == 0 and i < len(colors) and colors[i]:
                it.setForeground(QColor(str(colors[i])))
            if i < len(tips) and tips[i]:
                it.setToolTip(str(tips[i]))
            t.setItem(i, j, it)
    t.setUpdatesEnabled(True)


def record_rows(items: Sequence[dict[str, Any]], with_time: bool = True) -> tuple[list[list[str]], list[str | None], list[str | None]]:
    rows: list[list[str]] = []
    colors: list[str | None] = []
    tips: list[str | None] = []
    for r in items:
        op = str(r.get("op"))
        name = r.get("name") or ntpath.basename(str(r.get("src") or ""))
        dst = str(r.get("dst") or "")
        rule = str(r.get("rule") or ("アーカイブ" if "archive" in op else ""))
        row = [OP_TEXT.get(op, op)] + ([_short_ts(r.get("ts"))] if with_time else []) + [
            str(name), rule, dst, _motw_text(r), reason_text(r.get("reason"))]
        rows.append(row)
        colors.append(_op_color(op))
        tips.append(f"{r.get('src') or ''}\n→ {dst}" if dst else str(r.get("src") or ""))
    return rows, colors, tips


REC_HEADERS = ["判定", "日時", "ファイル名", "ルール", "行き先", "MOTW", "理由"]
BATCH_HEADERS = ["判定", "ファイル名", "ルール", "行き先", "MOTW", "理由"]


class RecordsDialog(W.StyledDialog):
    """結果の一覧を出すだけのダイアログ(ファイルを開く操作は置かない)。"""

    def __init__(self, parent: QWidget | None, title: str, items: Sequence[dict[str, Any]], *, with_time: bool,
                 note: str | None = None, on_open_page: Callable[[], None] | None = None) -> None:
        super().__init__(parent, title, G.LIST, _acc(), width=860)
        if note:
            self.body.addWidget(W.label(note, "Dim", wrap=True))
        t = make_table(REC_HEADERS if with_time else BATCH_HEADERS, stretch=4 if with_time else 3, min_height=360)
        rows, colors, tips = record_rows(items, with_time)
        fill_table(t, rows, colors, tips)
        if not rows:
            self.body.addWidget(W.EmptyState(G.LIST, "まだ記録がありません", "該当するファイルが来ると、ここに並びます。"))
        else:
            self.body.addWidget(t)
        if on_open_page is not None:
            def go() -> None:
                self.accept()
                try:
                    on_open_page()
                except Exception:  # noqa: BLE001 - 画面が開けなくてもダイアログは閉じる
                    pass

            self.buttons.addWidget(W.button("画面で続ける", "ghost", G.OPEN, on_click=go))
        self.buttons.addWidget(W.button("閉じる", "primary", on_click=self.accept))


class DryRunDialog(RecordsDialog):
    """トレイの「試運転の結果を見る」。"""

    def __init__(self, module: DropSortModule, parent: QWidget | None = None) -> None:
        items = list(reversed(module.service.dryrun.tail(300)))
        super().__init__(parent, "試運転の結果", items, with_time=True,
                         note="試運転(dry-run)のルールが「動かすなら こうする」と判定した記録です。ファイルはまだ動いていません。",
                         on_open_page=lambda: module.open_page("dryrun"))


# ------------------------------------------------------------------ ルール編集ダイアログ
class RuleEditDialog(W.StyledDialog):
    def __init__(self, page: DropSortPage, rule: dict[str, Any], existing_names: set[str], title: str) -> None:
        super().__init__(page.window() if page.isVisible() else None, title, G.FILTER, _acc(), width=620)
        self._page = page
        self._m = page.m
        self._names = existing_names
        self.result_rule: dict[str, Any] | None = None
        m = rule.get("match") or {}

        self.name = QLineEdit(str(rule.get("name") or ""))
        self.name.setPlaceholderText("例: PDF")
        self.mode = W.Segmented([("dry-run", "試運転"), ("apply", "本番")], str(rule.get("mode") or "dry-run"), _acc())
        self.body.addLayout(self._row("ルール名", self.name, self.mode))

        self.body.addWidget(W.label("条件(すべて満たしたときに一致。空欄は「問わない」)", "Eyebrow"))
        ext = m.get("ext") or []
        self.ext = QLineEdit(" ".join(ext if isinstance(ext, list) else [str(ext)]))
        self.ext.setPlaceholderText("拡張子 例: .pdf .docx(空白かカンマ区切り)")
        self.ext.textChanged.connect(page.ctx.safe(self._check_dest, "dropsort:ext"))
        self.body.addLayout(self._row("拡張子", self.ext))
        self.regex = QLineEdit(str(m.get("name_regex") or ""))
        self.regex.setPlaceholderText("ファイル名の正規表現(部分一致) 例: ^invoice_")
        self.regex_state = W.label("", "Mute")
        self.regex.textChanged.connect(page.ctx.safe(self._check_regex, "dropsort:regex"))
        self.body.addLayout(self._row("名前", self.regex, self.regex_state))
        self.smin = QLineEdit(fmt_size_exact(m.get("size_min")))
        self.smin.setPlaceholderText("最小 例: 1MB")
        self.smax = QLineEdit(fmt_size_exact(m.get("size_max")))
        self.smax.setPlaceholderText("最大 例: 2GB")
        self.body.addLayout(self._row("サイズ", self.smin, W.label("〜", "Dim"), self.smax))
        mv = m.get("motw")
        self.motw = W.Segmented([("any", "問わない"), ("yes", "MOTW あり"), ("no", "MOTW なし")],
                                "any" if mv is None else ("yes" if mv else "no"), _acc())
        self.body.addLayout(self._row("入手元の印", self.motw, None))
        zids = set(m.get("zone_ids") or [])
        self.zone_boxes: list[tuple[int, QCheckBox]] = []
        zrow: list[QWidget | None] = []
        for z, zn in ZONE_NAMES.items():
            cb = QCheckBox(f"{z} {zn}")
            cb.setChecked(z in zids)
            self.zone_boxes.append((z, cb))
            zrow.append(cb)
        self.body.addLayout(self._row("ゾーン", *zrow, None))
        hd = m.get("host_domain") or []
        self.domain = QLineEdit(" ".join(hd if isinstance(hd, list) else [str(hd)]))
        self.domain.setPlaceholderText("入手元ドメイン 例: github.com(サブドメインも一致)")
        self.body.addLayout(self._row("ドメイン", self.domain))
        self.body.addWidget(W.label("ドメイン条件は Zone.Identifier に入手元が記録されている場合だけ効きます。"
                                    "記録・表示するのはドメイン名だけです。", "Mute", wrap=True))

        self.body.addWidget(W.label("移動先", "Eyebrow"))
        self.dest = QLineEdit(str(rule.get("dest") or ""))
        self.dest.setPlaceholderText("移動先フォルダ(ドライブ文字から始まるパス)")
        self.dest.textChanged.connect(page.ctx.safe(self._check_dest, "dropsort:dest"))
        browse = W.button("参照…", "secondary", G.FOLDER, on_click=page.ctx.safe(self._browse, "dropsort:browse"))
        self.mk = W.button("フォルダを作成", "secondary", G.ADD, on_click=page.ctx.safe(self._mkdir, "dropsort:mkdir"))
        self.body.addLayout(W.hbox(self.dest, browse, self.mk))
        self.dest_state = W.label("", "Mute", wrap=True)
        self.body.addWidget(self.dest_state)
        self.dest_preview = W.label("", "Dim", wrap=True)
        self.dest_preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.dest_preview.setVisible(False)
        self.body.addWidget(self.dest_preview)
        self.body.addWidget(W.label("移動先に {yyyy} {mm} {dd}(ファイルが来た日)・{ext}(拡張子)・{domain}(入手元。分からなければ "
                                    f"{tpl.UNKNOWN_DOMAIN})を書くと、その前のフォルダの下に自動でフォルダを作って分けます"
                                    "(例: D:\\整理\\{yyyy}\\{mm})。その前のフォルダ自体は自動では作りません。", "Mute", wrap=True))
        self.err = W.label("", wrap=True)
        self.err.setStyleSheet(f"color: {T.DANGER};")
        self.err.setVisible(False)
        self.body.addWidget(self.err)
        self.buttons.addWidget(W.button("キャンセル", "ghost", on_click=self.reject))
        self.buttons.addWidget(W.button("保存", "primary", G.SAVE, on_click=page.ctx.safe(self._save, "dropsort:rule-save")))
        self._check_regex()
        self._check_dest()

    def _row(self, title: str, *items: QWidget | None) -> QHBoxLayout:
        lb = W.label(title, "Dim")
        lb.setFixedWidth(84)
        return W.hbox(lb, *items)

    def _check_regex(self, *_: Any) -> None:
        import re

        t = self.regex.text()
        if not t:
            self.regex_state.setText("")
            return
        try:
            re.compile(t)
            self.regex_state.setText("OK")
            self.regex_state.setStyleSheet(f"color: {T.SUCCESS};")
        except re.error as e:
            self.regex_state.setText(f"不正: {e.msg}")
            self.regex_state.setStyleSheet(f"color: {T.DANGER};")

    def _target(self) -> str:
        """検査と「フォルダを作成」の対象: テンプレートならその前のフォルダ(基準フォルダ)。"""
        d = self.dest.text().strip()
        return tpl.base(d) if tpl.has_placeholder(d) else d

    def _check_dest(self, *_: Any) -> None:
        d = self.dest.text().strip()
        self.mk.setVisible(False)
        self.dest_preview.setVisible(False)
        if not d:
            self.dest_state.setText("「参照…」でフォルダを選んでください。")
            self.dest_state.setStyleSheet(f"color: {T.TEXT_MUTE};")
            return
        prefix = ""
        if tpl.has_placeholder(d):
            err = tpl.validate(d)
            if err is not None:
                self.dest_state.setText(err)
                self.dest_state.setStyleSheet(f"color: {T.DANGER};")
                return
            exts = [norm_ext(x) for x in self.ext.text().replace(",", " ").split()]
            ext = next((e for e in exts if e), ".pdf")
            self.dest_preview.setText(f"例: {tpl.example(d, when=time.time(), ext=ext)}"
                                      f"(今日来た example{ext}・入手元 example.com の場合)")
            self.dest_preview.setVisible(True)
            prefix = f"基準フォルダ {tpl.base(d)}: "
        target = self._target()
        svc = self._m.service
        c = dpaths.check_dest(svc.api, target, svc.downloads, self._m.cfg.archive.dir_name)
        if c.ok:
            fs = c.volume.fs_name if c.volume else "?"
            ns = bool(c.volume and c.volume.named_streams)
            msg = f"{prefix}OK — {fs}" + ("" if ns else "(代替データストリーム非対応: MOTW 付きのファイルは移動を拒否します)")
            self.dest_state.setText(msg)
            self.dest_state.setStyleSheet(f"color: {T.SUCCESS if ns else T.WARN};")
        else:
            self.dest_state.setText(prefix + (c.message or "使えません"))
            self.dest_state.setStyleSheet(f"color: {T.DANGER if c.code == 'network_dest' else T.WARN};")
            self.mk.setVisible(c.code == "dest_missing" and ntpath.isabs(target) and len(target) > 3 and target[1] == ":")

    def _browse(self) -> None:
        start = self.dest.text().strip() or (self._m.service.downloads or "")
        d = QFileDialog.getExistingDirectory(self, "移動先フォルダを選ぶ", start, QFileDialog.Option.ShowDirsOnly)
        if d:
            self.dest.setText(ntpath.normpath(d))

    def _mkdir(self) -> None:
        d = ntpath.normpath(self._target())
        ok, _ = W.confirm(self, "フォルダを作成", f"次のフォルダを作成します。\n{d}", ok_text="作成")
        if not ok:
            return
        e = self._m.create_folder(d)
        if e != 0:
            W.message(self, "作成できません", f"フォルダを作成できませんでした(Win32 エラー {e})。", kind="error")
        self._check_dest()

    def _fail(self, msg: str) -> None:
        self.err.setText(msg)
        self.err.setVisible(True)

    def _save(self) -> None:
        name = self.name.text().strip()
        if not name:
            return self._fail("ルール名を入れてください。")
        if name in self._names:
            return self._fail("同じ名前のルールがあります。")
        exts = [norm_ext(x) for x in self.ext.text().replace(",", " ").split()]
        exts = [e for e in exts if e]
        try:
            smin = parse_size(self.smin.text())
            smax = parse_size(self.smax.text())
        except ValueError:
            return self._fail("サイズは 1024 / 10KB / 5MB / 1.5GB のように入れてください。")
        mv = self.motw.value()
        zids = [z for z, cb in self.zone_boxes if cb.isChecked()]
        doms = [x.strip().lower() for x in self.domain.text().replace(",", " ").split() if x.strip()]
        rule: dict[str, Any] = {
            "name": name, "mode": self.mode.value(),
            "match": {"ext": exts or None, "name_regex": self.regex.text() or None, "size_min": smin, "size_max": smax,
                      "motw": None if mv == "any" else mv == "yes", "zone_ids": zids or None, "host_domain": doms or None},
            "dest": ntpath.normpath(self.dest.text().strip()) if self.dest.text().strip() else "",
        }
        rd = parse_rule(0, rule)
        if rd.error:
            return self._fail(rd.error)
        svc = self._m.service
        c = dpaths.check_dest(svc.api, rd.base_dest, svc.downloads, self._m.cfg.archive.dir_name)
        if not c.ok and c.code in ("network_dest", "dest_is_downloads", "dest_in_archive"):
            return self._fail(c.message or "この移動先は使えません。")
        if not c.ok:
            ok, _ = W.confirm(self, "移動先を使えません", f"{c.message}\nこのまま保存すると、使えるようになるまでこのルールは働きません。",
                              ok_text="このまま保存")
            if not ok:
                return None
        self.result_rule = rule
        self.accept()
        return None


# ------------------------------------------------------------------ ルールのカード
class RuleCard(QFrame):
    def __init__(self, page: DropSortPage, idx: int, rule: dict[str, Any], total: int) -> None:
        super().__init__()
        self.setObjectName("Inset")
        s = page.ctx.safe
        self.rule_name = str(rule.get("name") or f"#{idx + 1}")
        self.apply = str(rule.get("mode") or "dry-run") == "apply"
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        badge = QLabel(str(idx + 1))
        badge.setFixedSize(26, 26)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet(f"color: {_acc()}; background: {T.alpha(_acc(), 0.14)}; border-radius: 13px; font-weight: 700;")
        name = W.label(str(rule.get("name") or f"#{idx + 1}"), "H3")
        mode = W.Segmented([("dry-run", "試運転"), ("apply", "本番")], str(rule.get("mode") or "dry-run"), _acc())
        mode.changed.connect(s(lambda v, i=idx, seg=mode: page.set_rule_mode(i, v, seg), "dropsort:rule-mode"))
        self.mode_seg = mode
        up = W.icon_button(G.UP, "上へ", s(lambda i=idx: page.move_rule(i, -1), "dropsort:up"))
        down = W.icon_button(G.DOWN, "下へ", s(lambda i=idx: page.move_rule(i, 1), "dropsort:down"))
        up.setEnabled(idx > 0)
        down.setEnabled(idx < total - 1)
        edit = W.icon_button(G.EDIT, "編集", s(lambda i=idx: page.edit_rule(i), "dropsort:edit"))
        dele = W.icon_button(G.DELETE, "削除", s(lambda i=idx: page.delete_rule(i), "dropsort:delete"))
        lay.addLayout(W.hbox(badge, name, 6, mode, None, up, down, edit, dele))

        rd = parse_rule(idx, rule)
        chk = page.m.service.rule_checks.get(idx)
        self.valid = rd.error is None and (chk is None or chk.ok)
        dest = str(rule.get("dest") or "(未設定)")
        dest_lb = W.label(f"{G.FOLDER}  {dest}", "Dim")
        f = T.ui_font(12)
        f.setFamilies([*T.UI_FAMILIES[:2], T.icon_family(), *T.UI_FAMILIES[2:]])
        dest_lb.setFont(f)
        dest_lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        pill: W.StatusPill
        if rd.error:
            pill = W.StatusPill(f"無効: {rd.error}", "error")
        elif chk is not None and not chk.ok:
            pill = W.StatusPill(("見送り中: " if chk.unavailable else "無効: ") + (chk.message or ""),
                                "warn" if chk.unavailable else "error")
        elif chk is not None and chk.volume is not None:
            ns = chk.volume.named_streams
            pill = W.StatusPill(f"{chk.volume.fs_name}" + ("" if ns else " · MOTW 付きは拒否"), "ok" if ns else "warn")
        else:
            pill = W.StatusPill("未検査", "off")
        lay.addLayout(W.hbox(dest_lb, None, pill))
        if rd.is_template:
            ex = W.label(f"例: {tpl.example(rd.dest, when=time.time(), ext=next(iter(sorted(rd.match.ext or [])), '.pdf'))}",
                         "Mute")
            ex.setToolTip("今日来たファイル・入手元 example.com の場合の行き先。足りないフォルダは移動のときに作ります。")
            lay.addWidget(ex)
        chips = QHBoxLayout()
        chips.setSpacing(6)
        for d in (describe(rd) if not rd.error else ["—"]):
            c = QLabel(d)
            c.setStyleSheet(f"color: {T.TEXT_DIM}; background: {T.SURFACE3}; border-radius: 8px; padding: 2px 8px; font-size: 12px;")
            chips.addWidget(c)
        chips.addStretch(1)
        lay.addLayout(chips)
        # 実績(D3): 作業スレッドで集計して後から入れる
        self.stats_label = W.label("実績を集計しています…", "Mute")
        self.promote = W.button("本番へ切り替え", "primary", G.LIGHTNING,
                                on_click=s(lambda i=idx: page.promote_rule(i), "dropsort:promote"))
        self.promote.setToolTip(f"試運転で {dstats.PROMOTE_MIN_DAYS} 日以上・{dstats.PROMOTE_MIN_PLANNED} 件以上の予定があり、"
                                "拒否・取り消しが無いルールに出します。切り替えるかどうかはあなたが決めます。")
        self.promote.setVisible(False)
        lay.addLayout(W.hbox(W.Glyph(G.CLOCK, 12, T.TEXT_MUTE), self.stats_label, None, self.promote, spacing=6))

    def set_stats(self, s: RuleStats | None, now: float) -> None:
        self.stats_label.setText(dstats.summary(s, self.apply))
        ready = not self.apply and self.valid and dstats.promotion_ready(s, now)
        self.promote.setVisible(ready)
        if ready:
            self.stats_label.setText(self.stats_label.text() + " — 本番に切り替えても良さそうです")


# ------------------------------------------------------------------ 画面本体
class DropSortPage(W.ScrollPage):
    def __init__(self, module: DropSortModule) -> None:
        super().__init__()
        self.m = module
        self.ctx = module.ctx
        info = catalog.info("dropsort")
        s = self.ctx.safe
        self._existing: BatchResult | None = None

        # ---- ヒーロー
        hero = W.Hero("DropSort", "ダウンロードが終わったファイルだけを、あなたのルールで決めたフォルダへ。"
                      "開かない・実行しない・MOTW(入手元の印)を失わせない。", info.glyph, _acc())
        self.pill_state = W.StatusPill()
        self.pill_dry = W.StatusPill()
        self.pill_watch = W.StatusPill()
        self.pill_saved = W.StatusPill("保存しました", "ok")
        self.pill_saved.setVisible(False)
        for p in (self.pill_state, self.pill_dry, self.pill_watch, self.pill_saved):
            hero.add_pill(p)
        self.path_label = W.label("", "Mute")
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.btn_pause = W.button("一時停止", "primary", G.PAUSE, on_click=s(self._toggle_pause, "dropsort:pause"))
        hero.add_action(self.btn_pause)
        hero.add_action(W.button("今すぐスキャン", "secondary", G.REFRESH,
                                 on_click=s(lambda: self.m.request_scan(0, manual=True), "dropsort:scan")))
        hero.add_action(W.button("直前を元に戻す", "secondary", G.UNDO, on_click=s(self._undo_last, "dropsort:undo1")))
        self.add(hero)
        pathw = QWidget()
        pathw.setLayout(W.hbox(W.Glyph(G.DOWNLOAD, 14, T.TEXT_MUTE), self.path_label, None, spacing=4, margins=(8, 0, 0, 0)))
        self.add(pathw)

        # ---- 数値タイル
        self.t_moved = W.StatTile("今日の移動数", "0", G.CHECK, T.SUCCESS)
        self.t_pending = W.StatTile("保留中(完了待ち)", "0", G.CLOCK, T.INFO)
        self.t_flagged = W.StatTile("要確認", "0", G.SHIELD, T.WARN)
        self.t_dry = W.StatTile("試運転の予定", "0", G.EYE, _acc())
        tiles = QWidget()
        tiles.setLayout(W.hbox(self.t_moved, self.t_pending, self.t_flagged, self.t_dry, spacing=12))
        self.add(tiles)

        # ---- 切替ナビ + セクション
        self.nav = W.Segmented([("rules", "ルール"), ("existing", "既存ファイル"), ("dryrun", "試運転の結果"),
                                ("history", "履歴"), ("flagged", "要確認"), ("archive", "アーカイブ"),
                                ("advanced", "詳細設定")], "rules", _acc())
        navw = QWidget()
        navw.setLayout(W.hbox(self.nav, None))
        self.add(navw)
        self.stack = QWidget()
        stack_lay = QVBoxLayout(self.stack)
        stack_lay.setContentsMargins(0, 0, 0, 0)
        self.sections: dict[str, QWidget] = {
            "rules": self._build_rules(), "existing": self._build_existing(), "dryrun": self._build_dryrun(),
            "history": self._build_history(), "flagged": self._build_flagged(), "archive": self._build_archive(),
            "advanced": self._build_advanced(),
        }
        for key, w in self.sections.items():
            stack_lay.addWidget(w)
            w.setVisible(key == "rules")
        self._current = "rules"
        self.nav.changed.connect(s(self._switch, "dropsort:nav"))
        self.add(self.stack)
        self.finish()

        # ---- 更新
        self._timer = QTimer(self)
        self._timer.setInterval(2500)
        self._timer.timeout.connect(s(self.refresh_light, "dropsort:page-timer"))
        self._timer.start()
        self._listener = self._on_module_changed
        module.add_listener(self._listener)
        self.destroyed.connect(lambda *_a, m=module, cb=self._listener: m.remove_listener(cb))
        self.refresh_all()
        self._apply_request()

    # ------------------------------------------------------------ 共通
    def _apply_request(self) -> None:
        """クイックアクション・通知から「このセクションを開いて」と頼まれていれば応える。"""
        req = self.m.take_page_request()
        if req is None:
            return
        section, action = req
        if section in self.sections:
            self.nav.set_value(section)
            self._switch(section)
        if action == "existing_dry" and self.ex_run.isEnabled():
            self._existing_dry()

    def _alive(self) -> bool:
        return isValid(self)

    def _parent(self) -> QWidget | None:
        return self.window() if self.isVisible() else None

    def _switch(self, key: str) -> None:
        """セクションを切り替える。隠したセクションは高さに数えないので、ページの長さは表示中のものに合う。"""
        w = self.sections.get(key)
        if w is None or key == self._current:
            return
        self.sections[self._current].setVisible(False)
        self._current = key
        w.setVisible(True)
        eff = QGraphicsOpacityEffect(w)
        w.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b"opacity", w)
        anim.setDuration(200)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(lambda: w.setGraphicsEffect(None) if isValid(w) else None)  # type: ignore[arg-type]
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        self.refresh_section(key)

    def _flash_saved(self) -> None:
        self.pill_saved.setVisible(True)
        QTimer.singleShot(1800, lambda: self.pill_saved.setVisible(False) if isValid(self.pill_saved) else None)

    def _save(self, sec: dict[str, Any], *, restart: bool = False) -> bool:
        try:
            self.m.update_settings(sec, restart=restart)
        except ConfigError as e:
            W.message(self._parent(), "保存できません", str(e), kind="error")
            return False
        except Exception as e:  # noqa: BLE001 - settings.json の構文エラー等
            W.message(self._parent(), "保存できません", f"{type(e).__name__}: {e}", kind="error")
            return False
        if not restart:
            self._flash_saved()
        return True

    def _on_module_changed(self) -> None:
        if not self._alive():
            return
        self._apply_request()
        self.refresh_light()
        key = self.nav.value()
        if key in ("dryrun", "history", "flagged", "rules"):
            self.refresh_section(key)

    def refresh_all(self) -> None:
        self.refresh_light()
        for k in self.sections:
            self.refresh_section(k)

    def refresh_light(self) -> None:
        if not self._alive():
            return
        m = self.m
        svc = m.service
        dl = svc.downloads
        blocked = m.auto_blocked()
        if dl is None:
            self.pill_state.set_state("error", "監視先なし")
        elif m.cfg.paused:
            self.pill_state.set_state("warn", "一時停止")
        elif blocked == "mode":
            self.pill_state.set_state("warn", f"停止中: モード「{m.mode_label(m.mode_paused)}」")
        elif blocked == "snooze":
            self.pill_state.set_state("warn", "停止中: スヌーズ")
        else:
            self.pill_state.set_state("ok", "監視中")
        dry_rules = sum(1 for r in m.cfg.rules if r.mode != "apply")
        if not m.cfg.rules:
            self.pill_dry.set_state("off", "ルール未設定")
        elif dry_rules:
            self.pill_dry.set_state("warn", f"試運転 {dry_rules}/{len(m.cfg.rules)} ルール")
        else:
            self.pill_dry.set_state("ok", "全ルール本番")
        self.pill_watch.set_state("info" if m.cfg.watch_mode == "rdcw" and not m._watch_dead else "off", m.watch_label)
        self.path_label.setText(f"監視先: {dl}" if dl else "監視先: ダウンロードフォルダを解決できません")
        self.btn_pause.setText(f"{G.PLAY}  再開" if m.cfg.paused else f"{G.PAUSE}  一時停止")
        snap = svc.get_snapshot()
        try:
            c = svc.today_counts()
            self.t_moved.set_value(str(c["move"] + c["archive"]))
        except OSError:
            pass
        self.t_pending.set_value(str(snap.get("pending", 0)))
        self.t_flagged.set_value(str(snap.get("flagged", 0)))
        self.t_dry.set_value(str(snap.get("dryrun_present", 0)))

    def refresh_section(self, key: str) -> None:
        if not self._alive():
            return
        fn = {"rules": self._refresh_rules, "dryrun": self._refresh_dryrun, "history": self._refresh_history,
              "flagged": self._refresh_flagged}.get(key)
        if fn is not None:
            fn()

    def _toggle_pause(self) -> None:
        self.m.set_paused(not self.m.cfg.paused)
        self.refresh_light()

    def _undo_last(self) -> None:
        n = self.m.service.undoable_count()
        if n == 0:
            W.message(self._parent(), "元に戻す", "元に戻せる操作はありません。")
            return
        ok, _ = W.confirm(self._parent(), "直前の1件を元に戻す", "最後に移動したファイルを元の場所へ戻します。"
                          "同名のファイルがあれば ' (1)' を付けて戻します(上書きしません)。", ok_text="元に戻す")
        if ok:
            self.m.undo(1)

    # ------------------------------------------------------------ ルール
    def _build_rules(self) -> QWidget:
        card = W.Card("振り分けルール", "上から順に評価し、最初に一致したルールだけを使います。新しいルールは試運転から始まります。",
                      G.FILTER, _acc())
        s = self.ctx.safe
        tmpl = W.button("テンプレートから追加", "primary", G.ADD)
        tmpl.clicked.connect(s(lambda *_: self._template_menu(tmpl), "dropsort:tmpl"))
        card.add_header_widget(tmpl)
        card.add_header_widget(W.button("空のルール", "secondary", G.EDIT, on_click=s(self._add_blank, "dropsort:blank")))
        self.rules_box = QVBoxLayout()
        self.rules_box.setSpacing(10)
        card.add_layout(self.rules_box)
        note = W.label("移動先のフォルダは自動では作りません。無い場合は編集画面の「フォルダを作成」を押したときだけ作ります"
                       "({yyyy} などのテンプレートは、その前のフォルダの下だけ自動で作ります)。"
                       "ネットワーク上のフォルダは移動先にできません。", "Mute", wrap=True)
        card.add(note)
        card.add(self._build_tester())
        return card

    # ------------------------------------------------------------ ルールを試す(D1)
    def _build_tester(self) -> QWidget:
        s = self.ctx.safe
        box = QFrame()
        box.setObjectName("Inset")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        lay.addLayout(W.hbox(W.Glyph(G.SEARCH, 16, _acc()), W.label("ルールを試す", "H3"), None))
        lay.addWidget(W.label("ファイル名などを入れると、どのルールに当たり、どこへ動く予定かを表示します。"
                              "ファイルには触らず、入れた内容は記録しません。", "Mute", wrap=True))
        self.tt_name = QLineEdit()
        self.tt_name.setPlaceholderText("ファイル名 例: invoice_2026.pdf")
        self.tt_size = QLineEdit()
        self.tt_size.setPlaceholderText("サイズ 例: 2MB")
        self.tt_size.setFixedWidth(120)
        self.tt_zone = QComboBox()
        for label, data in (("入手元の印(MOTW)なし", "none"), ("インターネット(ゾーン 3)", "3"),
                            ("イントラネット(ゾーン 1)", "1"), ("信頼済み(ゾーン 2)", "2"), ("ローカル(ゾーン 0)", "0"),
                            ("制限付き(ゾーン 4)", "4"), ("MOTW あり(ゾーン不明)", "unknown")):
            self.tt_zone.addItem(label, data)
        self.tt_zone.setCurrentIndex(1)
        self.tt_domain = QLineEdit()
        self.tt_domain.setPlaceholderText("入手元のドメイン(任意) 例: github.com")
        lay.addLayout(W.hbox(self.tt_name, self.tt_size))
        lay.addLayout(W.hbox(self.tt_zone, self.tt_domain))
        self.tt_result = W.label("", "H3", wrap=True)
        self.tt_dest = W.label("", "Dim", wrap=True)
        self.tt_dest.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.tt_notes = W.label("", "Mute", wrap=True)
        lay.addWidget(self.tt_result)
        lay.addWidget(self.tt_dest)
        lay.addWidget(self.tt_notes)
        self.tt_rules = QVBoxLayout()
        self.tt_rules.setSpacing(2)
        lay.addLayout(self.tt_rules)
        run = s(self._run_tester, "dropsort:tester")
        self.tt_name.textChanged.connect(run)
        self.tt_size.textChanged.connect(run)
        self.tt_domain.textChanged.connect(run)
        self.tt_zone.currentIndexChanged.connect(run)
        self._run_tester()
        return box

    def _run_tester(self, *_: Any) -> None:
        while self.tt_rules.count():
            it = self.tt_rules.takeAt(0)
            w = it.widget() if it is not None else None
            if w is not None:
                w.deleteLater()
        self.tt_dest.setText("")
        self.tt_notes.setText("")
        name = self.tt_name.text().strip()
        if not name:
            self.tt_result.setText("ファイル名を入れてください")
            self.tt_result.setStyleSheet(f"color: {T.TEXT_MUTE};")
            return
        try:
            size = parse_size(self.tt_size.text()) or 0
        except ValueError:
            self.tt_result.setText("サイズは 1024 / 10KB / 5MB のように入れてください")
            self.tt_result.setStyleSheet(f"color: {T.DANGER};")
            return
        m = self.m
        motw = make_motw(str(self.tt_zone.currentData() or "none"), self.tt_domain.text())
        r = simulate(m.cfg, m.service.rule_checks, name, size, motw, time.time())
        if r.blocked is not None:
            self.tt_result.setText("動かしません")
            self.tt_result.setStyleSheet(f"color: {T.WARN};")
        elif r.matched is not None:
            self.tt_result.setText(f"→ ルール「{r.matched.name}」({'本番' if r.matched.apply else '試運転'})")
            self.tt_result.setStyleSheet(f"color: {T.SUCCESS};")
        else:
            self.tt_result.setText("どのルールにも当たりません")
            self.tt_result.setStyleSheet(f"color: {T.TEXT_MUTE};")
        if r.matched is not None and r.dest:
            self.tt_dest.setText(f"{'行き先の予定' if r.blocked is None else '(当たるルールの行き先)'}: "
                                 f"{ntpath.join(r.dest, ntpath.basename(name))}")
        self.tt_notes.setText("\n".join(r.notes))
        colors = {"match": T.SUCCESS, "miss": T.TEXT_MUTE, "disabled": T.DANGER, "shadowed": T.WARN}
        for v in r.verdicts:
            lb = W.label(f"{v.index + 1}. {v.name} — {v.text}", wrap=True)
            lb.setStyleSheet(f"color: {colors.get(v.status, T.TEXT_DIM)}; font-size: 12px;")
            self.tt_rules.addWidget(lb)
        if not r.verdicts:
            self.tt_rules.addWidget(W.label("ルールがまだありません。", "Mute"))

    def _rules_raw(self) -> list[dict[str, Any]]:
        rules = self.m.settings_dict().get("rules") or []
        return [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else []

    def _refresh_rules(self) -> None:
        while self.rules_box.count():
            it = self.rules_box.takeAt(0)
            w = it.widget() if it is not None else None
            if w is not None:
                w.deleteLater()
        rules = self._rules_raw()
        self._rule_cards: list[RuleCard] = []
        if hasattr(self, "tt_rules"):
            self._run_tester()
        if not rules:
            self.rules_box.addWidget(W.EmptyState(G.FILTER, "ルールがまだありません",
                                                  "「テンプレートから追加」で PDF・画像などの雛形を選び、移動先フォルダを決めてください。"))
            return
        for i, r in enumerate(rules):
            card = RuleCard(self, i, r, len(rules))
            self._rule_cards.append(card)
            self.rules_box.addWidget(card)
        self.m.rule_stats(self._apply_stats)

    def _apply_stats(self, stats: dict[str, RuleStats]) -> None:
        if not self._alive():
            return
        now = self.m.service.clock()
        for card in getattr(self, "_rule_cards", []):
            if isValid(card):
                card.set_stats(stats.get(card.rule_name), now)

    def promote_rule(self, i: int) -> None:
        """実績の提案から本番へ切り替える(確認ダイアログを出す。切り替えは利用者の操作だけ)。"""
        cards = getattr(self, "_rule_cards", [])
        if 0 <= i < len(cards) and isValid(cards[i]):
            seg = cards[i].mode_seg
            seg.set_value("apply")
            self.set_rule_mode(i, "apply", seg)

    def _template_menu(self, anchor: QWidget) -> None:
        menu = QMenu(self)
        for label, name, exts in TEMPLATES:
            act = menu.addAction(f"{label}  ({' '.join(exts[:4])}{' …' if len(exts) > 4 else ''})")
            act.triggered.connect(self.ctx.safe(lambda *_a, n=name, e=exts: self._add_rule(new_rule(n, e)), "dropsort:tmpl-pick"))
        menu.exec(anchor.mapToGlobal(QPoint(0, anchor.height() + 4)))

    def _add_blank(self) -> None:
        self._add_rule(new_rule("新しいルール"))

    def _unique(self, base: str, names: set[str]) -> str:
        if base not in names:
            return base
        n = 2
        while f"{base} {n}" in names:
            n += 1
        return f"{base} {n}"

    def _add_rule(self, rule: dict[str, Any]) -> None:
        sec = self.m.settings_dict()
        rules = sec.setdefault("rules", [])
        names = {str(r.get("name")) for r in rules if isinstance(r, dict)}
        rule["name"] = self._unique(str(rule["name"]), names)
        dlg = RuleEditDialog(self, rule, names, "ルールを追加")
        if dlg.exec() and dlg.result_rule is not None:
            rules.append(dlg.result_rule)
            if self._save(sec):
                self._refresh_rules()

    def edit_rule(self, i: int) -> None:
        sec = self.m.settings_dict()
        rules = sec.get("rules") or []
        if not (0 <= i < len(rules)):
            return
        names = {str(r.get("name")) for j, r in enumerate(rules) if j != i and isinstance(r, dict)}
        dlg = RuleEditDialog(self, rules[i], names, "ルールを編集")
        if dlg.exec() and dlg.result_rule is not None:
            rules[i] = dlg.result_rule
            if self._save(sec):
                self._refresh_rules()

    def move_rule(self, i: int, d: int) -> None:
        sec = self.m.settings_dict()
        rules = sec.get("rules") or []
        j = i + d
        if 0 <= i < len(rules) and 0 <= j < len(rules):
            rules[i], rules[j] = rules[j], rules[i]
            if self._save(sec):
                self._refresh_rules()

    def delete_rule(self, i: int) -> None:
        sec = self.m.settings_dict()
        rules = sec.get("rules") or []
        if not (0 <= i < len(rules)):
            return
        ok, _ = W.confirm(self._parent(), "ルールを削除", f"ルール「{rules[i].get('name')}」を削除します。"
                          "移動済みのファイルと操作履歴はそのまま残ります。", ok_text="削除", danger=True)
        if ok:
            del rules[i]
            if self._save(sec):
                self._refresh_rules()

    def pending_for_rule(self, name: str) -> int:
        svc = self.m.service
        if not svc.downloads:
            return 0
        ds = svc.store.load().dir(svc.downloads)
        if ds is None:
            return 0
        return sum(1 for h in ds.handled.values() if h.get("op") == "would_move" and h.get("rule") == name)

    def set_rule_mode(self, i: int, mode: str, seg: W.Segmented) -> None:
        sec = self.m.settings_dict()
        rules = sec.get("rules") or []
        if not (0 <= i < len(rules)):
            return
        prev = str(rules[i].get("mode") or "dry-run")
        if mode == "apply":
            n = self.pending_for_rule(str(rules[i].get("name")))
            extra = f"\n試運転で「移動予定」になっている {n} 件は、次のスキャンで実際に移動されます。" if n else ""
            ok, _ = W.confirm(self._parent(), "本番に切り替える",
                              f"ルール「{rules[i].get('name')}」に一致したファイルを、実際に移動するようにします。{extra}"
                              "\n誤って動いたものはトレイや履歴の「元に戻す」で戻せます。", ok_text="本番にする")
            if not ok:
                seg.set_value(prev)
                return
        rules[i]["mode"] = mode
        if not self._save(sec):
            seg.set_value(prev)
        else:
            self.refresh_light()

    # ------------------------------------------------------------ 既存ファイル(sort-existing)
    def _build_existing(self) -> QWidget:
        card = W.Card("既存ファイルの整理", "DropSort を使い始める前からあったファイル(基準線)は自動では動かしません。"
                      "ここで試運転して結果を確かめてから、まとめて移動できます。", G.ARCHIVE, _acc())
        s = self.ctx.safe
        self.ex_run = W.button("試運転で調べる", "primary", G.SEARCH, on_click=s(self._existing_dry, "dropsort:ex-dry"))
        self.ex_apply = W.button("移動する", "danger", G.CHECK, on_click=s(self._existing_apply, "dropsort:ex-apply"))
        self.ex_apply.setEnabled(False)
        self.ex_hide = QCheckBox("対象外のファイルを隠す")
        self.ex_hide.setChecked(True)
        self.ex_hide.toggled.connect(s(lambda *_: self._show_existing(), "dropsort:ex-hide"))
        self.ex_state = W.label("まだ調べていません。", "Dim")
        card.add_layout(W.hbox(self.ex_run, self.ex_apply, 12, self.ex_state, None, self.ex_hide))
        self.ex_table = make_table(BATCH_HEADERS, stretch=3, min_height=340)
        card.add(self.ex_table)
        card.add(W.label("本番の実行では、試運転中のルールも含めて一致したルールを使います(試運転の結果で確かめたとおりに動きます)。"
                         "アーカイブが有効なら、どのルールにも当たらず長く触られていないファイルはアーカイブへ移します。", "Mute", wrap=True))
        return card

    def _existing_dry(self) -> None:
        self.ex_run.setEnabled(False)
        self.ex_apply.setEnabled(False)
        self.ex_state.setText("調べています…")
        self.m.sort_existing(False, self._existing_done_dry)

    def _existing_done_dry(self, r: Any) -> None:
        if not self._alive():
            return
        self.ex_run.setEnabled(True)
        if isinstance(r, Exception):
            self.ex_state.setText(f"調べられませんでした: {r}")
            return
        assert isinstance(r, BatchResult)
        if r.error:
            self.ex_state.setText("ダウンロードフォルダを解決できません。")
            return
        self._existing = r
        n = r.count("would_move", "would_archive")
        self.ex_state.setText(f"移動予定 {r.count('would_move')} · アーカイブ予定 {r.count('would_archive')} · "
                              f"拒否予定 {r.count('would_refuse')} · 要確認 {r.count('flagged')} · 対象外 {r.count('no_match')}")
        self.ex_apply.setText(f"{G.CHECK}  {n} 件を移動する")
        self.ex_apply.setEnabled(n > 0)
        self._show_existing()

    def _show_existing(self) -> None:
        r = self._existing
        if r is None:
            return
        items = [i for i in r.items if not (self.ex_hide.isChecked() and i.get("op") == "no_match")]
        rows, colors, tips = record_rows(items, with_time=False)
        fill_table(self.ex_table, rows, colors, tips)

    def _existing_apply(self) -> None:
        r = self._existing
        if r is None:
            return
        n = r.count("would_move", "would_archive")
        ok, _ = W.confirm(self._parent(), "既存ファイルを移動", f"試運転の結果どおり {n} 件を移動します。"
                          "移動は操作履歴に残り、「元に戻す」で戻せます。", ok_text=f"{n} 件を移動", danger=True)
        if not ok:
            return
        self.ex_run.setEnabled(False)
        self.ex_apply.setEnabled(False)
        self.ex_state.setText("移動しています…")
        self.m.sort_existing(True, self._existing_done_apply)

    def _existing_done_apply(self, r: Any) -> None:
        if not self._alive():
            return
        self.ex_run.setEnabled(True)
        if isinstance(r, Exception):
            self.ex_state.setText(f"実行できませんでした: {r}")
            W.message(self._parent(), "実行できません", str(r), kind="error")
            return
        assert isinstance(r, BatchResult)
        self._existing = r
        self._show_existing()
        moved = r.count("move", "archive")
        bad = r.count("refused", "failed")
        self.ex_state.setText(f"移動 {moved} · 拒否/失敗 {bad} · 要確認 {r.count('flagged')}")
        self.ex_apply.setText(f"{G.CHECK}  移動する")
        W.message(self._parent(), "既存ファイルの整理", f"{moved} 件を移動しました。" + (f"\n{bad} 件は移動しませんでした(理由は一覧)。" if bad else ""),
                  kind="warn" if bad else "ok")

    # ------------------------------------------------------------ 試運転の結果
    def _build_dryrun(self) -> QWidget:
        card = W.Card("試運転の結果", "試運転のルールとアーカイブが「動かすなら こうする」と判定した記録(dryrun.jsonl)です。", G.EYE, _acc())
        s = self.ctx.safe
        self.dry_filter = W.Segmented([("all", "すべて"), ("would_move", "移動予定"), ("would_refuse", "拒否予定"),
                                       ("would_archive", "アーカイブ予定")], "all", _acc())
        self.dry_filter.changed.connect(s(lambda *_: self._refresh_dryrun(), "dropsort:dry-filter"))
        card.add_layout(W.hbox(self.dry_filter, None, W.button("更新", "ghost", G.REFRESH, on_click=s(self._refresh_dryrun, "dropsort:dry-refresh"))))
        self.dry_table = make_table(REC_HEADERS, stretch=4, min_height=360)
        card.add(self.dry_table)
        return card

    def _refresh_dryrun(self) -> None:
        items = list(reversed(self.m.service.dryrun.tail(400)))
        f = self.dry_filter.value()
        if f != "all":
            items = [i for i in items if i.get("op") == f]
        rows, colors, tips = record_rows(items)
        fill_table(self.dry_table, rows, colors, tips)

    # ------------------------------------------------------------ 履歴
    def _build_history(self) -> QWidget:
        card = W.Card("操作履歴", "移動・アーカイブ・拒否・要確認・元に戻す の記録(oplog.jsonl)。", G.LOG, _acc())
        s = self.ctx.safe
        self.undo_count = QSpinBox()
        self.undo_count.setRange(1, 100)
        self.undo_count.setValue(self.m.cfg.undo_default_count)
        self.undo_count.setFixedWidth(70)
        self.undo_info = W.label("", "Dim")
        card.add_layout(W.hbox(W.label("新しい順に", "Dim"), self.undo_count, W.label("件を", "Dim"),
                               W.button("元に戻す", "primary", G.UNDO, on_click=s(self._undo_n, "dropsort:undo-n")),
                               12, self.undo_info, None,
                               W.button("更新", "ghost", G.REFRESH, on_click=s(self._refresh_history, "dropsort:hist-refresh"))))
        self.hist_table = make_table(REC_HEADERS, stretch=4, min_height=360)
        card.add(self.hist_table)
        card.add(W.label("移動後に中身や更新時刻が変わったファイルは戻しません(「変更あり」として記録)。"
                         "元の場所に同名があれば ' (1)' を付けて戻します。", "Mute", wrap=True))
        return card

    def _refresh_history(self) -> None:
        svc = self.m.service
        ops = list(reversed(svc.oplog.tail(400)))
        undone = {str(r.get("undo_of")) for r in ops if r.get("op") == "undo"}
        rows, colors, tips = record_rows(ops)
        for row, r in zip(rows, ops, strict=True):
            if str(r.get("id")) in undone:
                row[-1] = (row[-1] + " " if row[-1] else "") + "(元に戻し済み)"
        fill_table(self.hist_table, rows, colors, tips)
        try:
            self.undo_info.setText(f"元に戻せる操作: {svc.undoable_count()} 件")
        except OSError:
            pass

    def _undo_n(self) -> None:
        n = int(self.undo_count.value())
        if self.m.service.undoable_count() == 0:
            W.message(self._parent(), "元に戻す", "元に戻せる操作はありません。")
            return
        ok, _ = W.confirm(self._parent(), "元に戻す", f"新しい順に最大 {n} 件の移動・アーカイブを元の場所へ戻します。", ok_text="元に戻す")
        if ok:
            self.m.undo(n, lambda _r: self._refresh_history() if self._alive() else None)

    # ------------------------------------------------------------ 要確認
    def _build_flagged(self) -> QWidget:
        card = W.Card("要確認のファイル", "名前の偽装が疑われる・他のプログラムが掴み続けている等の理由で、どのルールにも当てずに"
                      "ダウンロードフォルダへ残しているファイルです。DropSort は開きも削除もしません。", G.SHIELD, T.WARN)
        s = self.ctx.safe
        self.flag_table = make_table(["ファイル名", "理由", "サイズ", "検出"], stretch=0, min_height=260)
        self.flag_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        card.add_layout(W.hbox(W.label("確認したものは「確認済みにする」で一覧から外せます(ファイルはそのまま・以後も自動では動かしません)。", "Mute", wrap=True),
                               None, W.button("確認済みにする", "secondary", G.CHECK, on_click=s(self._ack, "dropsort:ack"))))
        card.add(self.flag_table)
        return card

    def _refresh_flagged(self) -> None:
        self._flag_items = self.m.service.flagged_list()
        rows = []
        for h in self._flag_items:
            ts = h.get("ts")
            rows.append([str(h.get("name")), reason_text(h.get("reason")), fmt_size(h.get("size")),
                         datetime.fromtimestamp(ts).strftime("%m/%d %H:%M") if isinstance(ts, int | float) else ""])
        fill_table(self.flag_table, rows, [T.WARN] * len(rows), [str(h.get("path")) for h in self._flag_items])

    def _ack(self) -> None:
        sel = sorted({i.row() for i in self.flag_table.selectedIndexes()})
        names = [str(self._flag_items[r].get("name")) for r in sel if r < len(self._flag_items)]
        if not names:
            W.message(self._parent(), "確認済みにする", "一覧から項目を選んでください。")
            return
        ok, _ = W.confirm(self._parent(), "確認済みにする", f"{len(names)} 件を一覧から外します。ファイルは動かしません。", ok_text="確認済みにする")
        if ok:
            for n in names:
                self.m.acknowledge(n, lambda _r: self._refresh_flagged() if self._alive() else None)

    # ------------------------------------------------------------ アーカイブ
    def _build_archive(self) -> QWidget:
        a = self.m.settings_dict().get("archive") or {}
        s = self.ctx.safe
        card = W.Card("アーカイブ", "しばらく触られていないファイルを <ダウンロード>\\_archive\\YYYY-MM\\ へ退避します(削除はしません)。"
                      "月は「最後に触られた月」です。", G.ARCHIVE, T.INFO)
        self.ar_enabled = W.ToggleSwitch(bool(a.get("enabled")), _acc())
        self.ar_enabled.toggled.connect(s(lambda v: self._set_archive("enabled", bool(v)), "dropsort:ar-en"))
        card.add(W.SettingRow("自動アーカイブ", "定期スキャンのたびに評価します。", self.ar_enabled, G.POWER))
        self.ar_mode = W.Segmented([("dry-run", "試運転"), ("apply", "本番")], str(a.get("mode") or "dry-run"), _acc())
        self.ar_mode.changed.connect(s(self._set_archive_mode, "dropsort:ar-mode"))
        card.add(W.SettingRow("動作", "試運転では予定を記録するだけで、ファイルは動かしません。", self.ar_mode, G.EYE))
        self.ar_days = QSpinBox()
        self.ar_days.setRange(1, 36500)
        self.ar_days.setSuffix(" 日")
        self.ar_days.setValue(int(a.get("idle_days") or 30))
        self.ar_days.editingFinished.connect(s(lambda: self._set_archive("idle_days", int(self.ar_days.value())), "dropsort:ar-days"))
        card.add(W.SettingRow("触られていない日数", "更新・作成・DropSort が最初に見た時刻のうち最も新しいものから数えます。", self.ar_days, G.CLOCK))
        self.ar_dir = QLineEdit(str(a.get("dir_name") or "_archive"))
        self.ar_dir.setFixedWidth(180)
        self.ar_dir.editingFinished.connect(s(lambda: self._set_archive("dir_name", self.ar_dir.text().strip()), "dropsort:ar-dir"))
        card.add(W.SettingRow("フォルダ名", "ダウンロードフォルダの中に作ります。", self.ar_dir, G.FOLDER))
        self.ar_atime = W.ToggleSwitch(bool(a.get("use_atime")), _acc())
        self.ar_atime.toggled.connect(s(lambda v: self._set_archive("use_atime", bool(v)), "dropsort:ar-atime"))
        card.add(W.SettingRow("最終アクセス日時も使う", "NTFS で最終アクセス日時の更新が有効な PC だけオンにしてください。", self.ar_atime, G.INFO))
        card.add(W.divider())
        card.add_layout(W.hbox(W.button("今すぐ評価(試運転)", "secondary", G.SEARCH, on_click=s(lambda: self._archive_now(False), "dropsort:ar-dry")),
                               W.button("今すぐアーカイブ", "danger", G.ARCHIVE, on_click=s(lambda: self._archive_now(True), "dropsort:ar-apply")),
                               None))
        card.add(W.label("使い始める前からあったファイル(基準線)は、ここでは動かしません。「既存ファイル」の整理で扱います。", "Mute", wrap=True))
        return card

    def _set_archive(self, key: str, value: Any) -> None:
        sec = self.m.settings_dict()
        arch = dict(sec.get("archive") or {})
        if arch.get(key) == value:
            return
        arch[key] = value
        sec["archive"] = arch
        self._save(sec)

    def _set_archive_mode(self, mode: str) -> None:
        if mode == "apply":
            ok, _ = W.confirm(self._parent(), "アーカイブを本番にする", "条件に当たるファイルを実際に _archive へ移すようにします。", ok_text="本番にする")
            if not ok:
                self.ar_mode.set_value("dry-run")
                return
        self._set_archive("mode", mode)

    def _archive_now(self, apply: bool) -> None:
        if apply:
            ok, _ = W.confirm(self._parent(), "今すぐアーカイブ", "条件に当たるファイルを今すぐ _archive へ移します。", ok_text="アーカイブ", danger=True)
            if not ok:
                return

        def done(r: Any) -> None:
            if not self._alive():
                return
            if isinstance(r, Exception):
                W.message(self._parent(), "実行できません", str(r), kind="error")
                return
            assert isinstance(r, BatchResult)
            RecordsDialog(self._parent(), "アーカイブの評価" + ("(本番)" if apply else "(試運転)"), r.items, with_time=False,
                          note=f"{self.m.cfg.archive.idle_days} 日以上触られていないファイル").exec()

        self.m.archive_now(apply, done)

    # ------------------------------------------------------------ 詳細設定
    def _build_advanced(self) -> QWidget:
        sec = self.m.settings_dict()
        s = self.ctx.safe
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)

        c1 = W.Card("ダウンロード完了の判定", "一時ファイル名でなく、サイズと更新時刻が一定時間変わらず、他のプログラムが掴んでいないものだけを完了とみなします。",
                    G.CLOCK, _acc())
        self.adv_stable = self._spin(int(sec.get("stable_seconds") or 5), 0, 3600, " 秒", "stable_seconds")
        c1.add(W.SettingRow("安定を待つ時間", "0 バイトのファイルはこの2倍待ちます。", self.adv_stable, G.CLOCK))
        self.adv_locked = self._spin(int(sec.get("locked_retry_max") or 10), 1, 10000, " 回", "locked_retry_max")
        c1.add(W.SettingRow("使用中の再試行", "この回数続けて開けなければ「要確認」にします。", self.adv_locked, G.LOCK))
        c1.add(W.label("一時ファイルの拡張子(ダウンロード中の名前)", "Dim"))
        self.adv_temp = W.StringListEditor(sec.get("temp_extensions") or [], ".crdownload など", norm_ext, 110)
        self.adv_temp.changed.connect(s(lambda v: self._set_key("temp_extensions", list(v)), "dropsort:temp"))
        c1.add(self.adv_temp)
        lay.addWidget(c1)

        c2 = W.Card("危険な名前の検出", "最後の拡張子がこの一覧にあり、その前にも拡張子らしい部分がある名前(例: report.pdf.exe)を「要確認」にします。",
                    G.SHIELD, T.WARN)
        self.adv_exec = W.StringListEditor(sec.get("exec_extensions") or [], ".exe など", norm_ext, 130)
        self.adv_exec.changed.connect(s(lambda v: self._set_key("exec_extensions", list(v)), "dropsort:exec"))
        c2.add(self.adv_exec)
        lay.addWidget(c2)

        c3 = W.Card("監視", "変更の通知は「スキャンのきっかけ」にだけ使い、判断は毎回フォルダの実際の状態から行います。", G.EYE, _acc())
        self.adv_watch = W.Segmented([("rdcw", "リアルタイム + 定期"), ("poll", "定期スキャンのみ")], str(sec.get("watch_mode") or "rdcw"), _acc())
        self.adv_watch.changed.connect(s(lambda v: self._set_key("watch_mode", v, restart=True), "dropsort:watch"))
        c3.add(W.SettingRow("監視方式", "切り替えるとモジュールを再起動します。", self.adv_watch, G.MONITOR))
        self.adv_scan = self._spin(int(sec.get("full_scan_interval_s") or 30), 2, 86400, " 秒", "full_scan_interval_s")
        c3.add(W.SettingRow("定期スキャンの間隔", None, self.adv_scan, G.REFRESH))
        self.adv_override = QLineEdit(str(sec.get("downloads_dir_override") or ""))
        self.adv_override.setPlaceholderText("空欄なら Windows のダウンロードフォルダ")
        self.adv_override.setReadOnly(True)
        c3.add(W.SettingRow("監視するフォルダ", "通常は空欄のまま(Windows の設定から場所を解決します)。", None, G.FOLDER))
        c3.add_layout(W.hbox(self.adv_override,
                             W.button("参照…", "secondary", G.FOLDER, on_click=s(self._pick_override, "dropsort:ov")),
                             W.button("既定に戻す", "ghost", G.CLEAR, on_click=s(lambda: self._set_override(None), "dropsort:ov-clear"))))
        lay.addWidget(c3)

        c5 = W.Card("モード連携とスヌーズ", "ModeShift のモードがここで選んだどれかの間と、DeskKit のスヌーズ中は、自動の整理を止めます"
                    "(「今すぐスキャン」など手で押した操作は動きます)。止まっている間に来たファイルは、再開したときにフォルダ全体を"
                    "調べ直して扱います。止めた・再開したことは通知せず、この画面とトレイの状態に出します。", G.MODE, _acc())
        self.pm_box = QVBoxLayout()
        self.pm_box.setSpacing(4)
        c5.add_layout(self.pm_box)
        self._build_pause_modes(list(sec.get("pause_in_modes") or []))
        lay.addWidget(c5)

        c4 = W.Card("その他", None, G.SETTINGS, _acc())
        self.adv_suffix = self._spin(int(sec.get("max_suffix") or 99), 1, 9999, "", "max_suffix")
        c4.add(W.SettingRow("連番の上限", "移動先に同名があるとき ' (1)' ' (2)' … をこの番号まで試します。", self.adv_suffix, G.LIST))
        self.adv_undo = self._spin(int(sec.get("undo_default_count") or 1), 1, 1000, " 件", "undo_default_count")
        c4.add(W.SettingRow("undo の既定件数", "CLI の undo で --count を省いたときの件数。", self.adv_undo, G.UNDO))
        hk = (sec.get("hotkeys") or {}).get("undo_last") or ""
        self.adv_hotkey = W.HotkeyEdit(str(hk))
        self.adv_hotkey.changed.connect(s(self._set_hotkey, "dropsort:hotkey"))
        state = "" if not hk else ("(登録済み)" if self.m.hotkey_ok else "(登録できませんでした: 他と競合している可能性)")
        c4.add(W.SettingRow("「直前を元に戻す」のホットキー", "既定は未割り当て。変更するとモジュールを再起動します。" + state, self.adv_hotkey, G.KEYBOARD))
        lay.addWidget(c4)
        return box

    def _build_pause_modes(self, selected: list[str]) -> None:
        """ModeShift のモード(ctx.list_modes)をチェックで選ぶ。今の設定に無いモード名も消さずに残す。"""
        modes = self.m.list_modes()
        known = {n for n, _ in modes}
        self._pm_boxes: list[tuple[str, QCheckBox]] = []
        for n, lb in modes:
            cb = QCheckBox(lb if lb == n or not lb else f"{lb}({n})")
            cb.setChecked(n in selected)
            self._pm_boxes.append((n, cb))
        for n in selected:
            if n not in known:
                cb = QCheckBox(f"{n}(今の ModeShift の設定にありません)")
                cb.setChecked(True)
                self._pm_boxes.append((n, cb))
        for _n, cb in self._pm_boxes:
            cb.toggled.connect(self.ctx.safe(lambda *_a: self._save_pause_modes(), "dropsort:pause-modes"))
            self.pm_box.addWidget(cb)
        if not self._pm_boxes:
            self.pm_box.addWidget(W.label("ModeShift にモードがありません。ModeShift の画面でモードを作ると、ここで選べます。",
                                          "Mute", wrap=True))

    def _save_pause_modes(self) -> None:
        self._set_key("pause_in_modes", [n for n, cb in self._pm_boxes if cb.isChecked()])
        self.refresh_light()

    def _spin(self, value: int, lo: int, hi: int, suffix: str, key: str) -> QSpinBox:
        sp = QSpinBox()
        sp.setRange(lo, hi)
        sp.setValue(value)
        if suffix:
            sp.setSuffix(suffix)
        sp.setFixedWidth(110)
        sp.editingFinished.connect(self.ctx.safe(lambda: self._set_key(key, int(sp.value())), f"dropsort:{key}"))
        return sp

    def _set_key(self, key: str, value: Any, *, restart: bool = False) -> None:
        sec = self.m.settings_dict()
        if sec.get(key) == value:
            return
        sec[key] = value
        self._save(sec, restart=restart)

    def _pick_override(self) -> None:
        start = self.m.service.downloads or ""
        d = QFileDialog.getExistingDirectory(self._parent(), "監視するフォルダを選ぶ", start, QFileDialog.Option.ShowDirsOnly)
        if d:
            self._set_override(ntpath.normpath(d))

    def _set_override(self, path: str | None) -> None:
        if path and dpaths.is_unc(path):
            W.message(self._parent(), "使えません", "ネットワーク上のフォルダは監視できません。", kind="error")
            return
        if path:
            ok, _ = W.confirm(self._parent(), "監視するフォルダを変える",
                              f"{path}\nを監視します。今そこにあるファイルは基準線として記録し、自動では動かしません。", ok_text="変更")
            if not ok:
                return
        self._set_key("downloads_dir_override", path, restart=True)

    def _set_hotkey(self, text: str) -> None:
        sec = self.m.settings_dict()
        hk = dict(sec.get("hotkeys") or {})
        hk["undo_last"] = text
        sec["hotkeys"] = hk
        self._save(sec, restart=True)
