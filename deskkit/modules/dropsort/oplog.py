# 操作ログ oplog.jsonl / 試運転ログ dryrun.jsonl(追記のみ・1行1件。§9.3)と、host と CLI を直列化する op.lock。
# URL は書かない(INV-7)。書くのはパス・サイズ・時刻・MOTW の有無・ZoneId・(FR-7 有効時のみ)ドメイン名。
from __future__ import annotations

import json
import msvcrt
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

OPS = ("move", "archive", "refused", "flagged", "undo", "failed")
DRY_OPS = ("would_move", "would_archive", "would_refuse")

_FIELDS = ("id", "ts", "op", "src", "dst", "size", "mtime", "mtime_epoch", "rule", "motw", "zone_id", "dst_fs",
           "reason", "undo_of", "win_error", "domain", "source")


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _clean(rec: dict[str, Any]) -> dict[str, Any]:
    out = {k: rec.get(k) for k in _FIELDS if k in rec or k in ("id", "ts", "op", "src", "dst", "reason", "undo_of")}
    for k, v in list(out.items()):
        if isinstance(v, str) and "://" in v:  # 念のため: URL らしき値は書かない(INV-7)
            out[k] = None
    return out


def tail_jsonl(path: Path, max_lines: int, max_bytes: int = 2 * 1024 * 1024) -> list[dict[str, Any]]:
    """末尾から最大 max_lines 件(古い順)。壊れた行は飛ばす。"""
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        start = max(0, size - max_bytes)
        f.seek(start)
        data = f.read()
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]
    lines = [ln for ln in lines if ln.strip()]
    out: list[dict[str, Any]] = []
    for ln in lines[-max_lines:] if max_lines > 0 else lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            v = json.loads(ln.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(v, dict):
            out.append(v)
    return out


class JsonlLog:
    def __init__(self, path: Path, id_prefix: str = "") -> None:
        self.path = path
        self._prefix = id_prefix
        self._last_id = ""
        self._mu = threading.Lock()

    def _next_id(self, now: float) -> str:
        base = self._prefix + datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")
        last = self._last_id
        if not last:
            tail = tail_jsonl(self.path, 1, 64 * 1024)
            last = str(tail[-1].get("id", "")) if tail else ""
        n = 1
        if last.startswith(base + "-"):
            try:
                n = int(last.rsplit("-", 1)[1]) + 1
            except ValueError:
                n = 1
        new = f"{base}-{n:04d}"
        self._last_id = new
        return new

    def append(self, rec: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        with self._mu:
            t = time.time() if now is None else now
            rec = dict(rec)
            rec.setdefault("id", self._next_id(t))
            rec.setdefault("ts", iso(t))
            out = _clean(rec)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(out, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return out

    def tail(self, n: int) -> list[dict[str, Any]]:
        return tail_jsonl(self.path, n)

    def all(self) -> list[dict[str, Any]]:
        return tail_jsonl(self.path, 0, max_bytes=1 << 62)


class LockBusyError(Exception):
    pass


class OpLock:
    """%LOCALAPPDATA%\\DeskKit\\dropsort\\op.lock のバイト範囲ロック(プロセスが落ちれば OS が外す)。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def try_acquire(self) -> bool:
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        os.close(self._fd)
        self._fd = None

    @contextmanager
    def hold(self, timeout: float = 0.0) -> Iterator[None]:
        deadline = time.monotonic() + timeout
        while not self.try_acquire():
            if time.monotonic() >= deadline:
                raise LockBusyError("op.lock を取得できません(他の DropSort が動作中)")
            time.sleep(0.1)
        try:
            yield
        finally:
            self.release()
