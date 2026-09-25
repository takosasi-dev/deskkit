# モード編集画面: モードの作成・名前変更・複製・削除(確認あり)・並べ替えと、モードごとのアクション列の追加
# (8種類のメニュー)・並べ替え・種別ごとの入力フォームでの編集。検証エラーはモードごとにその場で表示する(FR-1)。
# 保存は ctx.write_settings 経由。定義が変わればハッシュが変わるので、そのモードは自動で「未確認」に戻る。
from __future__ import annotations

import copy
import logging
import ntpath
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from deskkit.modules.modeshift.config import (
    NAME_RE,
    definition_hash,
    normalize_action,
    safe_url,
    url_ok,
    validate_section,
)
from deskkit.modules.modeshift.model import ACTION_TYPES, TYPE_LABELS, pct
from deskkit.modules.modeshift.system import PowerScheme
from deskkit.modules.modeshift.visuals import TYPE_GLYPHS, Chip, accent_key, guard, mode_accent, palette
from deskkit.ui import theme as T
from deskkit.ui import widgets as W
from deskkit.ui.theme import G

if TYPE_CHECKING:
    from deskkit.modules.modeshift.module import ModeShiftModule

log = logging.getLogger("deskkit.modeshift")

TYPE_HINTS: dict[str, str] = {
    "launch_app": "exe を引数つきで起動(動作中なら起動しない設定も可)",
    "close_app": "ウィンドウに閉じる要求を送り、終わるまで待つ",
    "power_plan": "Windows の電源プランを切り替える",
    "master_volume": "既定の再生デバイスの音量・ミュート",
    "app_volume": "アプリ別の音量(音量ミキサーの値)",
    "open_path": "フォルダやファイルを既定のアプリで開く",
    "open_url": "http / https の URL を既定のブラウザで開く",
    "layout_apply": "LayoutKeep に配置の適用を頼む(送るだけ)",
}


def default_action(t: str) -> dict[str, Any]:
    table: dict[str, dict[str, Any]] = {
        "launch_app": {"type": t, "path": "", "args": [], "cwd": None, "skip_if_running": True},
        "close_app": {"type": t, "exe": "", "timeout_s": 10, "force_on_timeout": False},
        "power_plan": {"type": t, "guid": ""},
        "master_volume": {"type": t, "level": 0.5, "mute": None},
        "app_volume": {"type": t, "exe": "", "level": 0.5},
        "open_path": {"type": t, "path": ""},
        "open_url": {"type": t, "url": "https://"},
        "layout_apply": {"type": t, "layout": None, "wait_s": 0},
    }
    return table[t]


def action_summary(a: dict[str, Any], schemes: list[PowerScheme] | None = None) -> str:
    t = a.get("type")
    if t == "launch_app":
        args = a.get("args") or []
        return (ntpath.basename(str(a.get("path") or "")) or "(exe 未指定)") + (f"  {' '.join(args)}" if args else "") + (
            "" if a.get("skip_if_running", True) else "  ・動作中でも起動")
    if t == "close_app":
        return f"{a.get('exe') or '(exe 未指定)'}  ・{a.get('timeout_s', 10):g} 秒待つ" + ("  ・確認して強制終了" if a.get("force_on_timeout") else "")
    if t == "power_plan":
        g = str(a.get("guid") or "")
        for s in schemes or []:
            if s.guid == g.lower():
                return f"{s.name}  ({g})"
        return g or "(未選択)"
    if t == "master_volume":
        lv = a.get("level")
        mu = a.get("mute")
        return ("音量はそのまま" if lv is None else pct(float(lv))) + ("" if mu is None else ("  ・ミュート" if mu else "  ・ミュート解除"))
    if t == "app_volume":
        return f"{a.get('exe') or '(exe 未指定)'}  ・{pct(float(a.get('level') or 0))}"
    if t == "open_path":
        return str(a.get("path") or "(未指定)")
    if t == "open_url":
        return safe_url(str(a.get("url") or ""))
    if t == "layout_apply":
        return (a.get("layout") or "(既定のレイアウト)") + (f"  ・{float(a.get('wait_s') or 0):g} 秒待ち" if a.get("wait_s") else "")
    return f"未知の種別 {t}"


