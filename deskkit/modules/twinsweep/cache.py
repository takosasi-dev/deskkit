# 特徴のキャッシュ(G-7・§9)。SQLite の features 表に、パス・大きさ・更新時刻をキーとして特徴を残す。
# 接続はそれを開いたスレッドだけで使う(スレッドをまたがない。v0.2 の失敗の再発防止)。壊れた DB は cache.db.broken に
# ずらして作り直す(§10)。写真のパスを含むがログではない(V-7 の対象外)。ログにはパスを書かない。
from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from deskkit.modules.twinsweep.hashing import Features, to_signed64, to_unsigned64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS features (
  path TEXT PRIMARY KEY,
  size INTEGER,
  mtime_ns INTEGER,
  sha256 TEXT,
  dhash INTEGER,
  width INTEGER,
  height INTEGER,
  taken_at TEXT,
  sharpness REAL,
  last_seen TEXT
);
"""
KEEP_DAYS = 30
_file_lock = threading.Lock()  # DB ファイルそのものの操作(作り直し・削除)を1つずつにする


@dataclass(frozen=True)
class CachedRow:
    size: int
    mtime_ns: int
    features: Features


class FeatureCache:
    def __init__(self, path: Path, today: date | None = None) -> None:
        self.path = path
        self.rebuilt = False
        self._today = (today or date.today()).isoformat()
        self._pending = 0
        self._conn = self._open()

    # ------------------------------------------------------------ 開閉
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=5.0)
        try:
            conn.execute("PRAGMA temp_store = MEMORY")  # 一時ファイルを作らない
            conn.executescript(_SCHEMA)
            row = conn.execute("PRAGMA quick_check").fetchone()
            if row is None or str(row[0]).lower() != "ok":
                raise sqlite3.DatabaseError("quick_check")
        except sqlite3.DatabaseError:
            conn.close()  # 閉じないと、壊れた DB をずらせない
            raise
        return conn

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock:
            try:
                return self._connect()
            except sqlite3.DatabaseError:
                pass
            # 壊れている: cache.db.broken にずらして作り直す(自分のデータ。上書きしてよい)
            try:
                os.replace(self.path, self.path.with_name(self.path.name + ".broken"))
            except OSError:
                pass
            for suffix in ("-journal", "-wal", "-shm"):
                _remove_own(self.path.with_name(self.path.name + suffix))
            self.rebuilt = True
            return self._connect()

    def close(self) -> None:
        try:
            self.commit()
        finally:
            self._conn.close()

    def commit(self) -> None:
        if self._conn.in_transaction:
            self._conn.commit()
        self._pending = 0

    # ------------------------------------------------------------ 読み書き
    def get(self, path: str, size: int, mtime_ns: int) -> Features | None:
        row = self._conn.execute(
            "SELECT size, mtime_ns, sha256, dhash, width, height, taken_at, sharpness, last_seen FROM features WHERE path = ?",
            (path,)).fetchone()
        if row is None or int(row[0]) != size or int(row[1]) != mtime_ns or row[2] is None:
            return None
        if row[8] != self._today:
            self._conn.execute("UPDATE features SET last_seen = ? WHERE path = ?", (self._today, path))
            self._bump()
        dh = None if row[3] is None else to_unsigned64(int(row[3]))
        return Features(str(row[2]), dh, int(row[4] or 0), int(row[5] or 0), row[6], float(row[7] or 0.0))

    def put(self, path: str, size: int, mtime_ns: int, f: Features) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO features (path, size, mtime_ns, sha256, dhash, width, height, taken_at, sharpness, last_seen)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (path, size, mtime_ns, f.sha256, None if f.dhash is None else to_signed64(f.dhash), f.width, f.height,
             f.taken_at, f.sharpness, self._today))
        self._bump()

    def _bump(self) -> None:
        self._pending += 1
        if self._pending >= 200:
            self.commit()

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM features").fetchone()[0])

    def purge_unused(self, days: int = KEEP_DAYS) -> int:
        cutoff = (date.fromisoformat(self._today) - timedelta(days=days)).isoformat()
        cur = self._conn.execute("DELETE FROM features WHERE last_seen IS NULL OR last_seen < ?", (cutoff,))
        self.commit()
        return max(0, cur.rowcount)

    def rows(self) -> Iterable[tuple[str, int, int]]:
        """テスト用: (path, size, mtime_ns)。"""
        return [(str(p), int(s), int(m)) for p, s, m in self._conn.execute("SELECT path, size, mtime_ns FROM features")]


def _remove_own(p: Path) -> bool:
    # 自分のキャッシュ(cache.db と付属ファイル)を消すためだけに使う。利用者のファイルには使わない(VAC-4 の例外)
    try:
        p.unlink()  # VAC-4 例外: TwinSweep 自身のキャッシュ DB の削除(FR-18・§10)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def clear_cache(path: Path) -> bool:
    """FR-18: キャッシュの DB を消す(自分のデータなので VINV-2 の対象外)。消せなかったら False。"""
    with _file_lock:
        ok = True
        for p in (path, path.with_name(path.name + "-journal"), path.with_name(path.name + "-wal"),
                  path.with_name(path.name + "-shm")):
            if p.exists() and not _remove_own(p):
                ok = False
        return ok


def row_count(path: Path) -> int:
    """診断用。呼んだスレッドで読み取り専用の接続をその場で開いて閉じる(スレッドをまたがない)。"""
    if not path.exists():
        return 0
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1.0)
        try:
            conn.execute("PRAGMA temp_store = MEMORY")
            return int(conn.execute("SELECT COUNT(*) FROM features").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return -1
