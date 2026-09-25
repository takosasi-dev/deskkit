# 開発用: 一時フォルダを DESKKIT_HOME にして Control Center の各ページを画像に保存する(見た目の確認用)。
# 使い方: python tools/screenshot.py <出力フォルダ> [有効にするモジュール,...]
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
out.mkdir(parents=True, exist_ok=True)
enable = [x for x in (sys.argv[2].split(",") if len(sys.argv) > 2 else []) if x]
os.environ["DESKKIT_HOME"] = tempfile.mkdtemp(prefix="deskkit-shot-")

from deskkit import paths  # noqa: E402
from deskkit.catalog import MODULE_NAMES  # noqa: E402

p = paths.settings_path()
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps({"version": 1, "game_processes": [], "host": {},
                         "modules": {n: {"enabled": n in enable} for n in MODULE_NAMES}}), encoding="utf-8")

from deskkit.ui import theme as _t  # noqa: E402

_t.set_mode(os.environ.get("DESKKIT_THEME", "dark"))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from deskkit.host import Host, dpi_setup  # noqa: E402
from deskkit.ui import icons, theme  # noqa: E402

dpi = dpi_setup()
app = QApplication(sys.argv[:1])
theme.apply(app)
app.setWindowIcon(icons.app_icon())
host = Host(app, dpi, start_ipc=False, show_tray=False)
host.start()
host.show_window()
win = host._window
assert win is not None
keys = ["home", "usage", *MODULE_NAMES, "settings", "logs"]


def shoot(i: int = 0) -> None:
    if i >= len(keys):
        host.open_quick_actions()
        QTimer.singleShot(600, lambda: host._quick.grab().save(str(out / "qa.png")))
        from deskkit.ui.onboarding import Onboarding

        ob = Onboarding(host, win)
        ob.show()
        QTimer.singleShot(700, lambda: (ob.grab().save(str(out / "onboard1.png")), ob._go(1), ob._go(1)))
        QTimer.singleShot(1500, lambda: (ob.grab().save(str(out / "onboard3.png")), ob._go(1), ob._go(1)))
        QTimer.singleShot(2300, lambda: ob.grab().save(str(out / "onboard5.png")))
        host.notify("host", "テスト通知です", "トーストの見た目確認", None)
        QTimer.singleShot(2800, lambda: (app.primaryScreen().grabWindow(0).save(str(out / "zz_desktop.png")), app.quit()))
        return
    win.open_page(keys[i], animate=False)
    QTimer.singleShot(700, lambda: (win.grab().save(str(out / f"{i:02d}_{keys[i]}.png")), shoot(i + 1)))


QTimer.singleShot(800, shoot)
app.exec()
host.loader.stop_all()
print("saved to", out)