# ------------------------------------------------------------------ プロセス選択
class ProcessPicker(W.StyledDialog):
    """動作中のプロセス(exe 名だけ)から選ぶ。音を出しているアプリを先頭に出せる。"""

    def __init__(self, parent: QWidget | None, module: ModeShiftModule, *, exclude_games: bool, audio_first: bool) -> None:
        super().__init__(parent, "動作中のアプリから選ぶ", G.APP, T.ACCENT, width=420)
        svc = module.service
        games = module.ctx.game_processes()
        names: set[str] = set()
        audio_names: set[str] = set()
        if svc is not None:
            try:
                procs = svc.backends.processes.list_processes()
                names = {p.exe for p in procs if p.exe.endswith(".exe")}
                if audio_first:
                    by_pid = {p.pid: p.exe for p in procs}
                    audio_names = {by_pid[s.pid] for s in svc.backends.audio.list_sessions() if s.pid in by_pid}
            except Exception:  # noqa: BLE001
                log.exception("プロセス一覧を取れません")
        if exclude_games:
            names -= set(games)
            audio_names -= set(games)
        self.body.addWidget(W.label("ゲーム(game_processes)は閉じる対象に選べません。" if exclude_games
                                    else "音を出しているアプリを先頭に出しています。", "Mute", wrap=True))
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("絞り込み…")
        self.body.addWidget(self.filter)
        self.list = QListWidget()
        self.list.setMinimumHeight(300)
        for n in sorted(audio_names):
            it = QListWidgetItem(f"♪  {n}")
            it.setData(Qt.ItemDataRole.UserRole, n)
            it.setToolTip("音を出しているアプリ")
            self.list.addItem(it)
        for n in sorted(names - audio_names):
            it = QListWidgetItem(n)
            it.setData(Qt.ItemDataRole.UserRole, n)
            self.list.addItem(it)
        self.body.addWidget(self.list)
        self.filter.textChanged.connect(guard(self._apply_filter))
        self.list.itemDoubleClicked.connect(guard(lambda _i: self.accept()))
        self.buttons.addWidget(W.button("キャンセル", "ghost", on_click=self.reject))
        self.buttons.addWidget(W.button("選ぶ", "primary", on_click=self.accept))
        self.filter.setFocus()

    def _apply_filter(self, text: str) -> None:
        q = text.strip().lower()
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setHidden(bool(q) and q not in str(it.data(Qt.ItemDataRole.UserRole)))

    def value(self) -> str | None:
        it = self.list.currentItem()
        return str(it.data(Qt.ItemDataRole.UserRole)) if it is not None else None


# ------------------------------------------------------------------ アクション入力フォーム
class VolumeSlider(QWidget):
    """0〜100% のスライダー(設定値はスカラー 0.0〜1.0)。"""

    changed = Signal()

    def __init__(self, level: float) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(W.Glyph(G.VOLUME, 14, T.TEXT_DIM))
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(round(level * 100))
        lay.addWidget(self.slider, 1)
        self.val = QLabel(f"{self.slider.value()}%")
        self.val.setMinimumWidth(44)
        self.val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.val.setStyleSheet("font-weight: 700;")
        lay.addWidget(self.val)
        self.slider.valueChanged.connect(guard(self._on))

    def _on(self, v: int) -> None:
        self.val.setText(f"{v}%")
        self.changed.emit()

    def level(self) -> float:
        return round(self.slider.value() / 100.0, 2)


