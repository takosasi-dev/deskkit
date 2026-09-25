# はじめてガイド。初回起動時(と「はじめてガイドを見る」)に出す5枚のスライド。横にすべるアニメーションで切り替わる。
# 最後のページで使うモジュールとテーマを選べる。閉じると host.onboarded を true にする。
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QEasingCurve, QParallelAnimationGroup, QPoint, QPropertyAnimation, QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QStackedWidget, QVBoxLayout, QWidget

from deskkit import APP_NAME, catalog
from deskkit.ui import icons
from deskkit.ui import theme as T
from deskkit.ui.theme import G
from deskkit.ui.widgets import Glyph, Segmented, StyledDialog, ToggleSwitch, button, label

if TYPE_CHECKING:
    from deskkit.host import Host


class _Dots(QWidget):
    def __init__(self, n: int) -> None:
        super().__init__()
        self._n = n
        self._i = 0
        self.setFixedHeight(12)
        self.setFixedWidth(n * 18 + 16)

    def set_index(self, i: int) -> None:
        self._i = i
        self.update()

    def paintEvent(self, _e: Any) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        x = 0.0
        for k in range(self._n):
            w = 24.0 if k == self._i else 8.0
            c = QColor(T.ACCENT if k == self._i else T.BORDER_HI)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(QRectF(x, 2, w, 8), 4, 4)
            x += w + 8


def _page(glyph_widget: QWidget, title: str, text: str) -> tuple[QWidget, QVBoxLayout]:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(8, 4, 8, 4)
    lay.setSpacing(12)
    lay.addWidget(glyph_widget, 0, Qt.AlignmentFlag.AlignHCenter)
    t = label(title, "H1")
    t.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lay.addWidget(t)
    d = label(text, "Dim", wrap=True)
    d.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lay.addWidget(d)
    return w, lay


def _big_glyph(g: str, accent: str) -> Glyph:
    gl = Glyph(g, 30, accent)
    gl.setFixedSize(72, 72)
    gl.setStyleSheet(f"color: {accent}; background: {T.alpha(accent, 0.14)}; border: 1px solid {T.alpha(accent, 0.3)}; border-radius: 20px;")
    return gl


def _point(glyph: str, accent: str, title: str, text: str) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 2, 0, 2)
    g = Glyph(glyph, 16, accent)
    g.setFixedSize(36, 36)
    g.setStyleSheet(f"color: {accent}; background: {T.alpha(accent, 0.13)}; border-radius: 10px;")
    lay.addWidget(g, 0, Qt.AlignmentFlag.AlignTop)
    box = QVBoxLayout()
    box.setSpacing(1)
    box.addWidget(label(title, "H3"))
    box.addWidget(label(text, "Mute", wrap=True))
    lay.addLayout(box, 1)
    return w


