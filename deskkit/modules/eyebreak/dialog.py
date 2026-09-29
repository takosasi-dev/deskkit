# 通知をクリックしたときに開く小さな窓「休憩の声かけ」(FR-5)。「10分あとで」「今日はもう出さない」「閉じる」「設定を開く」。
# 利用者のクリックでだけ開き、自分から前に出ない(INV-3)。枠なし・角丸・影・フェードは StyledDialog に任せる。
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from PySide6.QtWidgets import QWidget

from deskkit import catalog
from deskkit.modules.eyebreak.prompts import BODY, DISCLAIMER
from deskkit.ui import widgets as W
from deskkit.ui.theme import G

if TYPE_CHECKING:
    from deskkit.modules.eyebreak.module import EyeBreakModule

_log = logging.getLogger("deskkit.eyebreak")


def _safe(fn: Callable[[], Any]) -> Callable[[], None]:
    def run() -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            _log.warning("dialog handler failed: %s", type(e).__name__)

    return run


class BreakDialog(W.StyledDialog):
    def __init__(self, module: EyeBreakModule, kind: str, parent: QWidget | None = None) -> None:
        accent = catalog.info("eyebreak").accent
        super().__init__(parent, "休憩の声かけ", catalog.info("eyebreak").glyph or G.EYE, accent, width=420)
        self.m = module
        self.kind = kind
        lead = ("少し立って、ひと息つきませんか。" if kind == BODY else "20秒ほど、遠くを見てみましょう。")
        self.lead = W.label(lead, "Dim", wrap=True)
        self.body.addWidget(self.lead)
        self.done_label = W.label("", "Dim", wrap=True)
        self.done_label.hide()
        self.body.addWidget(self.done_label)
        self.body.addWidget(W.label(DISCLAIMER, "Mute", wrap=True))
        self.later_btn = W.button(f"{module.cfg.later_min}分あとで", "secondary", G.CLOCK, _safe(self._later))
        self.mute_btn = W.button("今日はもう出さない", "secondary", G.PAUSE, _safe(self._mute))
        self.settings_btn = W.button("設定を開く", "ghost", G.SETTINGS, _safe(self._settings))
        self.close_btn = W.button("閉じる", "primary", on_click=self.close)
        for b in (self.settings_btn, self.later_btn, self.mute_btn, self.close_btn):
            self.buttons.addWidget(b)

    def _later(self) -> None:
        self.m.later(self.kind, None)
        self.close()

    def _mute(self) -> None:
        until = self.m.mute_today()
        self.done_label.setText(f"{self.m.until_text(until)}まで出しません。トレイの『再開する』でいつでも戻せます。")
        self.done_label.show()
        self.lead.hide()
        self.later_btn.hide()
        self.mute_btn.hide()

    def _settings(self) -> None:
        self.m.open_page()
        self.close()
