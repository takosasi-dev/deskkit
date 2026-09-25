# 更新の確認・ダウンロード・入れ替えを GUI から扱う窓口。通信は別スレッドで行い、結果はシグナルで返す。
# 自動確認は起動30秒後と、前回から24時間たったとき(設定 host.update.auto_check でオフにできる)。
from __future__ import annotations

import datetime as _dt
import logging
import threading
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, QTimer, Signal

from deskkit import __version__, paths, updater

if TYPE_CHECKING:
    from deskkit.host import Host

log = logging.getLogger("deskkit.host.update")


class UpdateManager(QObject):
    changed = Signal()
    progress = Signal(int, int)
    _checked = Signal(object, object, bool)   # (ReleaseInfo | None, error | None, manual)
    _downloaded = Signal(object, object)       # (Path | None, error | None)

    def __init__(self, host: Host) -> None:
        super().__init__()
        self._host = host
        self.state = "idle"  # idle / checking / latest / available / downloading / installing / error
        self.info: updater.ReleaseInfo | None = None
        self.error: str | None = None
        self._cancel = False
        self._checked.connect(self._on_checked)
        self._downloaded.connect(self._on_downloaded)
        self._timer = QTimer(self)
        self._timer.setInterval(60 * 60 * 1000)
        self._timer.timeout.connect(self._tick)

    # ---- 設定
    def cfg(self) -> dict[str, Any]:
        base = {"auto_check": True, "repo": updater.DEFAULT_REPO, "skip_version": None, "last_check": None}
        cur = self._host.settings.host().get("update")
        if isinstance(cur, dict):
            base.update(cur)
        return base

    def save_cfg(self, **values: Any) -> None:
        c = self.cfg()
        c.update(values)
        self._host.settings.write_host({"update": c})
        self.changed.emit()

    @property
    def can_install(self) -> bool:
        return updater.current_exe() is not None

    def start(self) -> None:
        QTimer.singleShot(30_000, self._tick)
        self._timer.start()

    def _tick(self) -> None:
        c = self.cfg()
        if not c.get("auto_check", True) or self.state in ("checking", "downloading", "installing"):
            return
        last = c.get("last_check")
        try:
            if last and _dt.datetime.now() - _dt.datetime.fromisoformat(str(last)) < _dt.timedelta(hours=24):
                return
        except ValueError:
            pass
        self.check(manual=False)

    # ---- 確認
    def check(self, manual: bool = True) -> None:
        if self.state in ("checking", "downloading", "installing"):
            return
        self.state = "checking"
        self.error = None
        self.changed.emit()
        repo = str(self.cfg().get("repo") or updater.DEFAULT_REPO)

        def work() -> None:
            try:
                info = updater.fetch_latest(repo)
                self._checked.emit(info, None, manual)
            except updater.UpdateError as e:
                self._checked.emit(None, str(e), manual)
            except Exception as e:  # noqa: BLE001 - スレッドで落とさない
                self._checked.emit(None, f"確認に失敗しました({type(e).__name__})", manual)

        threading.Thread(target=work, name="deskkit-update-check", daemon=True).start()

    def _on_checked(self, info: updater.ReleaseInfo | None, error: str | None, manual: bool) -> None:
        try:
            self.save_cfg(last_check=_dt.datetime.now().isoformat(timespec="seconds"))
        except Exception:  # noqa: BLE001
            log.exception("更新確認の時刻を保存できません")
        if error is not None:
            self.state = "error"
            self.error = error
            log.info("update check failed")
            self.changed.emit()
            return
        assert info is not None
        self.info = info
        if updater.is_newer(info.version):
            self.state = "available"
            log.info("update available version=%s", info.version)
            if manual or info.version != self.cfg().get("skip_version"):
                self._host.notify("host", f"新しい版 v{info.version} があります", "クリックして内容を確認・更新",
                                  self.show_dialog, level="info")
        else:
            self.state = "latest"
            log.info("update check: latest")
        self.changed.emit()

    def skip(self) -> None:
        if self.info is not None:
            self.save_cfg(skip_version=self.info.version)

    # ---- ダウンロードと入れ替え
    def install(self) -> None:
        if self.info is None or self.state in ("downloading", "installing") or not self.can_install:
            return
        self.state = "downloading"
        self._cancel = False
        self.changed.emit()
        info = self.info
        dest = paths.local_dir() / "updates"

        def work() -> None:
            try:
                p = updater.download(info, dest, lambda a, b: self.progress.emit(a, b), lambda: self._cancel)
                updater.verify_runs(p, info.version)
                self._downloaded.emit(p, None)
            except updater.UpdateError as e:
                self._downloaded.emit(None, str(e))
            except Exception as e:  # noqa: BLE001
                self._downloaded.emit(None, f"更新に失敗しました({type(e).__name__})")

        threading.Thread(target=work, name="deskkit-update-download", daemon=True).start()

    def cancel(self) -> None:
        self._cancel = True

    def _on_downloaded(self, path: Any, error: str | None) -> None:
        if error is not None:
            self.state = "error"
            self.error = error
            self.changed.emit()
            return
        self.state = "installing"
        self.changed.emit()
        try:
            exe = updater.swap_in(path)
            log.info("update swapped in, relaunching")
            updater.relaunch(exe, ["--updated-from", __version__])
        except Exception as e:  # noqa: BLE001 - どの失敗でも「入れ替え中」のまま固まらせない
            self._fail(e, "更新に失敗しました")
            return
        QTimer.singleShot(200, self._host.quit)

    def rollback(self) -> None:
        if self.state in ("downloading", "installing"):
            return
        self.state = "installing"
        self.changed.emit()
        try:
            exe = updater.swap_back()
            updater.relaunch(exe, ["--rolled-back-from", __version__])
        except Exception as e:  # noqa: BLE001
            self._fail(e, "前の版に戻せませんでした")
            return
        QTimer.singleShot(200, self._host.quit)

    def _fail(self, e: BaseException, fallback: str) -> None:
        log.error("update swap failed: %s", type(e).__name__)  # 例外の本文(パスを含みうる)はログに書かない
        self.state = "error"
        self.error = str(e) if isinstance(e, updater.UpdateError) else f"{fallback}({type(e).__name__})"
        self.changed.emit()

    def has_previous(self) -> bool:
        return updater.previous_exe() is not None

    def status_text(self) -> str:
        if self.state == "checking":
            return "確認しています…"
        if self.state == "available" and self.info:
            return f"新しい版 v{self.info.version} があります"
        if self.state == "latest":
            return f"最新版です(v{__version__})"
        if self.state == "downloading":
            return "ダウンロードしています…"
        if self.state == "installing":
            return "入れ替えて再起動しています…"
        if self.state == "error":
            return self.error or "エラー"
        last = self.cfg().get("last_check")
        return f"前回の確認: {str(last).replace('T', ' ')}" if last else "まだ確認していません"

    def show_dialog(self) -> None:
        from deskkit.ui.update_dialog import UpdateDialog

        UpdateDialog(self, self._host.main_window_if_visible()).exec()