class Onboarding(StyledDialog):
    def __init__(self, host: Host, parent: QWidget | None) -> None:
        super().__init__(parent, f"{APP_NAME} へようこそ", G.SPARKLE, T.ACCENT, width=620)
        self._host = host
        self.stack = QStackedWidget()
        self.stack.setMinimumHeight(380)
        self.body.addWidget(self.stack)
        self.toggles: dict[str, ToggleSwitch] = {}

        logo = QLabel()
        logo.setPixmap(icons.render(192).scaled(96, 96, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        p1, _ = _page(logo, "PC 生活を、少しずつ快適に", "DeskKit は4つの小さな道具を、1つの常駐アプリ・1つのトレイアイコンにまとめたものです。"
                      "使いたいものだけをオンにして使います。")
        self.stack.addWidget(p1)

        grid_w = QWidget()
        grid = QGridLayout(grid_w)
        grid.setSpacing(10)
        for i, m in enumerate(catalog.MODULES):
            grid.addWidget(_point(m.glyph, m.accent, m.title, m.tagline), i // 2, i % 2)
        p2, l2 = _page(_big_glyph(G.APP, T.ACCENT), "4つの道具", "どれも独立していて、1つが止まっても他は動き続けます。")
        l2.addWidget(grid_w)
        self.stack.addWidget(p2)

        p3, l3 = _page(_big_glyph(G.SHIELD, T.SUCCESS), "安心して試せる仕組み", "")
        for g, t, d in ((G.EYE, "最初は試運転", "ファイルやウィンドウを動かす前に「何をする予定か」だけを表示します。確認してから本番に切り替えます。"),
                        (G.UNDO, "元に戻せる", "整理したファイル・戻したウィンドウ配置・変えた音量や電源プランは元に戻せます。"),
                        (G.GAME, "ゲーム中は控える", "ゲームや全画面の動画が前面にある間は、画面に割り込む操作をしません。"),
                        (G.LOCK, "中身を外に出さない", "クリップボードの履歴は暗号化して保存し、ログにも本文を書きません。通信は更新の確認だけです。")):
            l3.addWidget(_point(g, T.SUCCESS, t, d))
        self.stack.addWidget(p3)

        hk = str(host.settings.host().get("quick_action_hotkey") or "未設定")
        p4, l4 = _page(_big_glyph(G.KEYBOARD, T.ACCENT), "操作のコツ", "")
        for g, t, d in ((G.LIGHTNING, f"クイックアクション  {hk}", "どこからでも検索窓を開き、「ゲーム」「配置」「元に戻す」などと打つだけで実行できます。"),
                        (G.HOME, "トレイアイコン", "画面右下のアイコンをクリックすると、この画面(Control Center)が開きます。右クリックでメニュー。"),
                        (G.PASTE, "ClipShelf のパレット  Ctrl+Alt+V", "ClipShelf をオンにすると、コピー履歴と定型文をすぐに呼び出せます。")):
            l4.addWidget(_point(g, T.ACCENT, t, d))
        self.stack.addWidget(p4)

        p5, l5 = _page(_big_glyph(G.CHECK, T.ACCENT), "はじめましょう", "使いたい道具をオンにしてください。あとからいつでも変えられます。")
        for m in catalog.MODULES:
            row = QHBoxLayout()
            row.addWidget(Glyph(m.glyph, 15, m.accent))
            row.addWidget(label(m.title, "H3"))
            row.addWidget(label(m.tagline, "Mute"), 1)
            tg = ToggleSwitch(bool(host.settings.module_section(m.name).get("enabled", False)), m.accent)
            self.toggles[m.name] = tg
            row.addWidget(tg)
            l5.addLayout(row)
        trow = QHBoxLayout()
        trow.addWidget(label("テーマ", "H3"))
        trow.addStretch(1)
        self.theme = Segmented([("dark", "ダーク"), ("light", "ライト"), ("system", "Windows に合わせる")],
                               str(host.settings.host().get("theme", "dark")))
        trow.addWidget(self.theme)
        l5.addSpacing(6)
        l5.addLayout(trow)
        self.stack.addWidget(p5)
        for i in range(self.stack.count()):
            pg = self.stack.widget(i)
            lay = pg.layout() if pg is not None else None
            if isinstance(lay, QVBoxLayout):
                lay.addStretch(1)

        self.dots = _Dots(self.stack.count())
        self.buttons.insertWidget(0, self.dots)
        self.skip = button("スキップ", "ghost", on_click=self._finish)
        self.back = button("戻る", "secondary", on_click=lambda: self._go(-1))
        self.next = button("次へ", "primary", G.CHEVRON, lambda: self._go(1))
        for b in (self.skip, self.back, self.next):
            self.buttons.addWidget(b)
        self._sync()

    def _go(self, d: int) -> None:
        i = self.stack.currentIndex() + d
        if i >= self.stack.count():
            self._finish()
            return
        if i < 0:
            return
        new = self.stack.widget(i)
        if new is None:
            return
        w = self.stack.width()
        self.stack.setCurrentIndex(i)
        grp = QParallelAnimationGroup(self)
        a = QPropertyAnimation(new, b"pos", self)
        a.setDuration(280)
        a.setStartValue(QPoint(w // 3 * d, 0))
        a.setEndValue(QPoint(0, 0))
        a.setEasingCurve(QEasingCurve.Type.OutCubic)
        grp.addAnimation(a)
        grp.start(QParallelAnimationGroup.DeletionPolicy.DeleteWhenStopped)
        self._sync()

    def _sync(self) -> None:
        i = self.stack.currentIndex()
        last = i == self.stack.count() - 1
        self.dots.set_index(i)
        self.back.setVisible(i > 0)
        self.skip.setVisible(not last)
        self.next.setText("はじめる" if last else "次へ")

    def _finish(self) -> None:
        try:
            if self.stack.currentIndex() == self.stack.count() - 1:
                for name, tg in self.toggles.items():
                    if tg.isChecked() != bool(self._host.settings.module_section(name).get("enabled", False)):
                        self._host.set_module_enabled(name, tg.isChecked())
                theme = self.theme.value()
                changed = theme != str(self._host.settings.host().get("theme", "dark"))
                self._host.settings.write_host({"onboarded": True, "theme": theme})
                if changed:
                    self._host.notify("host", "テーマは再起動後に切り替わります", "クリックして今すぐ再起動",
                                      self._host.restart, level="info")
            else:
                self._host.settings.write_host({"onboarded": True})
        except Exception:  # noqa: BLE001
            import logging

            logging.getLogger("deskkit.host.ui").exception("ガイドの保存で例外")
        self.accept()
