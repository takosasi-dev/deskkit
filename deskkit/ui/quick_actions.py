# クイックアクション: ホットキーで開く検索窓から、全モジュールの操作(トレイ項目・ctx.add_quick_action)と
# DeskKit 本体の操作を名前で探して実行する。候補はトレイメニューをその場で読むので常に同期している。
# キー操作はこの窓の中のイベントだけ(グローバルなフックは使わない)。実行は窓を閉じて前面が戻ってから行う。
from __future__ import annotations

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QEasingCurve, QModelIndex, QPersistentModelIndex, QPoint, QPropertyAnimation, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QKeyEvent, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from deskkit import APP_NAME, catalog
from deskkit.ui import theme as T
from deskkit.ui.theme import G
from deskkit.ui.widgets import Glyph, label, shadow

if TYPE_CHECKING:
    from deskkit.host import Host


@dataclass
class Entry:
    title: str
    path: str
    source: str  # "host" またはモジュール名
    glyph: str
    run: Callable[[], Any]
    keywords: str = ""
    state: str | None = None  # "オン" / "オフ" など

    def haystack(self) -> str:
        return _norm(f"{self.title} {self.path} {self.keywords} {catalog.info(self.source).title}")


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s).lower()


def collect(host: Host) -> list[Entry]:
    out: list[Entry] = []
    hg = G.SPARKLE
    out.append(Entry("Control Center を開く", APP_NAME, "host", G.HOME, host.show_window, "ホーム 画面 開く"))
    for m in catalog.MODULES:
        out.append(Entry(f"{m.title} の画面を開く", APP_NAME, "host", m.glyph, _call(host.show_window, m.name),
                         f"{m.tagline} 設定"))
        enabled = bool(host.settings.module_section(m.name).get("enabled", False))
        out.append(Entry(f"{m.title} を{'無効' if enabled else '有効'}にする", APP_NAME, "host", G.POWER,
                         _call(host.set_module_enabled, m.name, not enabled), "オン オフ 切り替え",
                         "オン" if enabled else "オフ"))
    out.append(Entry("利用状況を開く", APP_NAME, "host", G.LIST, lambda: host.show_window("usage"), "統計 グラフ"))
    out.append(Entry("設定を開く", APP_NAME, "host", G.SETTINGS, lambda: host.show_window("settings"), "テーマ 自動起動"))
    out.append(Entry("ログを開く", APP_NAME, "host", G.LOG, lambda: host.show_window("logs"), ""))
    out.append(Entry("設定を再読み込み", APP_NAME, "host", G.REFRESH, host.reload, "リロード"))
    out.append(Entry("アップデートを確認", APP_NAME, "host", G.DOWNLOAD, lambda: host.updates.check(manual=True), "更新 バージョン"))
    out.append(Entry("はじめてガイドを見る", APP_NAME, "host", hg, host.show_onboarding, "チュートリアル 使い方"))
    # 一時停止(H2)・診断(H4)
    if host.snooze.manual_active():
        out.append(Entry("一時停止を終わる(再開)", APP_NAME, "host", G.PLAY, host.resume, "再開 スヌーズ 解除",
                         "停止中"))
    for label_, minutes in (("30分", 30), ("1時間", 60), ("再開するまで", None)):
        out.append(Entry(f"一時停止する({label_})", APP_NAME, "host", G.PAUSE, _call(host.snooze_for, minutes),
                         "スヌーズ 止める 休止 自動"))
    out.append(Entry("診断レポートをコピー", APP_NAME, "host", G.COPY, host.copy_diagnostics, "不具合 報告 サポート"))
    # トレイ項目(モジュールが追加したもの)
    for name, mm in list(host.tray._modules.items()):  # noqa: SLF001
        info = catalog.info(name)
        _walk_menu(mm.menu, [info.title], name, info.glyph, out, skip=(mm.status, mm.sep))
    # モジュールが追加したクイックアクション
    for name, slot in host.loader.slots.items():
        if slot.ctx is None or slot.state != "running":
            continue
        info = catalog.info(name)
        for qa in slot.ctx.quick_actions:
            if qa.enabled is not None:
                try:
                    if not qa.enabled():
                        continue
                except Exception:  # noqa: BLE001
                    continue
            out.append(Entry(qa.label, info.title, name, qa.glyph or info.glyph, qa.callback, qa.keywords))
    return out


def _call(fn: Callable[..., Any], *args: Any) -> Callable[[], Any]:
    def go() -> Any:
        return fn(*args)

    return go


