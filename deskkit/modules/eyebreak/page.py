# Control Center の EyeBreak 画面(FR-16): Hero(状態・様子見/声かけの切り替え)→ 3つの数値 → 間隔 → ゲームのとき →
# 静かにするモード → 7日間の記録 → 専門家への相談の1行(E-12)。設定を変えたらすぐ保存し、今の数えは保つ。
# 色は theme(T.*)と catalog のアクセント色を実行時に読む。
from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any, TypeVar

from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from deskkit import catalog
from deskkit.modules.eyebreak.prompts import BASIS, DISCLAIMER
from deskkit.ui import theme as T
from deskkit.ui import widgets as W
from deskkit.ui.theme import G

if TYPE_CHECKING:
    from deskkit.modules.eyebreak.module import EyeBreakModule

_log = logging.getLogger("deskkit.eyebreak")
F = TypeVar("F", bound=Callable[..., Any])
WEEKDAYS = "月火水木金土日"


def _guard(fn: F) -> F:
    @functools.wraps(fn)
    def wrapper(*a: Any, **k: Any) -> Any:
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001
            _log.warning("page handler %s failed: %s", getattr(fn, "__name__", "?"), type(e).__name__)
            return None

    return wrapper  # type: ignore[return-value]


def _spin(lo: int, hi: int, value: int, suffix: str) -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setValue(value)
    s.setSuffix(suffix)
    s.setMinimumWidth(96)
    return s


