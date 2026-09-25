# 開発用: 一時フォルダを DESKKIT_HOME にして Control Center の各ページを画像に保存する(見た目の確認用)。
# 使い方: python tools/screenshot.py <出力フォルダ> [有効にするモジュール,...] [--desktop]
#   --desktop を付けたときだけ、最後にデスクトップ全体(トーストの確認用。利用者の画面が写る)を zz_desktop.png に保存する。
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
flags = {a for a in sys.argv[1:] if a.startswith("--")}
args = [a for a in sys.argv[1:] if not a.startswith("--")]
out = Path(args[0] if args else ".")
out.mkdir(parents=True, exist_ok=True)
enable = [x for x in (args[1].split(",") if len(args) > 1 else []) if x]
grab_desktop = "--desktop" in flags
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


def finish() -> None:
    if grab_desktop:
        screen = app.primaryScreen()
        if screen is not None:
            screen.grabWindow(0).save(str(out / "zz_desktop.png"))
    # app.quit() だと Qt 6 は先に全部の窓を閉じようとし、Control Center は「トレイに残る」ために閉じるのを断るので、
    # 終了そのものが取り消されて app.exec() から戻らなかった。本物の終了と同じ host.quit() で終わる
    host.quit()


def shoot(i: int = 0) -> None:
    if i >= len(keys):
        host.open_quick_actions()
        QTimer.singleShot(600, lambda: host._quick.grab().save(str(out / "qa.png")))
        from deskkit.ui.onboarding import Onboarding

        ob = Onboarding(host, win)
        ob.show()
        QTimer.singleShot(700, lambda: (ob.grab().save(str(out / "onboard1.png")), ob._go(1), ob._go(1)))
        QTimer.singleShot(1500, lambda: (ob.grab().save(str(out / "onboard3.png")), ob._go(1), ob._go(1)))
        QTimer.singleShot(2300, lambda: (ob.grab().save(str(out / "onboard5.png")), ob.close()))
        host.notify("host", "テスト通知です", "トーストの見た目確認", None)
        QTimer.singleShot(2800, finish)
        return
    win.open_page(keys[i], animate=False)
    QTimer.singleShot(700, lambda: (win.grab().save(str(out / f"{i:02d}_{keys[i]}.png")), shoot(i + 1)))


QTimer.singleShot(800, shoot)
app.exec()
host.loader.stop_all()
print("saved to", out, flush=True)
# モジュールの作業スレッドが残っていても、撮影用のツールは待たずに終わる(本番の終了処理は host.quit で済んでいる)
os._exit(0)
