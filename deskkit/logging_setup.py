# ログの置き場と書式(§9.7)。日次ローテーション、保持日数で削除。faulthandler は別ファイル。
# 1行 = 時刻 レベル logger名 メッセージ。本文・URL を書かないのは呼び出し側の責務(C-12)。
# 例外は型名と DeskKit のソースの位置だけを書く(SafeFormatter。例外の文やパスは書かない。v0.3 VINV-4)。
from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import traceback
from pathlib import Path
from types import TracebackType
from typing import IO, Any

_fault_file: IO[str] | None = None
_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_OWN_PACKAGES = ("deskkit", "overlaykit")
_MAX_FRAMES = 8


def _own_path(filename: str) -> str | None:
    """DeskKit 自身のソースなら、パッケージ名から始まる相対パス(deskkit/modules/x/y.py)。それ以外は None。
    利用者のフォルダや exe の展開先(ユーザー名を含む)をログに出さないため、パッケージより上は捨てる。"""
    parts = filename.replace("\\", "/").split("/")
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] in _OWN_PACKAGES:
            return "/".join(parts[i:])
    return None


def describe_exception(e: BaseException | None, tb: TracebackType | None = None) -> str:
    """例外の型名と、発生した DeskKit のソースのファイル名・行番号だけの1行(v0.3 VINV-4)。
    例外の文(str(e))は書かない。OSError などのファイル名・パス・利用者の入力が入るため。数値のエラーコードだけは残す。"""
    if e is None:
        return "(例外なし)"
    head = type(e).__name__
    if isinstance(e, OSError):
        codes = [f"{k}={v}" for k, v in (("errno", e.errno), ("winerror", getattr(e, "winerror", None))) if isinstance(v, int)]
        if codes:
            head += "(" + " ".join(codes) + ")"
    frames = traceback.extract_tb(tb if tb is not None else e.__traceback__)
    own = [f"{p}:{f.lineno}" for f in reversed(frames) if (p := _own_path(f.filename)) is not None][:_MAX_FRAMES]
    text = head + (" at " + " < ".join(own) if own else "")
    cause = e.__cause__ or (None if e.__suppress_context__ else e.__context__)
    if cause is not None and cause is not e:
        text += f"(原因: {type(cause).__name__})"
    return text


class SafeFormatter(logging.Formatter):
    """例外のトレースバックを、型名と DeskKit のソースの位置だけにする書式(log.exception でも本文やパスが出ない)。"""

    def formatException(self, ei: Any) -> str:  # noqa: N802
        _tp, val, tb = ei
        return "  例外: " + describe_exception(val, tb)

    def formatStack(self, stack_info: str) -> str:  # noqa: N802
        return ""


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
    fh.setFormatter(SafeFormatter(_FORMAT))
    root.addHandler(fh)
    memory_tail.setFormatter(SafeFormatter(_FORMAT))
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
