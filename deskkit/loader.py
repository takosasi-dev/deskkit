# settings で enabled のモジュールだけを import し create(ctx) → start() する(FR-2)。
# create/start/stop の例外はそのモジュールだけを「停止中(理由)」にして他は続行する(D-2 / FR-3)。
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, QTimer, Signal

from deskkit.context import ModuleContextImpl

if TYPE_CHECKING:
    from deskkit.host import Host

log = logging.getLogger("deskkit.host.loader")


@dataclass
class Slot:
    name: str
    state: str = "disabled"  # disabled / running / stopped
    reason: str | None = None
    module: Any = None
    ctx: ModuleContextImpl | None = None


def module_package(name: str) -> str:
    if name.startswith("_selftest_"):
        return f"deskkit.selftest_modules.{name}"
    return f"deskkit.modules.{name}"


def _short(e: BaseException) -> str:
    """停止の理由(画面・ログ・診断レポートに出る)。例外の文はパスを含みうるので、型名と DeskKit のソースの位置だけ。"""
    from deskkit.logging_setup import describe_exception

    return describe_exception(e)[:200]


class Loader(QObject):
    changed = Signal(str)

    def __init__(self, host: Host) -> None:
        super().__init__()
        self._host = host
        self.slots: dict[str, Slot] = {}

    def names(self) -> list[str]:
        return self._host.settings.module_names()

    def load_all(self) -> None:
        for name in self.names():
            self.start_one(name)

    def start_one(self, name: str) -> None:
        slot = self.slots.setdefault(name, Slot(name))
        if slot.state == "running":
            return
        settings = self._host.settings
        if name in settings.module_errors:
            self._set(slot, "stopped", f"設定エラー: {settings.module_errors[name]}")
            return
        if not settings.module_section(name).get("enabled", False):
            self._set(slot, "disabled", None)
            self._host.tray.remove_module(name)
            return
        ctx = ModuleContextImpl(self._host, name)
        slot.ctx = ctx
        self._host.tray.ensure_module(name)
        self._host.tray.set_module_status(name, "起動中…")
        try:
            pkg = importlib.import_module(module_package(name))
            module = pkg.create(ctx)
            module.start()
        except Exception as e:  # noqa: BLE001 - モジュールの失敗を隔離する(D-2)
            log.exception("モジュール %s の起動に失敗", name)
            ctx.teardown()
            slot.module = None
            self._set(slot, "stopped", _short(e))
            return
        slot.module = module
        self._set(slot, "running", None)
        if not ctx.status_text:
            self._host.tray.set_module_status(name, "動作中")
        log.info("モジュール %s を起動しました", name)

    def stop_one(self, name: str, *, state: str = "disabled", reason: str | None = None) -> None:
        slot = self.slots.get(name)
        if slot is None:
            return
        if slot.module is not None:
            try:
                slot.module.stop()
            except Exception:  # noqa: BLE001
                log.exception("モジュール %s の stop で例外", name)
        if slot.ctx is not None:
            slot.ctx.teardown()
        slot.module = None
        slot.ctx = None
        self._host.tray.reset_module(name)
        self._set(slot, state, reason)
        if state == "disabled":
            self._host.tray.remove_module(name)

    def restart(self, name: str) -> None:
        self.stop_one(name)
        self.start_one(name)
        self._host.after_hotkey_registration()

    def fault(self, name: str, reason: str) -> None:
        slot = self.slots.get(name)
        if slot is None or slot.state == "stopped":
            return
        if slot.ctx is not None:
            slot.ctx._alive = False  # 以後のハンドラ呼び出しを止める  # noqa: SLF001
        QTimer.singleShot(0, lambda: self._do_fault(name, reason))

    def _do_fault(self, name: str, reason: str) -> None:
        log.error("モジュール %s を停止中にしました: %s", name, reason)
        self.stop_one(name, state="stopped", reason=reason)
        self._host.notify("host", f"{self._host.title_of(name)} を停止しました", reason, None, level="error")

    def stop_all(self) -> None:
        for name in list(self.slots):
            if self.slots[name].module is not None:
                self.stop_one(name, state="disabled")

    def _set(self, slot: Slot, state: str, reason: str | None) -> None:
        slot.state = state
        slot.reason = reason
        if state == "stopped":
            self._host.tray.ensure_module(slot.name)
            self._host.tray.set_module_status(slot.name, f"停止中: {reason}")
        self.changed.emit(slot.name)
        self._host.refresh_badge()

    def module(self, name: str) -> Any:
        slot = self.slots.get(name)
        return slot.module if slot and slot.state == "running" else None
