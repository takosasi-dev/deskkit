# Control Center(メイン画面)。サイドバー+ホーム(モジュールカード・前面状態・通知履歴)+各モジュール画面+
# host 設定+ログ表示。モジュール画面の中身はモジュール自身の create_page() が作る(host は枠だけを持つ)。
# 閉じてもトレイ常駐は続く。無効なモジュールは import しないので、無効時は紹介画面と有効化スイッチだけを出す。
from __future__ import annotations

import datetime as _dt
import logging
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QObject,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPainterPath, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from deskkit import APP_NAME, __version__, autostart, catalog, paths
from deskkit.foreground import query_foreground
from deskkit.logging_setup import memory_tail
from deskkit.settings import SettingsError
from deskkit.ui import icons
from deskkit.ui import theme as T
from deskkit.ui.pickers import pick_running_exes
from deskkit.ui.theme import G
from deskkit.ui.usage_page import UsagePage
from deskkit.ui.widgets import (
    Card,
    FadeStack,
    Glyph,
    Hero,
    HotkeyEdit,
    ScrollPage,
    Segmented,
    SettingRow,
    StatTile,
    StatusPill,
    StringListEditor,
    ToggleSwitch,
    button,
    confirm,
    dark_titlebar,
    label,
    message,
)

if TYPE_CHECKING:
    from deskkit.host import Activity, Host

log = logging.getLogger("deskkit.host.ui")

STATE_TEXT = {"running": ("ok", "動作中"), "stopped": ("error", "停止中"), "disabled": ("off", "無効")}


