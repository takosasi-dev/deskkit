# ログの置き場と書式(§9.7)。日次ローテーション、保持日数で削除。faulthandler は別ファイル。
# 1行 = 時刻 レベル logger名 メッセージ。本文・URL を書かないのは呼び出し側の責務(C-12)。
from __future__ import annotations

import faulthandler
import logging
import logging.handlers
from pathlib import Path
from typing import IO

_fault_file: IO[str] | None = None
_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class MemoryTail(logging.Handler):
    """GUI のログ表示用に直近の行をメモリに保持する(ファイルと同じ内容)。"""

    def __init__(self, capacity: int = 800) -> None:
        super().__init__()
        self.capacity = capacity
        self.records: list[logging.LogRecord] = []
        self.listeners: list[object] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        if len(self.records) > self.capacity:
            del self.records[: len(self.records) - self.capacity]
        for cb in list(self.listeners):
            try:
                cb(record)  # type: ignore[operator]
            except Exception:  # noqa: BLE001 - ログ処理でアプリを落とさない
                pass


memory_tail = MemoryTail()


def setup(log_dir: Path, retention_days: int, level: int = logging.INFO) -> None:
    root = logging.getLogger("deskkit")
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = logging.handlers.TimedRotatingFileHandler(
        log_dir / "deskkit.log", when="midnight", backupCount=max(1, retention_days), encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(fh)
    memory_tail.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(memory_tail)
    ok = logging.getLogger("overlaykit")
    ok.setLevel(level)
    ok.addHandler(fh)
    ok.addHandler(memory_tail)
    root.propagate = False


def setup_faulthandler(log_dir: Path) -> None:
    global _fault_file
    try:
        _fault_file = open(log_dir / "faulthandler.log", "a", encoding="utf-8")  # noqa: SIM115 - プロセス終了まで開いておく
        faulthandler.enable(file=_fault_file, all_threads=True)
    except OSError:
        pass
