# 起動の入口(bootstrap)。DPI awareness 確定 → faulthandler → 単一インスタンス確認/CLI 転送 → QApplication。
# 既に起動中なら引数を転送して、その終了コードで終わる(FR-1)。--selftest は host を起動せずに検査する。
from __future__ import annotations

import io
import logging
import sys
from typing import Any

from deskkit import APP_NAME, __version__, paths


class _NullStream(io.TextIOBase):
    def write(self, s: str) -> int:
        return len(s)


def _fix_std_streams() -> None:
    """windowed exe では stdout/stderr が None になるので、書き込みで落ちないようにする。"""
    if sys.stdout is None:
        sys.stdout = _NullStream()
    if sys.stderr is None:
        sys.stderr = _NullStream()


def _attach_console() -> None:
    """CLI として呼ばれたとき、親のコンソールに出力を出す(windowed exe 用)。"""
    if not paths.is_frozen():
        return
    import ctypes

    if ctypes.windll.kernel32.AttachConsole(ctypes.c_uint32(-1).value):
        try:
            cp = int(ctypes.windll.kernel32.GetConsoleOutputCP()) or 65001
            sys.stdout = open("CONOUT$", "w", encoding=f"cp{cp}", errors="replace")  # noqa: SIM115
            sys.stderr = sys.stdout
            print()
        except OSError:
            pass


def _excepthook(tp: type[BaseException], val: BaseException, tb: Any) -> None:
    logging.getLogger("deskkit.host").error("捕まえられなかった例外", exc_info=(tp, val, tb))


HELP = f"""{APP_NAME} {__version__}
  DeskKit                       常駐を開始(起動中なら画面を開く)
  DeskKit --selftest [対象]     自己検査(host / all / modeshift / dropsort / layoutkeep / clipshelf / sendprep / pccheckup / twinsweep)
  DeskKit <モジュール> <引数>   起動中の DeskKit へ転送(例: DeskKit mode --list, DeskKit dropsort status)
  DeskKit <モジュール> open <ファイル...>
                                ファイルを渡す(例: DeskKit sendprep open a.jpg)。起動していなければ起動してから渡す
  DeskKit --quit                常駐を終了
"""


def main(argv: list[str]) -> int:
    _fix_std_streams()
    if len(argv) >= 2 and argv[0] == "--version-file":
        # 更新前の起動確認用(updater.verify_runs)。版数を書いてすぐ終わる
        from pathlib import Path

        Path(argv[1]).write_text(__version__, encoding="utf-8")
        return 0
    if argv and argv[0] == "--version":
        _attach_console()
        print(__version__)
        return 0
    notice: str | None = None
    post_update = False
    if len(argv) >= 2 and argv[0] == "--post-update":
        # 更新・巻き戻しの直後: 旧プロセスの終了を待ってから通常どおり起動する
        from deskkit import updater

        try:
            updater.wait_for_pid_exit(int(argv[1]))
        except ValueError:
            pass
        post_update = True
        rest = argv[2:]
        if len(rest) >= 2 and rest[0] == "--updated-from":
            notice = f"v{rest[1]} から v{__version__} に更新しました"
        elif len(rest) >= 2 and rest[0] == "--rolled-back-from":
            notice = f"v{rest[1]} から v{__version__} に戻しました"
        argv = []
    if argv and argv[0] in ("-h", "--help", "/?"):
        _attach_console()
        print(HELP)
        return 0
    if argv and argv[0] == "--selftest":
        _attach_console()
        from deskkit import selftest

        return selftest.main(argv[1:])

    from deskkit.host import dpi_setup

    dpi = dpi_setup()
    from deskkit import logging_setup

    logging_setup.setup_faulthandler(paths.log_dir())
    from deskkit import ipc

    is_command = bool(argv) and argv[0] not in ("--autostart", "--show", LAUNCHED_FLAG)
    if is_command:
        from PySide6.QtCore import QCoreApplication

        _app = QCoreApplication(sys.argv[:1])
        _attach_console()
        if ipc.is_open_command(argv):
            # H-B: 「送る」などから渡されたファイルは、DeskKit が起動していなければ起動してから渡す(最大 15 秒待つ)
            code, out = ipc.forward_or_launch(argv, launch_host_detached)
        else:
            code, out = ipc.forward(argv)
        if out:
            print(out)
        return code
    if not ipc.acquire_instance_mutex():
        if post_update:
            # 更新・再起動の直後: 旧プロセスの終了処理(大きなファイルの移動の完了待ちなど)が長引いても、
            # 終わるまで待ってから常駐を引き継ぐ。終わりかけの旧プロセスへ --show を送ると両方いなくなるので送らない
            if not ipc.wait_instance_mutex(POST_UPDATE_MUTEX_WAIT_S):
                logging.getLogger("deskkit.host").error("旧プロセスが終了しないため起動を中止しました")
                return 1
        else:
            return _forward_to_running(argv)
    return _run_resident(argv, dpi, notice)