# ================================================================== サイドバー
class NavItem(QAbstractButton):
    def __init__(self, key: str, text: str, glyph: str, accent: str) -> None:
        super().__init__()
        self.key = key
        self._text = text
        self._glyph = glyph
        self.accent = accent
        self._dot: str | None = None
        self._hover = 0.0
        self.setCheckable(True)
        self.setFixedHeight(40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._anim = QPropertyAnimation(self, b"hover", self)
        self._anim.setDuration(140)

    def _gh(self) -> float:
        return self._hover

    def _sh(self, v: float) -> None:
        self._hover = v
        self.update()

    hover = Property(float, _gh, _sh)

    def set_dot(self, color: str | None) -> None:
        self._dot = color
        self.update()

    def enterEvent(self, e: Any) -> None:  # noqa: N802
        self._anim.stop()
        self._anim.setEndValue(1.0)
        self._anim.start()
        super().enterEvent(e)

    def leaveEvent(self, e: Any) -> None:  # noqa: N802
        self._anim.stop()
        self._anim.setEndValue(0.0)
        self._anim.start()
        super().leaveEvent(e)

    def sizeHint(self) -> QSize:
        return QSize(200, 40)

    def paintEvent(self, _e: Any) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(8, 2, -8, -2)
        if self._hover > 0 and not self.isChecked():
            c = QColor(T.SURFACE2)
            c.setAlphaF(self._hover)
            path = QPainterPath()
            path.addRoundedRect(r, 9, 9)
            p.fillPath(path, c)
        on = self.isChecked()
        p.setFont(T.icon_font(15))
        p.setPen(QColor(self.accent if on else T.TEXT_DIM))
        p.drawText(QRectF(r.left() + 12, r.top(), 22, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._glyph)
        f = T.ui_font(13, QFont.Weight.DemiBold if on else QFont.Weight.Medium)
        p.setFont(f)
        p.setPen(QColor(T.TEXT if on else T.TEXT_DIM))
        p.drawText(QRectF(r.left() + 42, r.top(), r.width() - 60, r.height()), Qt.AlignmentFlag.AlignVCenter, self._text)
        if self._dot:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(self._dot))
            p.drawEllipse(QRectF(r.right() - 16, r.center().y() - 3.5, 7, 7))


class Indicator(QWidget):
    """選択中のナビ項目の背景。位置と色がアニメーションする。"""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._color = QColor(T.ACCENT)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_color(self, c: str) -> None:
        self._color = QColor(c)
        self.update()

    def paintEvent(self, _e: Any) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(8, 2, -8, -2)
        path = QPainterPath()
        path.addRoundedRect(r, 9, 9)
        bg = QColor(self._color)
        bg.setAlpha(34)
        p.fillPath(path, bg)
        bar = QPainterPath()
        bar.addRoundedRect(QRectF(r.left(), r.top() + 10, 3, r.height() - 20), 1.5, 1.5)
        p.fillPath(bar, self._color)


class Sidebar(QFrame):
    def __init__(self, on_select: Any) -> None:
        super().__init__()
        self.setFixedWidth(236)
        self.setStyleSheet(f"Sidebar, QFrame#SidebarRoot {{ background: {T.BG1}; border-right: 1px solid {T.BORDER}; }}")
        self.setObjectName("SidebarRoot")
        self._on_select = on_select
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 18, 0, 14)
        lay.setSpacing(2)
        brand = QHBoxLayout()
        brand.setContentsMargins(20, 0, 16, 14)
        logo = QLabel()
        logo.setPixmap(icons.render(34 * 2).scaled(34, 34, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        brand.addWidget(logo)
        bt = QVBoxLayout()
        bt.setSpacing(0)
        name = label(APP_NAME, "H2")
        bt.addWidget(name)
        bt.addWidget(label(f"QOL ツールキット  v{__version__}", "Mute"))
        brand.addLayout(bt, 1)
        lay.addLayout(brand)
        self.indicator = Indicator(self)
        self._anim = QPropertyAnimation(self.indicator, b"geometry", self)
        self._anim.setDuration(240)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.items: dict[str, NavItem] = {}
        self._add(lay, "home", "ホーム", G.HOME, T.ACCENT)
        self._add(lay, "usage", "利用状況", G.LIST, T.ACCENT)
        lay.addSpacing(10)
        eb = label("モジュール", "Eyebrow")
        eb.setContentsMargins(22, 4, 0, 4)
        lay.addWidget(eb)
        for m in catalog.MODULES:
            self._add(lay, m.name, m.title, m.glyph, m.accent)
        lay.addStretch(1)
        eb2 = label("システム", "Eyebrow")
        eb2.setContentsMargins(22, 4, 0, 4)
        lay.addWidget(eb2)
        self._add(lay, "settings", "設定", G.SETTINGS, T.ACCENT)
        self._add(lay, "logs", "ログ", G.LOG, T.ACCENT)
        self.indicator.lower()

    def _add(self, lay: QVBoxLayout, key: str, text: str, glyph: str, accent: str) -> None:
        it = NavItem(key, text, glyph, accent)
        it.clicked.connect(lambda _=False, k=key: self._on_select(k))
        lay.addWidget(it)
        self.items[key] = it

    def select(self, key: str, animate: bool = True) -> None:
        for k, it in self.items.items():
            it.setChecked(k == key)
        it = self.items[key]
        self.indicator.set_color(it.accent)
        target = it.geometry()
        if animate and self.indicator.width() > 0:
            self._anim.stop()
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self.indicator.setGeometry(target)
        self.indicator.show()

    def resizeEvent(self, e: Any) -> None:  # noqa: N802
        super().resizeEvent(e)
        for it in self.items.values():
            if it.isChecked():
                self.indicator.setGeometry(it.geometry())


# ================================================================== ホーム
HOTKEY_LIMIT = 12
# ホットキーの表示名(登録名 → 画面に出す名前)。知らない名前は登録名をそのまま読みやすくして出す
HOTKEY_LABELS = {
    "host.quick": "クイックアクション",
    "clipshelf.open_palette": "パレットを開く",
    "clipshelf.plain_text": "書式なしで貼り付け",
    "clipshelf.toggle_pause": "記録の一時停止/再開",
    "dropsort.undo_last": "直前の移動を元に戻す",
    "modeshift.undo": "モードを元に戻す",
    "layoutkeep.save": "今の配置を保存",
    "layoutkeep.apply": "配置を適用",
}


def greeting(now: _dt.datetime | None = None) -> str:
    hour = (now or _dt.datetime.now()).hour
    return "おはようございます" if 5 <= hour < 11 else ("こんにちは" if 11 <= hour < 18 else "こんばんは")


def hotkey_label(full: str, modes: dict[str, str]) -> str:
    """'modeshift.mode.game' → 'モード「ゲーム」に切り替え' など、ホットキーの登録名を画面向けの名前にする。"""
    if full in HOTKEY_LABELS:
        return HOTKEY_LABELS[full]
    mod, _, rest = full.partition(".")
    if mod == "modeshift" and rest.startswith("mode."):
        name = rest[len("mode."):]
        return f"モード「{modes.get(name, name)}」に切り替え"
    return rest.replace("_", " ").replace(".", " › ") or full


class _Clickable(QFrame):
    """全体をクリックできる行・カードの土台。子の部品(ボタン・スイッチ)のクリックはそちらが受け取る。"""

    def __init__(self, on_click: Any, hover_bg: bool = True) -> None:
        super().__init__()
        self._on_click = on_click
        self._pressed = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if hover_bg:
            self.setObjectName("ClickRow")
            self.setStyleSheet(f"QFrame#ClickRow {{ border-radius: 8px; }} QFrame#ClickRow:hover {{ background: {T.SURFACE2}; }}")

    def mousePressEvent(self, e: Any) -> None:  # noqa: N802
        self._pressed = e.button() == Qt.MouseButton.LeftButton
        e.accept()

    def mouseReleaseEvent(self, e: Any) -> None:  # noqa: N802
        try:
            if self._pressed and e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
                self._pressed = False
                self._on_click()
        except Exception:  # noqa: BLE001 - クリック処理で落とさない
            log.exception("クリックの処理で例外")
        self._pressed = False


class ModuleCard(Card):
    def __init__(self, host: Host, name: str, open_page: Any) -> None:
        info = catalog.info(name)
        super().__init__(hover=True, padding=18)
        self._host = host
        self.name = name
        self._open_page = open_page
        self._pressed = False
        self.setMinimumHeight(176)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"クリックで {info.title} の画面を開く")
        head = QHBoxLayout()
        g = Glyph(info.glyph, 20, info.accent)
        g.setFixedSize(44, 44)
        g.setStyleSheet(f"color: {info.accent}; background: {T.alpha(info.accent, 0.14)}; border: 1px solid {T.alpha(info.accent, 0.3)}; border-radius: 13px;")
        head.addWidget(g)
        tb = QVBoxLayout()
        tb.setSpacing(1)
        tb.addWidget(label(info.title, "H3"))
        tb.addWidget(label(info.tagline, "Mute"))
        head.addLayout(tb, 1)
        self.toggle = ToggleSwitch(False, info.accent)
        self.toggle.setToolTip("有効 / 無効")
        self.toggle.toggled.connect(self._toggled)
        head.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignTop)
        self.body.addLayout(head)
        self.pill = StatusPill()
        self.status = label("", "Dim", wrap=True)
        self.status.setMinimumHeight(34)
        self.body.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignLeft)
        self.body.addWidget(self.status)
        row = QHBoxLayout()
        self.retry = button("再起動", "secondary", G.REFRESH, lambda: self._host.loader.restart(self.name))
        row.addWidget(self.retry)
        row.addStretch(1)
        row.addWidget(button("開く", "ghost", G.CHEVRON, lambda: open_page(self.name)))
        self.body.addLayout(row)
        self.refresh()

    # カードのどこを押しても画面を開く(ボタン・スイッチはそれぞれの動作)
    def mousePressEvent(self, e: Any) -> None:  # noqa: N802
        self._pressed = e.button() == Qt.MouseButton.LeftButton
        e.accept()

    def mouseReleaseEvent(self, e: Any) -> None:  # noqa: N802
        try:
            if self._pressed and e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
                self._open_page(self.name)
        except Exception:  # noqa: BLE001
            log.exception("モジュールカードのクリックで例外")
        self._pressed = False

    def _toggled(self, on: bool) -> None:
        try:
            self._host.set_module_enabled(self.name, on)
        except SettingsError as e:
            message(self.window(), "保存できません", str(e), kind="error")
            self.toggle.set_checked_silent(not on)

    def refresh(self) -> None:
        slot = self._host.loader.slots.get(self.name)
        state = slot.state if slot else "disabled"
        kind, text = STATE_TEXT.get(state, ("off", state))
        self.pill.set_state(kind, text)
        enabled = bool(self._host.settings.module_section(self.name).get("enabled", False))
        self.toggle.set_checked_silent(enabled)
        if state == "stopped" and slot is not None:
            self.status.setText(slot.reason or "")
            self.status.setStyleSheet(f"color: {T.DANGER};")
        elif state == "running" and slot is not None and slot.ctx is not None:
            self.status.setText(slot.ctx.status_text or "動作中")
            self.status.setStyleSheet("")
        else:
            self.status.setText("オンにすると使えるようになります")
            self.status.setStyleSheet(f"color: {T.TEXT_MUTE};")
        self.retry.setVisible(state == "stopped")