def _walk_menu(menu: QMenu, path: list[str], source: str, glyph: str, out: list[Entry], skip: tuple[Any, ...] = ()) -> None:
    for act in menu.actions():
        if act in skip or act.isSeparator() or not act.isVisible():
            continue
        sub = act.menu()
        if sub is not None:
            _walk_menu(sub, [*path, act.text()], source, glyph, out)  # type: ignore[arg-type]
            continue
        if not act.isEnabled():
            continue
        state = ("オン" if act.isChecked() else "オフ") if act.isCheckable() else None
        out.append(Entry(act.text().replace("&", ""), " › ".join(path), source, glyph, act.trigger, "", state))


def rank(entries: list[Entry], query: str, mru: list[str]) -> list[Entry]:
    terms = [t for t in _norm(query).split() if t]
    scored: list[tuple[float, Entry]] = []
    for e in entries:
        hay = e.haystack()
        if not all(t in hay for t in terms):
            continue
        title = _norm(e.title)
        score = 0.0
        if terms:
            if title.startswith(terms[0]):
                score += 3
            elif terms[0] in title:
                score += 2
        key = f"{e.path}|{e.title}"
        if key in mru:
            score += 1.5 - mru.index(key) * 0.1
        if e.source != "host":
            score += 0.2
        scored.append((score, e))
    scored.sort(key=lambda x: -x[0])
    return [e for _s, e in scored]


class _Delegate(QStyledItemDelegate):
    H = 52

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex) -> QSize:
        return QSize(10, self.H)  # 幅はビューに合わせる(横スクロールを出さない)

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex) -> None:
        try:
            e: Entry = index.data(Qt.ItemDataRole.UserRole)
            r = option.rect.adjusted(6, 2, -6, -2)
            p.save()
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            accent = QColor(T.ACCENT if e.source == "host" else catalog.info(e.source).accent)
            if option.state & QStyle.StateFlag.State_Selected:
                bg = QColor(accent)
                bg.setAlpha(40)
                path = QPainterPath()
                path.addRoundedRect(r, 10, 10)
                p.fillPath(path, bg)
                bar = QPainterPath()
                bar.addRoundedRect(r.left(), r.top() + 12, 3, r.height() - 24, 1.5, 1.5)
                p.fillPath(bar, accent)
            icon = QRect(r.left() + 12, r.top() + 9, 30, 30)
            ib = QColor(accent)
            ib.setAlpha(30)
            ip = QPainterPath()
            ip.addRoundedRect(icon, 9, 9)
            p.fillPath(ip, ib)
            p.setFont(T.icon_font(14))
            p.setPen(accent)
            p.drawText(icon, Qt.AlignmentFlag.AlignCenter, e.glyph)
            p.setFont(T.ui_font(14))
            p.setPen(QColor(T.TEXT))
            tx = r.left() + 54
            right_w = 70 if e.state else 0
            p.drawText(QRect(tx, r.top() + 6, r.width() - 70 - right_w, 22), Qt.AlignmentFlag.AlignVCenter, e.title)
            p.setFont(T.ui_font(11))
            p.setPen(QColor(T.TEXT_MUTE))
            p.drawText(QRect(tx, r.top() + 26, r.width() - 70 - right_w, 18), Qt.AlignmentFlag.AlignVCenter, e.path)
            if e.state:
                on = e.state == "オン"
                c = QColor(T.SUCCESS if on else T.TEXT_MUTE)
                chip = QRect(r.right() - 64, r.center().y() - 11, 52, 22)
                cb = QColor(c)
                cb.setAlpha(34)
                cp = QPainterPath()
                cp.addRoundedRect(chip, 11, 11)
                p.fillPath(cp, cb)
                p.setPen(c)
                p.setFont(T.ui_font(11))
                p.drawText(chip, Qt.AlignmentFlag.AlignCenter, e.state)
            p.restore()
        except Exception:  # noqa: BLE001 - 描画で落とさない
            pass