class ActionDialog(W.StyledDialog):
    def __init__(self, parent: QWidget | None, module: ModeShiftModule, action: dict[str, Any]) -> None:
        t = str(action.get("type"))
        super().__init__(parent, TYPE_LABELS.get(t, t), TYPE_GLYPHS.get(t, G.MODE), mode_accent(None, 0), width=520)
        self.module = module
        self.t = t
        self.a = copy.deepcopy(action)
        self._getters: list[Callable[[dict[str, Any]], None]] = []
        self.body.addWidget(W.label(TYPE_HINTS.get(t, ""), "Mute", wrap=True))
        getattr(self, f"_form_{t}")()
        self.err = W.label("", wrap=True)
        self.err.setStyleSheet(f"color: {T.DANGER};")
        self.body.addWidget(self.err)
        self.buttons.addWidget(W.button("キャンセル", "ghost", on_click=self.reject))
        self.ok = W.button("OK", "primary", on_click=guard(self._accept))
        self.buttons.addWidget(self.ok)
        QTimer.singleShot(0, guard(self._validate))

    # 共通の部品
    def _row(self, title: str, w: QWidget | QHBoxLayout, hint: str | None = None) -> None:
        box = QVBoxLayout()
        box.setSpacing(4)
        box.addWidget(W.label(title, "H3"))
        if isinstance(w, QWidget):
            box.addWidget(w)
        else:
            box.addLayout(w)
        if hint:
            box.addWidget(W.label(hint, "Mute", wrap=True))
        self.body.addLayout(box)

    def _line(self, key: str, placeholder: str = "") -> QLineEdit:
        e = QLineEdit(str(self.a.get(key) or ""))
        e.setPlaceholderText(placeholder)
        e.textChanged.connect(guard(lambda _t: self._validate()))
        return e

    def _exe_field(self, *, exclude_games: bool, audio_first: bool) -> QLineEdit:
        e = self._line("exe", "例: discord.exe")
        pick = W.button("動作中から選ぶ…", "secondary", G.APP,
                        on_click=guard(lambda: self._pick_proc(e, exclude_games, audio_first)))
        self._row("exe 名", self._wrap(W.hbox(e, pick)),
                  "ファイル名だけ(大文字小文字は区別しない)。同じ exe のプロセスが複数あれば全部が対象です。")
        self._getters.append(lambda d: d.__setitem__("exe", e.text().strip().lower()))
        return e

    @staticmethod
    def _wrap(lay: QHBoxLayout) -> QWidget:
        w = QWidget()
        w.setLayout(lay)
        return w

    def _pick_proc(self, target: QLineEdit, exclude_games: bool, audio_first: bool) -> None:
        dlg = ProcessPicker(self, self.module, exclude_games=exclude_games, audio_first=audio_first)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.value():
            target.setText(dlg.value() or "")

    def _browse_file(self, target: QLineEdit, title: str, filt: str) -> None:
        start = ntpath.dirname(target.text()) if target.text() else ""
        path, _ = QFileDialog.getOpenFileName(self, title, start, filt)
        if path:
            target.setText(path.replace("/", "\\"))

    def _browse_dir(self, target: QLineEdit, title: str) -> None:
        path = QFileDialog.getExistingDirectory(self, title, target.text() or "")
        if path:
            target.setText(path.replace("/", "\\"))

    # 種別ごとのフォーム
    def _form_launch_app(self) -> None:
        path = self._line("path", r"C:\Program Files\...\app.exe")
        browse = W.button("参照…", "secondary", G.FOLDER,
                          on_click=guard(lambda: self._browse_file(path, "起動する exe を選ぶ", "実行ファイル (*.exe)")))
        self._row("exe の場所", self._wrap(W.hbox(path, browse)), "ショートカットではなく exe の実体を選んでください。管理者権限が必要な exe は起動しません。")
        args = QPlainTextEdit("\n".join(self.a.get("args") or []))
        args.setPlaceholderText("1行に1つずつ(空欄なら引数なし)")
        args.setFixedHeight(80)
        args.textChanged.connect(guard(self._validate))
        self._row("引数", args, "シェルを通さずにそのまま渡します(引用符や & は解釈されません)。")
        cwd = QLineEdit(str(self.a.get("cwd") or ""))
        cwd.setPlaceholderText("空欄なら exe のあるフォルダ")
        cwd.textChanged.connect(guard(lambda _t: self._validate()))
        cb = W.button("フォルダ…", "secondary", G.FOLDER, on_click=guard(lambda: self._browse_dir(cwd, "作業フォルダを選ぶ")))
        self._row("作業フォルダ(任意)", self._wrap(W.hbox(cwd, cb)))
        sir = QCheckBox("同じ exe が動いていたら起動しない")
        sir.setChecked(bool(self.a.get("skip_if_running", True)))
        self.body.addWidget(sir)
        self._getters.append(lambda d: d.update(
            path=path.text().strip(), args=[x for x in args.toPlainText().splitlines() if x.strip()],
            cwd=cwd.text().strip() or None, skip_if_running=sir.isChecked()))

    def _form_close_app(self) -> None:
        self._exe_field(exclude_games=True, audio_first=False)
        to = QDoubleSpinBox()
        to.setRange(1, 300)
        to.setDecimals(0)
        to.setSuffix(" 秒")
        to.setValue(float(self.a.get("timeout_s", 10)))
        self._row("待つ時間", to, "閉じる要求を送ってから、終了を待つ時間。過ぎたら「終了せず」と記録して次へ進みます。")
        fot = QCheckBox("時間内に終わらなければ、確認のうえ強制終了する")
        fot.setChecked(bool(self.a.get("force_on_timeout", False)))
        allowed = bool(self.module.service and self.module.service.config.allow_force_kill)
        if not allowed:
            fot.setToolTip("「動作設定」で強制終了を許可したときだけ働きます")
        self.body.addWidget(fot)
        note = W.label("強制終了は、動作設定で許可し、毎回の確認ダイアログで承認したときだけ行います。未保存のデータは失われます。"
                       + ("" if allowed else "(今は許可されていないため、チェックしても強制終了しません)"), "Mute", wrap=True)
        self.body.addWidget(note)
        self._getters.append(lambda d: d.update(timeout_s=float(to.value()), force_on_timeout=fot.isChecked()))

    def _form_power_plan(self) -> None:
        combo = QComboBox()
        combo.setMinimumWidth(420)
        reload_btn = W.icon_button(G.REFRESH, "powercfg /list を読み直す", kind="secondary")
        self._row("電源プラン", self._wrap(W.hbox(combo, reload_btn)),
                  "powercfg /list に出るプランから選びます。照合は GUID だけで行い、名前は表示用です。")
        cur = str(self.a.get("guid") or "").lower()

        def fill() -> None:
            combo.clear()
            schemes: list[PowerScheme] = []
            if self.module.service is not None:
                try:
                    schemes = self.module.service.backends.power.list_schemes()
                except Exception:  # noqa: BLE001
                    log.exception("powercfg /list を読めません")
            for s in schemes:
                combo.addItem(f"{s.name or '(名前なし)'}   —   {s.guid}" + ("   ・現在" if s.active else ""), s.guid)
            if cur and cur not in {s.guid for s in schemes}:
                combo.addItem(f"(一覧に無い) {cur}", cur)
            if not schemes:
                combo.addItem("(プランを読めません)", "")
            i = combo.findData(cur)
            combo.setCurrentIndex(max(0, i))

        fill()
        reload_btn.clicked.connect(guard(lambda _=False: fill()))
        combo.currentIndexChanged.connect(guard(lambda _i: self._validate()))
        self._getters.append(lambda d: d.__setitem__("guid", str(combo.currentData() or "")))

    def _form_master_volume(self) -> None:
        use = QCheckBox("音量を変える")
        lv = self.a.get("level")
        use.setChecked(lv is not None)
        sl = VolumeSlider(float(lv if lv is not None else 0.5))
        sl.setEnabled(use.isChecked())
        def on_use(on: bool) -> None:
            sl.setEnabled(on)
            self._validate()

        use.toggled.connect(guard(on_use))
        sl.changed.connect(guard(self._validate))
        self.body.addWidget(use)
        self._row("マスター音量", sl, "Windows の音量表示(0〜100)を 0.0〜1.0 のスカラーとして保存します。")
        mute = QComboBox()
        for text, v in (("ミュートは変えない", None), ("ミュートする", True), ("ミュートを解除する", False)):
            mute.addItem(text, v)
        mute.setCurrentIndex({None: 0, True: 1, False: 2}.get(self.a.get("mute"), 0))
        mute.currentIndexChanged.connect(guard(lambda _i: self._validate()))
        self._row("ミュート", mute)
        self._getters.append(lambda d: d.update(level=sl.level() if use.isChecked() else None, mute=mute.currentData()))

    def _form_app_volume(self) -> None:
        self._exe_field(exclude_games=False, audio_first=True)
        sl = VolumeSlider(float(self.a.get("level") or 0.5))
        sl.changed.connect(guard(self._validate))
        self._row("アプリの音量", sl, "その exe の全セッションに同じ値を設定します。音を出していないアプリはスキップされます。")
        self._getters.append(lambda d: d.__setitem__("level", sl.level()))

    def _form_open_path(self) -> None:
        path = self._line("path", r"C:\Users\...\Documents")
        fb = W.button("フォルダ…", "secondary", G.FOLDER, on_click=guard(lambda: self._browse_dir(path, "開くフォルダを選ぶ")))
        ff = W.button("ファイル…", "secondary", G.OPEN,
                      on_click=guard(lambda: self._browse_file(path, "開くファイルを選ぶ", "すべてのファイル (*.*)")))
        self._row("開くもの", self._wrap(W.hbox(path, fb, ff)), "実行ファイルやショートカットは開けません(起動は「アプリ起動」で)。")
        self._getters.append(lambda d: d.__setitem__("path", path.text().strip()))

    def _form_open_url(self) -> None:
        url = self._line("url", "https://example.com/")
        self.url_state = W.label("", "Mute")
        self._row("URL", url, "http / https だけ。ModeShift 自身は接続せず、既定のブラウザに渡すだけです。")
        self.body.addWidget(self.url_state)

        def upd(text: str) -> None:
            ok = url_ok(text.strip())
            self.url_state.setText(("✓ 開ける URL です" if ok else "✗ http:// か https:// で始まる URL にしてください") if text.strip() else "")
            self.url_state.setStyleSheet(f"color: {T.SUCCESS if ok else T.DANGER};")

        url.textChanged.connect(guard(upd))
        upd(url.text())
        self._getters.append(lambda d: d.__setitem__("url", url.text().strip()))

    def _form_layout_apply(self) -> None:
        name = QLineEdit(str(self.a.get("layout") or ""))
        name.setPlaceholderText("空欄なら既定のレイアウト")
        self._row("レイアウト名(LayoutKeep)", name)
        ws = QDoubleSpinBox()
        ws.setRange(0, 600)
        ws.setDecimals(1)
        ws.setSuffix(" 秒")
        ws.setValue(float(self.a.get("wait_s") or 0))
        self._row("待ち秒数", ws, "直前の手順でアプリを起動したとき、ウィンドウが出るまで LayoutKeep に待ってもらう秒数。")
        self._getters.append(lambda d: d.update(layout=name.text().strip() or None, wait_s=float(ws.value())))

    # 検証と確定
    def collect(self) -> dict[str, Any]:
        d = copy.deepcopy(self.a)
        for g in self._getters:
            g(d)
        return d

    def _validate(self) -> list[str]:
        _norm, errs = normalize_action(self.collect(), self.module.ctx.game_processes())
        self.err.setText("\n".join("・" + e.split(": ", 1)[-1] for e in errs))
        self.err.setVisible(bool(errs))
        self.ok.setEnabled(not errs)
        return errs

    def _accept(self) -> None:
        if not self._validate():
            self.accept()

    def result_action(self) -> dict[str, Any]:
        return self.collect()