class HomePage(ScrollPage):
    def __init__(self, host: Host, open_page: Any) -> None:
        super().__init__()
        self._host = host
        self.hero = Hero(greeting(), f"DeskKit は1つの常駐プロセスで{len(catalog.MODULES)}つの QOL ツールをまとめて動かします。", G.SPARKLE, T.ACCENT)
        self.sn_pill = StatusPill("", "warn")
        self.fg_pill = StatusPill("前面ウィンドウを確認中…", "info")
        self.as_pill = StatusPill("", "off")
        self.dpi_pill = StatusPill("", "off")
        self.hero.add_pill(self.sn_pill)
        self.hero.add_pill(self.fg_pill)
        self.hero.add_pill(self.as_pill)
        self.hero.add_pill(self.dpi_pill)
        self.resume_btn = button("一時停止を終わる", "primary", G.PLAY, host.resume)
        self.snooze_btn = button("1時間 一時停止", "secondary", G.PAUSE, lambda: host.snooze_for(60))
        self.snooze_btn.setToolTip("自動で動く処理を1時間止めます(トレイの「一時停止」で時間を選べます)")
        self.hero.add_action(self.resume_btn)
        self.hero.add_action(self.snooze_btn)
        self.hero.add_action(button("設定を再読み込み", "secondary", G.REFRESH, host.reload))
        self.add(self.hero)

        stats = QHBoxLayout()
        stats.setSpacing(12)
        self.t_running = StatTile("動作中のモジュール", "0", G.PLAY, T.SUCCESS)
        self.t_stopped = StatTile("停止中", "0", G.WARNING, T.DANGER)
        self.t_hotkeys = StatTile("登録ホットキー", "0", G.KEYBOARD, T.ACCENT)
        self.t_notices = StatTile("今日の通知", "0", G.INFO, T.INFO)
        for t in (self.t_running, self.t_stopped, self.t_hotkeys, self.t_notices):
            stats.addWidget(t)
        w = QWidget()
        w.setLayout(stats)
        self.add(w)

        grid = QGridLayout()
        grid.setSpacing(14)
        self.cards: dict[str, ModuleCard] = {}
        for i, m in enumerate(catalog.MODULES):
            c = ModuleCard(host, m.name, open_page)
            grid.addWidget(c, i // 2, i % 2)
            self.cards[m.name] = c
        gw = QWidget()
        gw.setLayout(grid)
        self.add(gw)

        lower = QHBoxLayout()
        lower.setSpacing(14)
        self.activity = Card("最近の通知", "モジュールからのお知らせ(本文は記録しません)。クリックで開きます", G.CLOCK, T.ACCENT)
        self.act_box = QVBoxLayout()
        self.act_box.setSpacing(2)
        self.activity.add_layout(self.act_box)
        lower.addWidget(self.activity, 3)
        self.keys = Card("ホットキー", "登録中の組み合わせ", G.KEYBOARD, T.ACCENT)
        self.keys_box = QVBoxLayout()
        self.keys_box.setSpacing(6)
        self.keys.add_layout(self.keys_box)
        lower.addWidget(self.keys, 2)
        lw = QWidget()
        lw.setLayout(lower)
        self.add(lw)
        self.finish()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(2000)
        host.signals.snooze_changed.connect(self._sync_snooze)
        self.refresh()

    def _tick(self) -> None:
        try:
            if not self.isVisible():
                return
            fg = query_foreground(self._host.settings.game_processes(),
                                  str(self._host.settings.host().get("fullscreen_detection", "rect")))
            if fg.exe == "deskkit":
                self.fg_pill.set_state("info", "前面: DeskKit")
                return
            why = fg.reason()
            names = {"game": "ゲーム中", "fullscreen": "全画面", "elevated": "管理者ウィンドウ",
                     "elevated_unknown": "権限不明のウィンドウ", "fullscreen_unknown": "全画面か不明"}
            if why is None:
                self.fg_pill.set_state("ok", f"前面: {fg.exe or '—'}  自動操作 可")
            else:
                self.fg_pill.set_state("warn", f"前面: {fg.exe or '—'}  {names.get(why, why)} → 自動操作を控えます")
        except Exception:  # noqa: BLE001
            log.exception("前面ウィンドウの表示更新で例外")

    def _sync_snooze(self) -> None:
        try:
            text = self._host.snooze.status_text()
            self.sn_pill.set_state("warn", text)
            self.sn_pill.setVisible(bool(text))
            manual = self._host.snooze.manual_active()
            self.resume_btn.setVisible(manual)
            self.snooze_btn.setVisible(not manual)
        except RuntimeError:  # 画面破棄後
            pass

    def refresh(self) -> None:
        self.hero.set_title(greeting())
        self._sync_snooze()
        for c in self.cards.values():
            c.refresh()
        slots = self._host.loader.slots.values()
        self.t_running.set_value(str(sum(1 for s in slots if s.state == "running")))
        self.t_stopped.set_value(str(sum(1 for s in slots if s.state == "stopped" and not s.name.startswith("_"))))
        names = self._host.hotkeys.registry.names()
        self.t_hotkeys.set_value(str(len(names)))
        today = _dt.date.today()
        self.t_notices.set_value(str(sum(1 for a in self._host.activities if a.ts.date() == today)))
        st = autostart.state()
        self.as_pill.set_state({"on": "ok", "off": "off", "mismatch": "warn"}[st],
                               {"on": "自動起動 オン", "off": "自動起動 オフ", "mismatch": "自動起動 パス不一致"}[st])
        dpi = self._host.dpi_awareness
        self.dpi_pill.set_state("ok" if dpi == "per_monitor_aware_v2" else "warn", f"DPI {dpi}")
        self._render_hotkeys(names)
        self._render_activity()

    def _render_hotkeys(self, names: list[str]) -> None:
        from deskkit.context import list_modes

        _clear(self.keys_box)
        if not names:
            self.keys_box.addWidget(label("登録されているホットキーはありません", "Mute"))
            return
        modes = dict(list_modes(self._host.settings.module_section("modeshift")))
        order = sorted(names, key=lambda n: (n.partition(".")[0] != "host", n))
        for n in order[:HOTKEY_LIMIT]:
            mod = n.partition(".")[0]
            row = QHBoxLayout()
            is_host = mod == "host"
            chip = QLabel(APP_NAME if is_host else catalog.info(mod).title)
            chip.setStyleSheet(f"color: {T.ACCENT if is_host else catalog.info(mod).accent}; font-weight: 600; font-size: 12px;")
            row.addWidget(chip)
            name_l = label(hotkey_label(n, modes), "Dim")
            name_l.setMinimumWidth(0)
            row.addWidget(name_l, 1)
            combo = self._host.hotkeys.combo_text(n)
            if combo:
                key = QLabel(combo)
                key.setStyleSheet(f"color: {T.TEXT}; background: {T.SURFACE2}; border: 1px solid {T.BORDER}; border-radius: 6px;"
                                  " padding: 1px 7px; font-size: 11px;")
                row.addWidget(key)
            self.keys_box.addLayout(row)
        rest = len(order) - HOTKEY_LIMIT
        if rest > 0:
            self.keys_box.addWidget(label(f"他 {rest} 件", "Mute"))

    def _render_activity(self) -> None:
        _clear(self.act_box)
        acts = self._host.activities[:8]
        if not acts:
            self.act_box.addWidget(label("まだ通知はありません", "Mute"))
        for a in acts:
            self.act_box.addWidget(_activity_row(a, self._host))

    def add_activity(self, _a: Activity) -> None:
        self._render_activity()
        today = _dt.date.today()
        self.t_notices.set_value(str(sum(1 for a in self._host.activities if a.ts.date() == today)))


def _activity_row(a: Activity, host: Host) -> QWidget:
    w = _Clickable(lambda: host.open_activity(a))
    w.setToolTip("クリックして開く")
    lay = QHBoxLayout(w)
    lay.setContentsMargins(6, 3, 6, 3)
    color = {"ok": T.SUCCESS, "warn": T.WARN, "error": T.DANGER}.get(a.level, T.INFO if a.source == "host" else catalog.info(a.source).accent)
    dot = QLabel("●")
    dot.setStyleSheet(f"color: {color}; font-size: 10px;")
    lay.addWidget(dot)
    lay.addWidget(label(a.ts.strftime("%H:%M"), "Mute"))
    src = QLabel(host.title_of(a.source))
    src.setStyleSheet(f"color: {color}; font-weight: 600; font-size: 12px;")
    lay.addWidget(src)
    t = label(a.title, "Dim")
    t.setMinimumWidth(0)
    lay.addWidget(t, 1)
    lay.addWidget(Glyph(G.CHEVRON, 10, T.TEXT_MUTE))
    return w


def _clear(lay: Any) -> None:
    while lay.count():
        it = lay.takeAt(0)
        if it.widget() is not None:
            it.widget().deleteLater()
        elif it.layout() is not None:
            _clear(it.layout())


# ================================================================== モジュール画面の枠
class ModuleContainer(QWidget):
    def __init__(self, host: Host, name: str) -> None:
        super().__init__()
        self._host = host
        self.name = name
        self.info = catalog.info(name)
        self._module_obj: Any = None
        self._content: QWidget | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QFrame()
        bar.setObjectName("ModBar")
        bar.setStyleSheet(f"QFrame#ModBar {{ background: {T.BG0}; border-bottom: 1px solid {T.BORDER}; }}")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(32, 10, 32, 10)
        crumb = QLabel(f"<span style='color:{T.TEXT_MUTE}'>モジュール  /  </span><b>{self.info.title}</b>")
        bl.addWidget(crumb)
        bl.addSpacing(10)
        self.pill = StatusPill()
        bl.addWidget(self.pill)
        bl.addStretch(1)
        self.restart_btn = button("再起動", "ghost", G.REFRESH, lambda: host.loader.restart(name))
        bl.addWidget(self.restart_btn)
        bl.addWidget(label("有効", "Dim"))
        self.toggle = ToggleSwitch(False, self.info.accent)
        self.toggle.toggled.connect(self._toggled)
        bl.addWidget(self.toggle)
        lay.addWidget(bar)
        self.holder = QVBoxLayout()
        self.holder.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(self.holder, 1)
        self.refresh()

    def _toggled(self, on: bool) -> None:
        try:
            self._host.set_module_enabled(self.name, on)
        except SettingsError as e:
            message(self.window(), "保存できません", str(e), kind="error")
            self.toggle.set_checked_silent(not on)

    def _set_content(self, w: QWidget) -> None:
        if self._content is not None:
            self.holder.removeWidget(self._content)
            self._content.deleteLater()
        self._content = w
        self.holder.addWidget(w)

    def refresh(self) -> None:
        slot = self._host.loader.slots.get(self.name)
        state = slot.state if slot else "disabled"
        kind, text = STATE_TEXT.get(state, ("off", state))
        self.pill.set_state(kind, text)
        self.toggle.set_checked_silent(bool(self._host.settings.module_section(self.name).get("enabled", False)))
        self.restart_btn.setVisible(state in ("running", "stopped"))
        mod = slot.module if slot and state == "running" else None
        if mod is not None and mod is self._module_obj and self._content is not None:
            return
        self._module_obj = mod
        if mod is not None and hasattr(mod, "create_page"):
            try:
                page = mod.create_page()
            except Exception as e:  # noqa: BLE001 - 画面生成の失敗で Control Center を落とさない
                log.exception("%s の画面生成で例外", self.name)
                page = self._error_page(f"画面を作れませんでした: {type(e).__name__}")
            self._set_content(page)
        elif state == "stopped" and slot is not None:
            self._set_content(self._error_page(slot.reason or "理由不明"))
        else:
            self._set_content(self._intro_page())

    def _intro_page(self) -> QWidget:
        pg = ScrollPage()
        hero = Hero(self.info.title, self.info.tagline, self.info.glyph, self.info.accent)
        hero.add_pill(StatusPill("無効", "off"))
        hero.add_action(button("有効にする", "primary", G.POWER, lambda: self.toggle.setChecked(True)))
        pg.add(hero)
        c = Card("このモジュールについて", None, G.INFO, self.info.accent)
        c.add(label(self.info.description, "Dim", wrap=True))
        c.add(label("有効にすると設定画面がここに表示されます。最初は安全のため「試運転」で動き、"
                    "実際にファイルやウィンドウを動かす前に予定だけを確認できます。", "Mute", wrap=True))
        pg.add(c)
        pg.finish()
        return pg

    def _error_page(self, reason: str) -> QWidget:
        pg = ScrollPage()
        hero = Hero(self.info.title, self.info.tagline, self.info.glyph, self.info.accent)
        hero.add_pill(StatusPill("停止中", "error"))
        pg.add(hero)
        c = Card("このモジュールは停止しています", "他のモジュールと DeskKit 本体は動き続けています", G.ERROR, T.DANGER)
        rl = label(reason, "Dim", wrap=True)
        rl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        c.add(rl)
        row = QHBoxLayout()
        row.addWidget(button("もう一度起動", "primary", G.REFRESH, lambda: self._host.loader.restart(self.name)))
        row.addWidget(button("ログフォルダを開く", "secondary", G.FOLDER, self._host.open_logs))
        row.addStretch(1)
        c.add_layout(row)
        pg.add(c)
        pg.finish()
        return pg


# ================================================================== 設定
class SettingsPage(ScrollPage):
    def __init__(self, host: Host) -> None:
        super().__init__()
        self._host = host
        hero = Hero("設定", "DeskKit 全体の動作と、全モジュール共通の決まりごと", G.SETTINGS, T.ACCENT)
        hero.add_action(button("settings.json を開く", "secondary", G.OPEN, host.open_settings_file))
        self.add(hero)
        hs = host.settings.host()

        gen = Card("全般", None, G.SETTINGS, T.ACCENT)
        self.as_toggle = ToggleSwitch(autostart.state() == "on")
        self.as_toggle.toggled.connect(lambda _on: self._toggle_autostart())
        gen.add(SettingRow("Windows 起動時に自動起動", "現在のユーザーのスタートアップ(HKCU の Run)に1つだけ登録します。管理者権限は使いません。", self.as_toggle, G.POWER))
        sw = ToggleSwitch(bool(hs.get("show_window_on_start", True)))
        sw.toggled.connect(lambda on: self._save_host({"show_window_on_start": on}))
        gen.add(SettingRow("起動したらこの画面を開く", "オフにするとトレイにだけ常駐します(自動起動時は常にトレイのみ)。", sw, G.HOME))
        ns = Segmented([("toast", "トースト"), ("balloon", "Windows 標準")], str(hs.get("notification_style", "toast")))
        ns.changed.connect(lambda v: self._save_host({"notification_style": v}))
        gen.add(SettingRow("通知の出し方", "トーストは画面右下にアニメーション付きで表示し、フォーカスを奪いません。", ns, G.INFO))
        test = button("テスト通知", "ghost", G.SPARKLE, lambda: host.notify("host", "これはテスト通知です", "クリックすると DeskKit を開きます", host.show_window))
        gen.add(SettingRow("通知の見え方を確かめる", None, test))
        self.add(gen)

        game = Card("ゲーム・全画面中の安全装置", "ここに載せたアプリが前面にある間、または全画面表示の間は、前面に作用する操作(貼り付け・ウィンドウ移動・アプリ起動など)を行いません。",
                    G.GAME, T.WARN)
        self.games = StringListEditor(sorted(host.settings.game_processes()), "exe 名(例: game.exe)",
                                      normalize=lambda s: s.strip().lower(),
                                      extra_buttons=[button("実行中から選ぶ", "secondary", G.APP, self._pick_games)])
        self.games.changed.connect(self._save_games)
        game.add(self.games)
        fs = Segmented([("rect", "画面全体を覆えば全画面"), ("off", "判定しない")], str(hs.get("fullscreen_detection", "rect")))
        fs.changed.connect(lambda v: self._save_host({"fullscreen_detection": v}))
        game.add(SettingRow("全画面の判定", "前面ウィンドウがモニタ全体を覆っているかで判定します。", fs, G.MONITOR))
        hold = ToggleSwitch(bool(hs.get("hold_notifications", True)))
        hold.toggled.connect(lambda on: self._save_host({"hold_notifications": on}))
        game.add(SettingRow("この間は通知を保留する", "エラー以外の通知をためておき、終わってから「保留中の通知 N 件」として1つにまとめて出します。"
                            "ホームの「最近の通知」にはすぐ記録されます。", hold, G.INFO))
        self.add(game)

        # ---- 一時停止
        sn = Card("一時停止", "自動で動く処理(DropSort の自動移動・LayoutKeep の自動適用・ModeShift の自動切替・ClipShelf の記録など)を"
                  "しばらく止めます。ボタンやクイックアクションで手で行う操作はそのまま使えます。DeskKit を再起動すると解除されます。",
                  G.PAUSE, T.ACCENT)
        srow = QHBoxLayout()
        self.snooze_pill = StatusPill()
        srow.addWidget(self.snooze_pill)
        srow.addStretch(1)
        for text, minutes in (("30分", 30), ("1時間", 60), ("再開するまで", None)):
            srow.addWidget(button(text, "secondary", None, partial(host.snooze_for, minutes)))
        self.resume_btn = button("再開", "primary", G.PLAY, host.resume)
        srow.addWidget(self.resume_btn)
        sn.add_layout(srow)
        quns = ToggleSwitch(bool(hs.get("snooze_follow_quns", False)))
        quns.toggled.connect(self._quns_toggled)
        sn.add(SettingRow("Windows がプレゼン中・通知を控えている間も一時停止", "プレゼンテーション表示・全画面の Direct3D・"
                          "Windows が通知を控えている状態(SHQueryUserNotificationState)の間も、一時停止として扱います。", quns, G.MONITOR))
        self.add(sn)
        host.signals.snooze_changed.connect(self._sync_snooze)
        self._sync_snooze()

        adv = Card("詳細", None, G.FILTER, T.ACCENT)
        # 数値欄は1段ごとに settings.json を書かない(入力が 700 ms 止まってからまとめて保存する)
        self._adv_timer = QTimer(self)
        self._adv_timer.setSingleShot(True)
        self._adv_timer.setInterval(700)
        self._adv_timer.timeout.connect(self._save_adv)
        self.lim = QSpinBox()
        self.lim.setRange(1, 100)
        self.lim.setValue(int(hs.get("handler_error_limit", 5)))
        self.lim.valueChanged.connect(lambda _v: self._adv_timer.start())
        adv.add(SettingRow("例外の許容回数", "同じ処理で例外がこの回数続いたら、そのモジュールだけを停止します。", self.lim, G.SHIELD))
        self.ret = QSpinBox()
        self.ret.setRange(1, 365)
        self.ret.setSuffix(" 日")
        self.ret.setValue(int(hs.get("log_retention_days", 14)))
        self.ret.valueChanged.connect(lambda _v: self._adv_timer.start())
        adv.add(SettingRow("ログの保存日数", "日ごとに分けて保存し、古いものから削除します。", self.ret, G.LOG))
        self.keep = QSpinBox()
        self.keep.setRange(1, 200)
        self.keep.setSuffix(" 世代")
        self.keep.setValue(int(hs.get("settings_history_keep", 20)))
        self.keep.valueChanged.connect(lambda _v: self._adv_timer.start())
        adv.add(SettingRow("設定の世代を残す数", "settings.json を変更するたびに自動で残す世代の数。古いものから消します。", self.keep, G.SAVE))
        self.add(adv)

        files = Card("ファイルの置き場所", None, G.FOLDER, T.ACCENT)
        for title, p in (("設定", paths.settings_path()), ("データ", paths.local_dir()), ("ログ", paths.log_dir())):
            row = QHBoxLayout()
            row.addWidget(label(title, "H3"))
            pl = label(str(p), "Mute")
            pl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row.addWidget(pl, 1)
            target = p if p.is_dir() else p.parent
            row.addWidget(button("開く", "ghost", G.OPEN, _opener(str(target))))
            files.add_layout(row)
        self.add(files)

        # ---- 見た目
        look = Card("見た目", None, G.EYE, T.ACCENT)
        theme_seg = Segmented([("dark", "ダーク"), ("light", "ライト"), ("system", "Windows に合わせる")], str(hs.get("theme", "dark")))
        self.restart_btn = button("再起動して反映", "primary", G.REFRESH, host.restart)
        self.restart_btn.hide()
        theme_seg.changed.connect(self._theme_changed)
        look.add(SettingRow("テーマ", "切り替えは再起動後に反映されます。", theme_seg, G.EYE))
        look.add(SettingRow("", None, self.restart_btn))
        look.add(SettingRow("はじめてガイド", "最初に表示した使い方の案内をもう一度見ます。",
                            button("ガイドを見る", "secondary", G.SPARKLE, host.show_onboarding), G.INFO))
        self.add(look)

        # ---- クイックアクション
        qa = Card("クイックアクション", "どこからでも検索窓を開き、全モジュールの操作を名前で実行します(ゲーム・全画面中は開きません)。",
                  G.LIGHTNING, T.ACCENT)
        self.qa_key = HotkeyEdit(str(hs.get("quick_action_hotkey") or ""))
        self.qa_key.changed.connect(self._qa_hotkey)
        qa.add(SettingRow("ホットキー", "空にすると使いません。", self.qa_key, G.KEYBOARD))
        qa.add(SettingRow("今すぐ開いてみる", None, button("開く", "secondary", G.LIGHTNING, host.open_quick_actions)))
        self.add(qa)

        # ---- アップデート
        upd = Card("アップデート", "GitHub の Releases から新しい版を確認します。通信はこの確認とダウンロードだけです。", G.DOWNLOAD, T.ACCENT)
        self.upd_pill = StatusPill()
        up_row = QHBoxLayout()
        up_row.addWidget(label(f"今の版  v{__version__}", "H3"))
        up_row.addWidget(self.upd_pill)
        up_row.addStretch(1)
        self.upd_check = button("今すぐ確認", "secondary", G.REFRESH, lambda: host.updates.check(manual=True))
        self.upd_go = button("更新する…", "primary", G.DOWNLOAD, host.updates.show_dialog)
        self.upd_back = button("前の版に戻す", "ghost", G.UNDO, self._rollback)
        for b in (self.upd_back, self.upd_check, self.upd_go):
            up_row.addWidget(b)
        upd.add_layout(up_row)
        ucfg = host.updates.cfg()
        auto = ToggleSwitch(bool(ucfg.get("auto_check", True)))
        auto.toggled.connect(lambda on: host.updates.save_cfg(auto_check=on))
        upd.add(SettingRow("自動で確認する", "起動30秒後と、その後1日1回。新しい版があれば通知します(勝手に入れ替えはしません)。", auto, G.CLOCK))
        self.repo = QLineEdit(str(ucfg.get("repo") or ""))
        self.repo.setPlaceholderText("ユーザー名/リポジトリ名")
        self.repo.setMinimumWidth(240)
        self.repo.editingFinished.connect(self._save_repo)
        upd.add(SettingRow("取得先リポジトリ", "GitHub の「ユーザー名/リポジトリ名」。DeskKit.exe と DeskKit.exe.sha256 が添付された最新リリースを使います。",
                           self.repo, G.LINK))
        host.updates.changed.connect(self._sync_update)
        self._sync_update()
        self.add(upd)

        # ---- バックアップ
        bk = Card("設定のバックアップ", "settings.json の内容(モジュールの設定・ルール・モード定義など)を1ファイルに書き出し、別の PC で読み込めます。"
                  "履歴・定型文などのデータ本体は含みません。", G.SAVE, T.ACCENT)
        brow = QHBoxLayout()
        brow.addWidget(button("書き出す…", "secondary", G.SAVE, self._export))
        brow.addWidget(button("読み込む…", "secondary", G.OPEN, self._import))
        brow.addStretch(1)
        bk.add_layout(brow)
        bk.add(label("書き出したファイルにはフォルダのパスや exe のパスが含まれます。人に渡すときは中身を確認してください。", "Mute", wrap=True))
        self.add(bk)

        # ---- 設定の世代(自動)
        self.hist = Card("設定の世代", "settings.json を変更するたびに、その時点の内容を自動で残しています(数秒以内の連続した変更は1つにまとめます)。"
                         "戻すと、今の設定も1世代として残してから置き換え、「設定を再読み込み」と同じように反映します。", G.CLOCK, T.ACCENT)
        self.hist_box = QVBoxLayout()
        self.hist_box.setSpacing(4)
        self.hist.add_layout(self.hist_box)
        self.add(self.hist)
        host.signals.snapshots_changed.connect(self.refresh_history)
        self.refresh_history()

        # ---- 診断
        dg = Card("診断レポート", "不具合を相談するときに貼り付ける、DeskKit の状態の要約です。版・OS・画面の倍率・モジュールの状態・"
                  "ホットキーの競合などを含み、パス・ユーザー名・exe 名・ウィンドウタイトル・クリップボードや URL の中身は含みません。",
                  G.INFO, T.ACCENT)
        dg.add(SettingRow("診断レポートをコピー", "クリップボードにコピーします。貼り付ける前に中身を確認できます。",
                          button("コピー", "secondary", G.COPY, host.copy_diagnostics), G.COPY))
        self.add(dg)

        # ---- ライセンス(H-5)
        lic = Card("ライセンス", "DeskKit.exe に含まれているほかのソフトウェア(Qt・Pillow・ffmpeg など)と、そのライセンスです。"
                   "ffmpeg などのソースの入手先も載せています。", G.LIST, T.ACCENT)
        lic.add(SettingRow("サードパーティのライセンス", "THIRD_PARTY_LICENSES.txt を表示します。",
                           button("開く", "secondary", G.OPEN, self._show_licenses), G.LIST))
        self.add(lic)

        about = Card(f"{APP_NAME} {__version__}", "この道具がしないこと", G.SHIELD, T.SUCCESS)
        for s in ("通信するのはアップデートの確認とダウンロード(GitHub)だけです。上の設定でオフにできます。",
                  "キーボードやマウスの入力を横取りしません(ホットキーは Windows の RegisterHotKey のみ)。",
                  "管理者権限を求めません。ゲームのメモリや入力には触れません。",
                  "クリップボードの本文・ファイルの中身や名前・フォルダの場所・URL をログに書きません。",
                  "ファイルを消すときは、確認してからごみ箱へ送ります(元に戻せます)。元の写真や動画は書き換えません。"):
            about.add(label("✓  " + s, "Dim", wrap=True))
        self.add(about)
        self.finish()

    def _show_licenses(self) -> None:
        from deskkit.ui.license_dialog import show_licenses

        show_licenses(self.window())

    def _save_adv(self) -> None:
        self._save_host({"handler_error_limit": int(self.lim.value()), "log_retention_days": int(self.ret.value()),
                         "settings_history_keep": int(self.keep.value())})

    def _quns_toggled(self, on: bool) -> None:
        self._save_host({"snooze_follow_quns": on})
        self._host.snooze.sync_settings()

    def _sync_snooze(self) -> None:
        try:
            text = self._host.snooze.status_text()
            self.snooze_pill.set_state("warn" if text else "ok", text or "動作中(一時停止していません)")
            self.resume_btn.setVisible(self._host.snooze.manual_active())
        except RuntimeError:  # 画面破棄後
            pass

    def refresh_history(self) -> None:
        from deskkit import backup

        try:
            _clear(self.hist_box)
            snaps = self._host.snapshots.list()
        except RuntimeError:
            return
        except OSError:
            snaps = []
        if not snaps:
            self.hist_box.addWidget(label("まだ世代はありません", "Mute"))
            return
        today = _dt.date.today()
        for i, s in enumerate(snaps):
            row = QHBoxLayout()
            when = f"{s.ts:%H:%M:%S}" if s.ts.date() == today else f"{s.ts:%Y-%m-%d %H:%M}"
            tl = label(when + ("  (最新)" if i == 0 else ""), "H3" if i == 0 else None)
            tl.setMinimumWidth(150)
            row.addWidget(tl)
            try:
                summ = "  ・  ".join(backup.summary(self._host.snapshots.read(s)))
            except SettingsError:
                summ = "(読めない世代です)"
            sl = label(summ, "Mute", wrap=True)  # 折り返さないとページ全体が横にはみ出す
            row.addWidget(sl, 1)
            if i > 0:
                row.addWidget(button("この世代に戻す", "ghost", G.UNDO, partial(self._restore, s)))
            self.hist_box.addLayout(row)

    def _restore(self, snap: Any) -> None:
        from deskkit import backup

        try:
            data = self._host.snapshots.read(snap)
        except SettingsError as e:
            message(self.window(), "この世代を読めません", str(e), kind="error")
            return
        text = f"{snap.ts:%Y-%m-%d %H:%M:%S} の設定\n" + "\n".join(backup.summary(data)) + "\n\n今の設定も1世代として残してから置き換えます。"
        ok, _ = confirm(self.window(), "この世代に戻しますか?", text, ok_text="戻す")
        if not ok:
            return
        try:
            self._host.restore_snapshot(snap)
        except (OSError, SettingsError) as e:
            message(self.window(), "戻せませんでした", str(e), kind="error")

    def _theme_changed(self, v: str) -> None:
        self._save_host({"theme": v})
        effective = ("light" if T.system_prefers_light() else "dark") if v == "system" else v
        self.restart_btn.setVisible(effective != T.MODE)

    def _qa_hotkey(self, text: str) -> None:
        self._save_host({"quick_action_hotkey": text})
        self._host.register_host_hotkeys()
        self._host.after_hotkey_registration()

    def _save_repo(self) -> None:
        from deskkit import updater

        v = self.repo.text().strip()
        if v and not updater.valid_repo(v):
            message(self.window(), "リポジトリ名が正しくありません", "「ユーザー名/リポジトリ名」の形で入力してください。", kind="warn")
            return
        self._host.updates.save_cfg(repo=v or updater.DEFAULT_REPO)

    def _sync_update(self) -> None:
        try:
            m = self._host.updates
            kind = {"available": "accent", "latest": "ok", "error": "warn", "checking": "info",
                    "downloading": "info", "installing": "info"}.get(m.state, "off")
            self.upd_pill.set_state(kind, m.status_text())
            self.upd_go.setVisible(m.state == "available")
            self.upd_check.setEnabled(m.state not in ("checking", "downloading", "installing"))
            self.upd_back.setVisible(m.has_previous())
        except RuntimeError:
            pass

    def _rollback(self) -> None:
        ok, _ = confirm(self.window(), "前の版に戻しますか?", "DeskKit.previous.exe と入れ替えて再起動します。", ok_text="戻して再起動")
        if ok:
            self._host.updates.rollback()

    def _export(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        from deskkit import backup

        name = f"DeskKit-settings-{_dt.date.today():%Y%m%d}.json"
        path, _ = QFileDialog.getSaveFileName(self.window(), "設定を書き出す", str(Path.home() / "Documents" / name), "JSON (*.json)")
        if not path:
            return
        try:
            backup.export(self._host.settings, Path(path))
        except OSError as e:
            message(self.window(), "書き出せません", str(e), kind="error")
            return
        self._host.notify("host", "設定を書き出しました", Path(path).name, None, level="ok")

    def _import(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        from deskkit import backup

        path, _ = QFileDialog.getOpenFileName(self.window(), "設定を読み込む", str(Path.home() / "Documents"), "JSON (*.json)")
        if not path:
            return
        try:
            data = backup.read(Path(path))
        except SettingsError as e:
            message(self.window(), "読み込めません", str(e), kind="error")
            return
        text = "\n".join(backup.summary(data)) + "\n\n今の設定は settings.json.bak-<日時> として残します。"
        ok, _ = confirm(self.window(), "この設定で置き換えますか?", text, ok_text="置き換える")
        if not ok:
            return
        try:
            backup.restore(self._host.settings, data)
        except (OSError, SettingsError) as e:
            message(self.window(), "置き換えられません", str(e), kind="error")
            return
        self._host.reload()

    def _save_host(self, values: dict[str, Any]) -> None:
        try:
            self._host.settings.write_host(values)
        except SettingsError as e:
            message(self.window(), "保存できません", str(e), kind="error")

    def _save_games(self, items: list[str]) -> None:
        try:
            self._host.settings.write_game_processes(items)
        except SettingsError as e:
            message(self.window(), "保存できません", str(e), kind="error")

    def _pick_games(self) -> None:
        for n in pick_running_exes(self.window(), "ゲームとして扱うアプリを選ぶ", set(self.games.items())):
            self.games.add_value(n)

    def _toggle_autostart(self) -> None:
        try:
            self._host.toggle_autostart()
        finally:
            self.as_toggle.set_checked_silent(autostart.state() == "on")

    def sync(self) -> None:
        self.as_toggle.set_checked_silent(autostart.state() == "on")


def _opener(path: str) -> Any:
    def go() -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    return go


# ================================================================== ログ
class _LogRelay(QObject):
    """ログの行を GUI スレッドへ運ぶ中継。logging はどのスレッドからでも呼ばれるので、画面にはここから先でだけ触る。"""

    record = Signal(object)


class LogsPage(QWidget):
    LEVELS = {"all": 0, "info": 20, "warn": 30, "error": 40}

    def __init__(self, host: Host) -> None:
        super().__init__()
        self._host = host
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 26, 32, 26)
        lay.setSpacing(14)
        head = QHBoxLayout()
        tb = QVBoxLayout()
        tb.addWidget(label("ログ", "H1"))
        tb.addWidget(label("本文・URL・ウィンドウタイトルは記録しない方針です。共有するときも安心して添付できます。", "Dim"))
        head.addLayout(tb, 1)
        head.addWidget(button("フォルダを開く", "secondary", G.FOLDER, host.open_logs))
        lay.addLayout(head)
        bar = QHBoxLayout()
        self.level = Segmented([("all", "すべて"), ("info", "情報"), ("warn", "警告"), ("error", "エラー")], "all")
        self.level.changed.connect(lambda _v: self.rebuild())
        bar.addWidget(self.level)
        self.source = QComboBox()
        self.source.addItem("全モジュール", "")
        self.source.addItem(f"{APP_NAME} 本体", "deskkit.host")
        for m in catalog.MODULES:
            self.source.addItem(m.title, f"deskkit.{m.name}")
        self.source.currentIndexChanged.connect(lambda _i: self.rebuild())
        bar.addWidget(self.source)
        self.search = QLineEdit()
        self.search.setPlaceholderText("絞り込み")
        # 1文字ごとに全件を描き直さない(200 ms 入力が止まってから絞り込む)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self.rebuild)
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        bar.addWidget(self.search, 1)
        lay.addLayout(bar)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(3000)
        mono = QFont("Cascadia Mono")
        mono.setFamilies(["Cascadia Mono", "Consolas", "MS Gothic"])
        mono.setPixelSize(12)
        self.view.setFont(mono)
        self.view.setStyleSheet(f"QPlainTextEdit {{ background: {T.SURFACE}; border-radius: 12px; padding: 10px; }}")
        lay.addWidget(self.view, 1)
        self._relay = _LogRelay(self)
        self._relay.record.connect(self._on_record_gui, Qt.ConnectionType.QueuedConnection)
        memory_tail.listeners.append(self._on_record)
        self.destroyed.connect(lambda _o=None, cb=self._on_record: _remove_listener(cb))
        self.rebuild()

    def _match(self, r: logging.LogRecord) -> bool:
        if r.levelno < self.LEVELS.get(self.level.value(), 0):
            return False
        src = self.source.currentData()
        if src and not r.name.startswith(src):
            return False
        q = self.search.text().strip().lower()
        return not q or q in r.getMessage().lower()

    def _append(self, r: logging.LogRecord) -> None:
        color = T.DANGER if r.levelno >= 40 else T.WARN if r.levelno >= 30 else T.TEXT_DIM
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cur = self.view.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        ts = _dt.datetime.fromtimestamp(r.created).strftime("%H:%M:%S")
        tfmt = QTextCharFormat()
        tfmt.setForeground(QColor(T.TEXT_MUTE))
        cur.insertText(f"{ts}  ", tfmt)
        nfmt = QTextCharFormat()
        nfmt.setForeground(QColor(T.ACCENT))
        cur.insertText(f"{r.name.replace('deskkit.', ''):<16} ", nfmt)
        cur.insertText(f"{r.levelname:<7} {r.getMessage()}\n", fmt)

    def rebuild(self) -> None:
        self.view.clear()
        for r in list(memory_tail.records):
            if self._match(r):
                self._append(r)
        self.view.moveCursor(QTextCursor.MoveOperation.End)

    def _on_record(self, r: logging.LogRecord) -> None:
        """logging のスレッドから呼ばれる。部品には触らず、GUI スレッドへ送るだけ。"""
        try:
            self._relay.record.emit(r)
        except RuntimeError:  # 画面破棄後
            _remove_listener(self._on_record)

    def _on_record_gui(self, r: logging.LogRecord) -> None:
        try:
            if self._match(r):
                self._append(r)
        except RuntimeError:  # 画面破棄後
            pass


def _remove_listener(cb: Any) -> None:
    try:
        memory_tail.listeners.remove(cb)
    except ValueError:
        pass


# ================================================================== ウィンドウ
class ControlCenter(QMainWindow):
    def __init__(self, host: Host) -> None:
        super().__init__()
        self._host = host
        self.setWindowTitle(f"{APP_NAME} — Control Center")
        self.setWindowIcon(icons.app_icon())
        self.resize(1220, 800)
        self.setMinimumSize(1000, 660)
        root = QWidget()
        root.setObjectName("Root")
        rl = QHBoxLayout(root)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        self.sidebar = Sidebar(self.open_page)
        rl.addWidget(self.sidebar)
        self.stack = FadeStack()
        rl.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.pages: dict[str, QWidget] = {}
        self.home = HomePage(host, self.open_page)
        self._add_page("home", self.home)
        self.modules: dict[str, ModuleContainer] = {}
        for m in catalog.MODULES:
            c = ModuleContainer(host, m.name)
            self.modules[m.name] = c
            self._add_page(m.name, c)
        self.usage = UsagePage(host)
        self._add_page("usage", self.usage)
        self.settings_page = SettingsPage(host)
        self._add_page("settings", self.settings_page)
        self._add_page("logs", LogsPage(host))
        host.signals.module_changed.connect(self._module_changed)
        host.signals.activity.connect(self._activity)
        host.signals.settings_reloaded.connect(self._reloaded)
        host.signals.settings_replaced.connect(self._rebuild_settings)
        self._hint_shown = False
        self._refresh_dots()
        QTimer.singleShot(0, lambda: self.open_page("home", animate=False))
        dark_titlebar(self)

    def _add_page(self, key: str, w: QWidget) -> None:
        self.pages[key] = w
        self.stack.addWidget(w)

    def open_page(self, key: str, animate: bool = True) -> None:
        if key not in self.pages:
            return
        self.sidebar.select(key, animate)
        self.stack.switch_to(self.pages[key])
        if key == "home":
            self.home.refresh()
        elif key == "usage":
            self.usage.refresh()

    def _module_changed(self, name: str) -> None:
        try:
            if name in self.modules:
                self.modules[name].refresh()
            self.home.refresh()
            self._refresh_dots()
        except RuntimeError:
            pass

    def _activity(self, a: Activity) -> None:
        try:
            self.home.add_activity(a)
        except RuntimeError:
            pass

    def _reloaded(self) -> None:
        for c in self.modules.values():
            c.refresh()
        self.home.refresh()
        self.settings_page.sync()
        self._refresh_dots()

    def _rebuild_settings(self) -> None:
        """設定の再読み込み・世代の復元の後、設定画面を今の値で作り直す(表示中ならそのまま差し替える)。"""
        old = self.settings_page
        new = SettingsPage(self._host)
        idx = self.stack.indexOf(old)
        self.stack.insertWidget(idx, new)
        if self.stack.currentWidget() is old:
            pos = old.verticalScrollBar().value()
            self.stack.setCurrentWidget(new)
            QTimer.singleShot(0, lambda: new.verticalScrollBar().setValue(pos))
        self.stack.removeWidget(old)
        old.deleteLater()
        self.pages["settings"] = new
        self.settings_page = new

    def _refresh_dots(self) -> None:
        for m in catalog.MODULES:
            slot = self._host.loader.slots.get(m.name)
            st = slot.state if slot else "disabled"
            self.sidebar.items[m.name].set_dot({"running": T.SUCCESS, "stopped": T.DANGER}.get(st))

    def closeEvent(self, e: Any) -> None:  # noqa: N802
        if self._host.quitting:
            e.accept()
            return
        e.ignore()
        self.hide()
        if not self._hint_shown:
            self._hint_shown = True
            self._host.notify("host", "トレイで動作を続けています", "アイコンをクリックすると、いつでもこの画面を開けます", self._host.show_window)

    def ask_quit(self) -> None:
        ok, _ = confirm(self, "DeskKit を終了しますか?", "すべてのモジュールが止まります。", ok_text="終了", danger=True)
        if ok:
            self._host.quit()