class QuickActions(QWidget):
    WIDTH = 640

    def __init__(self, host: Host) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._host = host
        self._mru: list[str] = []
        self._entries: list[Entry] = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        self.panel = QFrame()
        self.panel.setObjectName("QPanel")
        self.panel.setStyleSheet(f"QFrame#QPanel {{ background: {T.BG1}; border: 1px solid {T.BORDER_HI}; border-radius: 18px; }}")
        shadow(self.panel, 48, 170 if not T.IS_LIGHT else 70, 14)
        outer.addWidget(self.panel)
        lay = QVBoxLayout(self.panel)
        lay.setContentsMargins(14, 14, 14, 10)
        lay.setSpacing(8)
        row = QHBoxLayout()
        row.setContentsMargins(8, 0, 6, 0)
        row.addWidget(Glyph(G.LIGHTNING, 18, T.ACCENT))
        self.search = QLineEdit()
        self.search.setPlaceholderText("何をしますか?(例: ゲーム、配置、元に戻す、パレット)")
        self.search.setStyleSheet(f"QLineEdit {{ background: transparent; border: none; font-size: 17px; padding: 8px 4px; color: {T.TEXT}; }}")
        self.search.textChanged.connect(self._refilter)
        self.search.installEventFilter(self)
        row.addWidget(self.search, 1)
        lay.addLayout(row)
        sep = QFrame()
        sep.setObjectName("Divider")
        lay.addWidget(sep)
        self.list = QListWidget()
        self.list.setItemDelegate(_Delegate(self.list))
        self.list.setStyleSheet("QListWidget { background: transparent; border: none; } QListWidget::item { border: none; }")
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setFixedHeight(_Delegate.H * 7 + 8)
        self.list.itemActivated.connect(lambda _it: self._run_current())
        self.list.itemClicked.connect(lambda _it: self._run_current())
        lay.addWidget(self.list)
        self.empty = label("一致する操作がありません", "Mute")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.hide()
        lay.addWidget(self.empty)
        foot = label("↑↓ 選択   Enter 実行   Esc 閉じる", "Mute")
        foot.setContentsMargins(10, 2, 0, 0)
        lay.addWidget(foot)
        self.setFixedWidth(self.WIDTH + 48)

    def open(self) -> None:
        self._entries = collect(self._host)
        self.search.clear()
        self._refilter("")
        scr = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        geo = scr.availableGeometry()
        self.adjustSize()
        target = QPoint(geo.center().x() - self.width() // 2, geo.top() + int(geo.height() * 0.18))
        self.move(target - QPoint(0, 16))
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self.activateWindow()
        self.search.setFocus()
        a1 = QPropertyAnimation(self, b"windowOpacity", self)
        a1.setDuration(150)
        a1.setEndValue(1.0)
        a1.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        a2 = QPropertyAnimation(self, b"pos", self)
        a2.setDuration(220)
        a2.setEndValue(target)
        a2.setEasingCurve(QEasingCurve.Type.OutCubic)
        a2.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def _refilter(self, text: str) -> None:
        self.list.clear()
        for e in rank(self._entries, text, self._mru)[:60]:
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, e)
            self.list.addItem(it)
        has = self.list.count() > 0
        self.list.setVisible(has)
        self.empty.setVisible(not has)
        if has:
            self.list.setCurrentRow(0)

    def _run_current(self) -> None:
        it = self.list.currentItem()
        if it is None:
            return
        e: Entry = it.data(Qt.ItemDataRole.UserRole)
        key = f"{e.path}|{e.title}"
        if key in self._mru:
            self._mru.remove(key)
        self._mru.insert(0, key)
        del self._mru[10:]
        self.hide()

        def go() -> None:
            try:
                e.run()
            except Exception:  # noqa: BLE001
                import logging

                logging.getLogger("deskkit.host.ui").exception("クイックアクションの実行で例外")

        QTimer.singleShot(150, go)  # 窓を閉じて前面ウィンドウが戻ってから実行する

    def eventFilter(self, obj: Any, ev: Any) -> bool:  # noqa: N802
        try:
            if obj is self.search and isinstance(ev, QKeyEvent) and ev.type() == QKeyEvent.Type.KeyPress:
                k = ev.key()
                if k in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                    n = self.list.count()
                    if n:
                        step = 1 if k == Qt.Key.Key_Down else -1
                        self.list.setCurrentRow((self.list.currentRow() + step) % n)
                    return True
                if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    self._run_current()
                    return True
                if k == Qt.Key.Key_Escape:
                    self.hide()
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def changeEvent(self, e: Any) -> None:  # noqa: N802
        try:
            if e.type() == e.Type.ActivationChange and not self.isActiveWindow() and self.isVisible():
                self.hide()  # 他の窓をクリックしたら閉じる
        except Exception:  # noqa: BLE001
            pass
        super().changeEvent(e)