class EyeBreakPage(W.ScrollPage):
    def __init__(self, module: EyeBreakModule) -> None:
        super().__init__()
        self.m = module
        acc = catalog.info("eyebreak").accent
        info = catalog.info("eyebreak")
        cfg = module.cfg
        # ---- Hero(FR-15)
        self.hero = W.Hero("EyeBreak", info.tagline, info.glyph, acc)
        self.pill = W.StatusPill("", "ok")
        self.hero.add_pill(self.pill)
        self.dry_label = W.label("", "Dim", wrap=True)
        self.hero.tag.setWordWrap(True)
        self.mode_btn = W.button("", "primary", G.PLAY, _guard(self._toggle_mode))
        self.hero.add_action(self.mode_btn)
        self.add(self.hero)
        self.add(self.dry_label)
        self.warn = W.label("この PC では使っている時間を読めません。声かけは出ません。", "Dim", wrap=True)
        self.warn.setStyleSheet(f"color: {T.WARN};")
        self.add(self.warn)
        # ---- 3つの数値
        self.t_prompts = W.StatTile("今日の声かけ", "0 回", G.EYE, acc)
        self.t_rested = W.StatTile("今日休めた回数", "0 回", G.CHECK, acc)
        self.t_use = W.StatTile("いま続けて使っている時間", "0分", G.CLOCK, acc)
        row = QHBoxLayout()
        row.setSpacing(12)
        for t in (self.t_prompts, self.t_rested, self.t_use):
            row.addWidget(t, 1)
        holder = QWidget()
        holder.setLayout(row)
        self.add(holder)
        # ---- 間隔
        c = W.Card("間隔", "続けて使った時間がこの長さになったら声をかけます。", G.CLOCK, acc)
        self.eye_on = W.ToggleSwitch(cfg.eye_enabled, acc)
        self.eye_min = _spin(5, 120, cfg.eye_min, " 分")
        c.add(W.SettingRow("目を休める声かけ", "画面から目を離して、遠くを見る声かけです。", self._pair(self.eye_on, self.eye_min)))
        self.body_on = W.ToggleSwitch(cfg.body_enabled, acc)
        self.body_min = _spin(20, 240, cfg.body_min, " 分")
        c.add(W.SettingRow("体を休める声かけ", "少し立って、ひと息つく声かけです。", self._pair(self.body_on, self.body_min)))
        self.break_min = _spin(1, 30, cfg.break_idle_min, " 分")
        c.add(W.SettingRow("休んだとみなす時間", "キーボードとマウスをこの長さ使わなかったら、数えを 0 に戻します。", self.break_min))
        self.later_min = _spin(1, 60, cfg.later_min, " 分")
        c.add(W.SettingRow("「あとで」の長さ", "声かけの窓の「あとで」を押してから、もう一度声をかけるまでの時間です。", self.later_min))
        self.day_hour = _spin(0, 23, cfg.day_start_hour, " 時")
        c.add(W.SettingRow("「今日」の区切り", "「今日はもう出さない」は、この時刻まで続きます。", self.day_hour))
        c.add(W.label(BASIS, "Mute", wrap=True))
        self.add(c)
        # ---- ゲームのとき
        g = W.Card("ゲームのとき", "ゲーム中と全画面の間は、声をかけません。", G.GAME, acc)
        self.game = W.Segmented([("after", "数え続けて、終わったら1回"), ("pause", "数えを止める")], cfg.game, acc)
        g.add(W.SettingRow("ゲーム中の数え方", None, self.game))
        self.delay = _spin(0, 600, cfg.after_game_delay_s, " 秒")
        g.add(W.SettingRow("ゲームを抜けてから待つ時間", "この間ゲームに戻らなければ、声をかけます。", self.delay))
        self.add(g)
        # ---- 静かにするモード
        q = W.Card("静かにするモード", "ModeShift でこのモードに切り替えている間は、声をかけずに待ちます。", G.MODE, acc)
        self.quiet_boxes: list[tuple[str, QCheckBox]] = []
        modes = self._modes()
        if not modes:
            empty = W.label("ModeShift のモードがありません。", "Mute")
            q.add(empty)
            q.setEnabled(False)
        for name, label in modes:
            cb = QCheckBox(label)
            cb.setChecked(name in cfg.quiet_modes)
            cb.toggled.connect(_guard(lambda _v: self._save_quiet()))
            q.add(cb)
            self.quiet_boxes.append((name, cb))
        self.add(q)
        # ---- 7日間の記録
        r = W.Card("7日間の記録", None, G.LOG, acc)
        self.table = QTableWidget(7, 4)
        self.table.setHorizontalHeaderLabels(["日付", "声かけ", "休めた", "今日はもう出さない"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(250)
        r.add(self.table)
        self.add(r)
        self.add(W.label(DISCLAIMER, "Mute", wrap=True))
        self.finish()
        # ---- 保存
        self.eye_on.toggled.connect(_guard(lambda v: self._save({"eye": {"enabled": bool(v)}})))
        self.body_on.toggled.connect(_guard(lambda v: self._save({"body": {"enabled": bool(v)}})))
        self.eye_min.editingFinished.connect(_guard(lambda: self._save({"eye": {"interval_min": self.eye_min.value()}})))
        self.body_min.editingFinished.connect(_guard(lambda: self._save({"body": {"interval_min": self.body_min.value()}})))
        self.break_min.editingFinished.connect(_guard(lambda: self._save({"break_idle_min": self.break_min.value()})))
        self.later_min.editingFinished.connect(_guard(lambda: self._save({"later_min": self.later_min.value()})))
        self.day_hour.editingFinished.connect(_guard(lambda: self._save({"day_start_hour": self.day_hour.value()})))
        self.delay.editingFinished.connect(_guard(lambda: self._save({"after_game_delay_s": self.delay.value()})))
        self.game.changed.connect(_guard(lambda v: self._save({"game": v})))
        self.m.notifier.changed.connect(self._refresh)
        self._refresh()

    @staticmethod
    def _pair(a: QWidget, b: QWidget) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addWidget(b)
        lay.addWidget(a)
        return w

    def _modes(self) -> list[tuple[str, str]]:
        try:
            return list(self.m.ctx.list_modes())
        except Exception:  # noqa: BLE001
            return []

    # ------------------------------------------------------------ 表示
    @_guard
    def _refresh(self) -> None:
        m = self.m
        live = m.cfg.mode == "live"
        status = m.status_text()
        kind = "warn" if m.engine.unavailable or m.muted() else ("info" if not live else "ok")
        self.pill.set_state(kind, status)
        counts = m.today_counts()
        if live:
            self.dry_label.hide()
            self.mode_btn.setText("様子見に戻す")
        else:
            self.dry_label.setText(f"様子見中: 声かけは出していません。出していたら今日は {counts['dry']} 回でした。")
            self.dry_label.show()
            self.mode_btn.setText("声かけを始める")
        self.warn.setVisible(m.engine.unavailable)
        self.t_prompts.set_value(f"{counts['eye'] + counts['body']} 回")
        self.t_rested.set_value(f"{counts['rested']} 回")
        self.t_use.set_value(m.use_text())
        today = m.today()
        for i in range(7):
            d = today - timedelta(days=i)
            vals = [f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})",
                    str(m.daily.get(d, "eye") + m.daily.get(d, "body")),
                    str(m.daily.get(d, "rested")), str(m.daily.get(d, "mute_today"))]
            for c, v in enumerate(vals):
                self.table.setItem(i, c, QTableWidgetItem(v))

    # ------------------------------------------------------------ 操作
    def _toggle_mode(self) -> None:
        self._save({"mode": "dry_run" if self.m.cfg.mode == "live" else "live"})

    def _save_quiet(self) -> None:
        chosen = [n for n, cb in self.quiet_boxes if cb.isChecked()]
        # 今は ModeShift に無いモード名(画面に出ていない物)は残す
        keep = [n for n in self.m.cfg.quiet_modes if n not in {x for x, _ in self.quiet_boxes}]
        self._save({"quiet_modes": keep + chosen})

    def _save(self, changes: dict[str, Any]) -> None:
        err = self.m.update_settings(changes)
        if err:
            W.message(self.m.ctx.window_parent() if hasattr(self.m.ctx, "window_parent") else None,
                      "保存できませんでした", err, kind="warn")
        self._refresh()