# ------------------------------------------------------------------ 行の表示
class _ModeRow(QWidget):
    def __init__(self, m: dict[str, Any], accent: str, errors: list[str], confirmed: bool) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(10)
        dot = QFrame()
        dot.setFixedSize(10, 10)
        dot.setStyleSheet(f"background: {accent}; border-radius: 5px;")
        lay.addWidget(dot)
        box = QVBoxLayout()
        box.setSpacing(0)
        box.addWidget(W.label(str(m.get("label") or m.get("name") or "(無名)"), "H3"))
        box.addWidget(W.label(str(m.get("name") or ""), "Mute"))
        lay.addLayout(box, 1)
        if errors:
            g = W.Glyph(G.ERROR, 14, T.DANGER)
            g.setToolTip("\n".join(errors))
            lay.addWidget(g)
        elif not confirmed:
            g = W.Glyph(G.SHIELD, 14, T.WARN)
            g.setToolTip("未確認(次の実行時にプレビューで確認)")
            lay.addWidget(g)


class _ActionRow(QWidget):
    def __init__(self, i: int, a: dict[str, Any], errors: list[str], schemes: list[PowerScheme]) -> None:
        super().__init__()
        t = str(a.get("type"))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 10, 6)
        lay.setSpacing(10)
        num = QLabel(str(i))
        num.setFixedSize(24, 24)
        num.setAlignment(Qt.AlignmentFlag.AlignCenter)
        num.setStyleSheet(f"background: {T.SURFACE3}; color: {T.TEXT_DIM}; border-radius: 12px; font-size: 11px; font-weight: 700;")
        lay.addWidget(num)
        color = T.DANGER if errors else T.TEXT_DIM
        lay.addWidget(W.Glyph(TYPE_GLYPHS.get(t, G.ERROR), 15, color))
        box = QVBoxLayout()
        box.setSpacing(0)
        box.addWidget(W.label(TYPE_LABELS.get(t, f"未知の種別 {t}"), "H3"))
        s = W.label(action_summary(a, schemes), "Mute")
        s.setToolTip(action_summary(a, schemes))
        box.addWidget(s)
        lay.addLayout(box, 1)
        if errors:
            ch = Chip("要修正", T.DANGER, G.ERROR)
            ch.setToolTip("\n".join(errors))
            lay.addWidget(ch)