POST_UPDATE_MUTEX_WAIT_S = 180.0
# H-B: `<module> open ...` が host を起動するときの引数。画面・はじめてガイド・「起動しました」の通知を出さずに常駐を始める
# (画面はモジュールの handle_cli が ctx.show_page() で開く)
LAUNCHED_FLAG = "--launched-for-open"


def host_launch_spec() -> tuple[list[str], str | None]:
    """常駐の DeskKit を別プロセスで起動するコマンドと cwd。exe なら自分の exe、ソース実行なら pythonw -m deskkit(cwd はリポジトリ直下)。"""
    cmd = paths.launch_command() + [LAUNCHED_FLAG]
    return cmd, None if paths.is_frozen() else str(paths.source_root())


def launch_host_detached() -> None:
    import subprocess

    cmd, cwd = host_launch_spec()
    subprocess.Popen(cmd, cwd=cwd, env=paths.child_env(), close_fds=True,
                     creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def _forward_to_running(argv: list[str]) -> int:
    """既に常駐している DeskKit に画面を開かせる(二重起動しない)。"""
    from PySide6.QtCore import QCoreApplication

    from deskkit import ipc

    _app = QCoreApplication(sys.argv[:1])
    quiet = bool(argv) and argv[0] in ("--autostart", LAUNCHED_FLAG)  # 画面を開かない起動(何もしない要求を送る)
    code, _out = ipc.forward(["--autostart"] if quiet else ["--show"], 5000)
    return 0 if code in (0, ipc.EXIT_NOT_RUNNING) else code


def after_update_housekeeping() -> None:
    """更新・巻き戻し後の起動に成功したら、ダウンロード済みの更新ファイルを消す(巻き戻しは DeskKit.previous.exe を使うので不要)。"""
    from deskkit import updater

    try:
        updater.cleanup_downloads(paths.local_dir() / "updates")
    except OSError:
        logging.getLogger("deskkit.host").warning("更新ファイルを片付けられませんでした")


def _run_resident(argv: list[str], dpi: str, notice: str | None) -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    from deskkit.settings import SettingsStore
    from deskkit.ui import theme as _theme

    _pre = SettingsStore(paths.settings_path())
    _pre.load()
    _theme.set_mode(str(_pre.host().get("theme", "dark")))  # 他の UI 部品を import する前に色を確定させる

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    from deskkit.ui import icons, theme, wheel_guard

    theme.apply(app)
    wheel_guard.install(app)  # フォーカスの無い数値欄・選択欄はホイールでページをスクロールする(UX-1)
    app.setWindowIcon(icons.app_icon())
    sys.excepthook = _excepthook
    from deskkit.host import Host

    host = Host(app, dpi)
    created = host.start()
    autostarted = bool(argv) and argv[0] == "--autostart"
    for_open = bool(argv) and argv[0] == LAUNCHED_FLAG  # H-B: ファイルを渡すために起動された(画面はモジュールが開く)
    if notice:
        host.notify("host", notice, "", host.show_window, level="ok")
        after_update_housekeeping()
    if created or (not autostarted and not for_open and host.settings.host().get("show_window_on_start", True)):
        host.show_window()
    if not host.settings.host().get("onboarded", False) and not autostarted and not for_open:
        from PySide6.QtCore import QTimer

        QTimer.singleShot(500, host.show_onboarding)
    elif autostarted:
        host.notify("host", "DeskKit を起動しました", "トレイのアイコンから操作できます", host.show_window)
    return int(app.exec())