# ------------------------------------------------------------------ 本体
class ModeEditor(QWidget):
    saved = Signal()

    def __init__(self, module: ModeShiftModule) -> None:
        super().__init__()
        self.module = module
        self.draft: list[dict[str, Any]] = []
        self.dirty = False
        self._schemes: list[PowerScheme] = []
        self._cur = -1
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)
        root = QHBoxLayout()
        root.setSpacing(16)
        outer.addLayout(root)

        # 左: モード一覧
        left = QVBoxLayout()
        left.setSpacing(8)
        self.modes = QListWidget()
        self.modes.setMinimumWidth(240)
        self.modes.setMaximumWidth(280)
        self.modes.setMinimumHeight(360)
        self.modes.currentRowChanged.connect(guard(self._select))
        left.addWidget(self.modes, 1)
        lt = QHBoxLayout()
        lt.setSpacing(4)
        lt.addWidget(W.icon_button(G.ADD, "モードを追加", guard(self._add_mode), "secondary"))
        lt.addWidget(W.icon_button(G.COPY, "複製", guard(self._dup_mode), "secondary"))
        lt.addWidget(W.icon_button(G.UP, "上へ", guard(lambda: self._move_mode(-1)), "secondary"))
        lt.addWidget(W.icon_button(G.DOWN, "下へ", guard(lambda: self._move_mode(1)), "secondary"))
        lt.addStretch(1)
        lt.addWidget(W.icon_button(G.DELETE, "削除", guard(self._del_mode), "danger"))
        left.addLayout(lt)
        root.addLayout(left)

        # 右: 詳細
        self.stack = QStackedWidget()
        self.empty = W.EmptyState(G.MODE, "モードを選ぶか、+ で作ってください",
                                  "モードは「上から順に実行する手順の列」です。作ったモードは最初の1回だけプレビューで確認してから使えます。")
        self.stack.addWidget(self.empty)
        self.form = QWidget()
        self._build_form()
        self.stack.addWidget(self.form)
        root.addWidget(self.stack, 1)
        outer.addWidget(W.divider())
        outer.addLayout(self._foot)
        self.reload()

    # ---------------------------------------------------------------- 構築
    def _build_form(self) -> None:
        lay = QVBoxLayout(self.form)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        r1 = QHBoxLayout()
        r1.setSpacing(12)
        b1 = QVBoxLayout()
        b1.addWidget(W.label("表示名", "Eyebrow"))
        self.ed_label = QLineEdit()
        self.ed_label.setPlaceholderText("例: ゲーム")
        self.ed_label.textEdited.connect(guard(lambda t: self._set_field("label", t)))
        b1.addWidget(self.ed_label)
        r1.addLayout(b1, 1)
        b2 = QVBoxLayout()
        b2.addWidget(W.label("名前(CLI 用・半角英数)", "Eyebrow"))
        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText("例: game")
        self.ed_name.textEdited.connect(guard(lambda t: self._set_field("name", t.strip())))
        b2.addWidget(self.ed_name)
        r1.addLayout(b2, 1)
        lay.addLayout(r1)

        r2 = QHBoxLayout()
        r2.setSpacing(12)
        b3 = QVBoxLayout()
        b3.addWidget(W.label("ホットキー", "Eyebrow"))
        self.ed_hotkey = W.HotkeyEdit("")
        self.ed_hotkey.changed.connect(guard(lambda t: self._set_field("hotkey", t or None)))
        b3.addWidget(self.ed_hotkey)
        r2.addLayout(b3, 1)
        b4 = QVBoxLayout()
        b4.addWidget(W.label("色", "Eyebrow"))
        sw = QHBoxLayout()
        sw.setSpacing(6)
        self._swatches: list[tuple[str, QPushButton]] = []
        for key, c in palette():
            b = QPushButton()
            b.setCheckable(True)
            b.setFixedSize(26, 26)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(f"QPushButton {{ background: {c}; border: 2px solid {T.BG1}; border-radius: 13px; padding: 0; }}"
                            f"QPushButton:checked {{ border: 2px solid {T.TEXT}; }}")
            b.clicked.connect(guard(lambda _=False, kk=key: self._set_field("accent", kk)))
            sw.addWidget(b)
            self._swatches.append((key, b))
        sw.addStretch(1)
        b4.addLayout(sw)
        r2.addLayout(b4, 1)
        lay.addLayout(r2)

        self.state_row = QHBoxLayout()
        self.state_chip = Chip()
        self.state_row.addWidget(self.state_chip)
        self.state_row.addWidget(W.label("定義(名前とアクション)を変えると、次の実行時にもう一度プレビューで確認します。", "Mute", wrap=True), 1)
        lay.addLayout(self.state_row)

        ah = QHBoxLayout()
        ah.addWidget(W.label("アクション(上から順に実行)", "H3"))
        ah.addStretch(1)
        self.btn_add = W.button("追加", "secondary", G.ADD)
        menu = QMenu(self.btn_add)
        for t in ACTION_TYPES:
            act = menu.addAction(f"{TYPE_LABELS[t]}  —  {TYPE_HINTS[t]}")
            act.setData(t)
            act.triggered.connect(guard(lambda _=False, tt=t: self._add_action(tt)))
        self.btn_add.setMenu(menu)
        ah.addWidget(self.btn_add)
        lay.addLayout(ah)
        self.act_list = QListWidget()
        self.act_list.setMinimumHeight(230)
        self.act_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.act_list.itemDoubleClicked.connect(guard(lambda _i: self._edit_action()))
        lay.addWidget(self.act_list, 1)
        at = QHBoxLayout()
        at.setSpacing(4)
        at.addWidget(W.button("編集", "secondary", G.EDIT, on_click=guard(self._edit_action)))
        at.addWidget(W.icon_button(G.UP, "上へ", guard(lambda: self._move_action(-1)), "secondary"))
        at.addWidget(W.icon_button(G.DOWN, "下へ", guard(lambda: self._move_action(1)), "secondary"))
        at.addStretch(1)
        at.addWidget(W.icon_button(G.DELETE, "このアクションを削除", guard(self._del_action), "danger"))
        lay.addLayout(at)

        self.errbox = QFrame()
        self.errbox.setStyleSheet(f"QFrame {{ background: {T.alpha(T.DANGER, 0.08)}; border: 1px solid {T.alpha(T.DANGER, 0.35)};"
                                  f" border-radius: 10px; }} QLabel {{ border: none; }}")
        el = QVBoxLayout(self.errbox)
        el.setContentsMargins(12, 8, 12, 8)
        self.errtext = W.label("", wrap=True)
        self.errtext.setStyleSheet(f"color: {T.DANGER};")
        el.addWidget(self.errtext)
        lay.addWidget(self.errbox)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.dirty_pill = W.StatusPill("未保存の変更があります", "warn")
        self.saved_pill = W.StatusPill("保存しました", "ok")
        self._saved_fx = QGraphicsOpacityEffect(self.saved_pill)
        self.saved_pill.setGraphicsEffect(self._saved_fx)
        self.saved_pill.setVisible(False)
        foot.addWidget(self.dirty_pill)
        foot.addWidget(self.saved_pill)
        foot.addStretch(1)
        self.btn_discard = W.button("変更を破棄", "ghost", G.UNDO, on_click=guard(self._discard))
        self.btn_save = W.button("保存", "primary", G.SAVE, on_click=guard(self.save))
        foot.addWidget(self.btn_discard)
        foot.addWidget(self.btn_save)
        self._foot = foot

    # ---------------------------------------------------------------- データ
    def reload(self) -> None:
        """設定から読み直す(未保存の変更は捨てる)。"""
        sec = self.module.ctx.settings_dict()
        modes = sec.get("modes", [])
        self.draft = copy.deepcopy(modes) if isinstance(modes, list) else []
        self._refresh_schemes()
        self._set_dirty(False)
        self._render_modes(select=min(max(self._cur, 0), len(self.draft) - 1))

    def _refresh_schemes(self) -> None:
        svc = self.module.service
        if svc is None:
            return
        try:
            self._schemes = svc.backends.power.list_schemes()
        except Exception:  # noqa: BLE001
            self._schemes = []

    def _validated(self) -> Any:
        return validate_section({"modes": self.draft}, self.module.ctx.game_processes())

    def _set_dirty(self, d: bool) -> None:
        self.dirty = d
        self.dirty_pill.setVisible(d)
        self.btn_save.setEnabled(d)
        self.btn_discard.setEnabled(d)

    def _changed(self, *, rerender_modes: bool = True) -> None:
        self._set_dirty(True)
        if rerender_modes:
            self._render_modes(select=self._cur, keep_form=True)
        self._render_detail()

    def _render_modes(self, select: int = -1, keep_form: bool = False) -> None:
        cfg = self._validated()
        self.modes.blockSignals(True)
        self.modes.clear()
        for i, m in enumerate(self.draft):
            md = cfg.modes[i] if i < len(cfg.modes) else None
            confirmed = bool(md and isinstance(m, dict) and m.get("confirmed_hash") == md.def_hash)
            row = _ModeRow(m if isinstance(m, dict) else {}, mode_accent(m if isinstance(m, dict) else None, i),
                           md.errors if md else [], confirmed)
            it = QListWidgetItem()
            it.setSizeHint(row.sizeHint())
            self.modes.addItem(it)
            self.modes.setItemWidget(it, row)
        self.modes.blockSignals(False)
        if 0 <= select < len(self.draft):
            self.modes.setCurrentRow(select)
            self._cur = select
        else:
            self._cur = -1
        if not keep_form:
            self._render_detail()

    def _mode(self) -> dict[str, Any] | None:
        if 0 <= self._cur < len(self.draft) and isinstance(self.draft[self._cur], dict):
            return self.draft[self._cur]
        return None

    def _select(self, row: int) -> None:
        self._cur = row
        self._render_detail()

    def _render_detail(self) -> None:
        m = self._mode()
        if m is None:
            self.stack.setCurrentWidget(self.empty)
            return
        self.stack.setCurrentWidget(self.form)
        if self.ed_label.text() != str(m.get("label") or ""):
            self.ed_label.setText(str(m.get("label") or ""))
        if self.ed_name.text() != str(m.get("name") or ""):
            self.ed_name.setText(str(m.get("name") or ""))
        self.ed_hotkey.setText(str(m.get("hotkey") or ""))
        acc = accent_key(m, self._cur)
        for key, b in self._swatches:
            b.setChecked(key == acc)
        cfg = self._validated()
        md = cfg.modes[self._cur] if self._cur < len(cfg.modes) else None
        if md is None or not md.valid:
            self.state_chip.set("無効(下の内容を直してください)", T.DANGER, G.ERROR)
        elif m.get("confirmed_hash") == definition_hash(md.name, md.actions):
            self.state_chip.set("確認済み", T.SUCCESS, G.CHECK)
        else:
            self.state_chip.set("未確認", T.WARN, G.SHIELD)
        # アクション一覧
        raw_acts = m.get("actions")
        acts: list[Any] = raw_acts if isinstance(raw_acts, list) else []
        row = self.act_list.currentRow()
        self.act_list.clear()
        for i, a in enumerate(acts, 1):
            _n, errs = normalize_action(a, self.module.ctx.game_processes())
            w = _ActionRow(i, a if isinstance(a, dict) else {}, errs, self._schemes)
            it = QListWidgetItem()
            it.setSizeHint(w.sizeHint())
            self.act_list.addItem(it)
            self.act_list.setItemWidget(it, w)
        if acts:
            self.act_list.setCurrentRow(min(max(row, 0), len(acts) - 1))
        # エラー(FR-1: モードごとにその場で)
        msgs = (md.errors + [f"(注意) {x}" for x in md.warnings]) if md else []
        self.errtext.setText("\n".join("・" + x for x in msgs))
        self.errbox.setVisible(bool(msgs))

    def _set_field(self, key: str, value: Any) -> None:
        m = self._mode()
        if m is None:
            return
        if key == "name" and value and not NAME_RE.match(value):
            self.ed_name.setStyleSheet(f"border: 1px solid {T.DANGER};")
        elif key == "name":
            self.ed_name.setStyleSheet("")
        m[key] = value
        self._changed()

    # ---------------------------------------------------------------- モードの操作
    def _unique_name(self, base: str) -> str:
        names = {str(m.get("name")) for m in self.draft if isinstance(m, dict)}
        n, i = base, 2
        while n in names:
            n, i = f"{base}{i}", i + 1
        return n

    def _add_mode(self) -> None:
        label = W.text_input(self, "モードを追加", "表示名(例: ゲーム / 勉強 / 開発)", "")
        if label is None:
            return
        label = label.strip() or "新しいモード"
        ascii_base = "".join(ch for ch in label.lower() if ch.isascii() and (ch.isalnum() or ch in "_-")) or "mode"
        self.draft.append({"name": self._unique_name(ascii_base[:30]), "label": label, "hotkey": None,
                           "confirmed_hash": None, "actions": []})
        self._set_dirty(True)
        self._render_modes(select=len(self.draft) - 1)

    def _dup_mode(self) -> None:
        m = self._mode()
        if m is None:
            return
        d = copy.deepcopy(m)
        d["name"] = self._unique_name(str(m.get("name") or "mode") + "_copy")
        d["label"] = str(m.get("label") or m.get("name")) + "(コピー)"
        d["hotkey"] = None
        d["confirmed_hash"] = None
        self.draft.insert(self._cur + 1, d)
        self._set_dirty(True)
        self._render_modes(select=self._cur + 1)

    def _del_mode(self) -> None:
        m = self._mode()
        if m is None:
            return
        ok, _ = W.confirm(self, "モードを削除", f"モード「{m.get('label') or m.get('name')}」を削除します。保存するまでは元に戻せます。",
                          ok_text="削除", danger=True)
        if not ok:
            return
        del self.draft[self._cur]
        self._set_dirty(True)
        self._render_modes(select=min(self._cur, len(self.draft) - 1))

    def _move_mode(self, d: int) -> None:
        i, j = self._cur, self._cur + d
        if self._mode() is None or not (0 <= j < len(self.draft)):
            return
        self.draft[i], self.draft[j] = self.draft[j], self.draft[i]
        self._set_dirty(True)
        self._render_modes(select=j)

    # ---------------------------------------------------------------- アクションの操作
    def _acts(self) -> list[Any] | None:
        m = self._mode()
        if m is None:
            return None
        if not isinstance(m.get("actions"), list):
            m["actions"] = []
        acts: list[Any] = m["actions"]
        return acts

    def _add_action(self, t: str) -> None:
        acts = self._acts()
        if acts is None:
            return
        dlg = ActionDialog(self, self.module, default_action(t))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        acts.append(dlg.result_action())
        self._changed()
        self.act_list.setCurrentRow(len(acts) - 1)
        self._flash_row(len(acts) - 1)

    def _edit_action(self) -> None:
        acts = self._acts()
        r = self.act_list.currentRow()
        if acts is None or not (0 <= r < len(acts)):
            return
        a = acts[r]
        if not isinstance(a, dict) or a.get("type") not in ACTION_TYPES:
            W.message(self, "編集できません", "未知の種別のアクションはフォームで編集できません。削除して作り直してください。", kind="warn")
            return
        dlg = ActionDialog(self, self.module, a)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            acts[r] = dlg.result_action()
            self._changed()
            self.act_list.setCurrentRow(r)
            self._flash_row(r)

    def _move_action(self, d: int) -> None:
        acts = self._acts()
        r = self.act_list.currentRow()
        if acts is None or not (0 <= r < len(acts)) or not (0 <= r + d < len(acts)):
            return
        acts[r], acts[r + d] = acts[r + d], acts[r]
        self._changed()
        self.act_list.setCurrentRow(r + d)

    def _del_action(self) -> None:
        acts = self._acts()
        r = self.act_list.currentRow()
        if acts is None or not (0 <= r < len(acts)):
            return
        del acts[r]
        self._changed()

    def _flash_row(self, r: int) -> None:
        it = self.act_list.item(r)
        w = self.act_list.itemWidget(it) if it is not None else None
        if w is None:
            return
        fx = QGraphicsOpacityEffect(w)
        w.setGraphicsEffect(fx)
        a = QPropertyAnimation(fx, b"opacity", w)
        a.setDuration(420)
        a.setStartValue(0.2)
        a.setEndValue(1.0)
        a.setEasingCurve(QEasingCurve.Type.OutCubic)
        a.finished.connect(guard(lambda: w.setGraphicsEffect(None)))  # type: ignore[arg-type]
        a.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    # ---------------------------------------------------------------- 保存
    def _discard(self) -> None:
        if self.dirty:
            ok, _ = W.confirm(self, "変更を破棄", "保存していない変更を捨てて、保存済みの設定に戻します。", ok_text="破棄", danger=True)
            if not ok:
                return
        self.reload()

    def save(self) -> None:
        sec = self.module.ctx.settings_dict()
        sec["modes"] = copy.deepcopy(self.draft)
        err = self.module.save_section(sec)
        if err:
            W.message(self, "保存できませんでした", err, kind="error")
            return
        self._set_dirty(False)
        self._render_modes(select=self._cur)
        self._show_saved()
        self.saved.emit()

    def _show_saved(self) -> None:
        self.saved_pill.setVisible(True)
        a = QPropertyAnimation(self._saved_fx, b"opacity", self.saved_pill)
        a.setDuration(2400)
        a.setKeyValueAt(0.0, 0.0)
        a.setKeyValueAt(0.12, 1.0)
        a.setKeyValueAt(0.8, 1.0)
        a.setKeyValueAt(1.0, 0.0)
        a.finished.connect(guard(lambda: self.saved_pill.setVisible(False)))
        a.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
