# 履歴と定型文の SQLite 保存(スキーマは §9.3 のとおり)。payload は行ごとの DPAPI 暗号文だけを書く(INV-2)。
# 起動時に全行を復号してメモリ上のキャッシュに持つ(ディスクに平文キャッシュを作らない)。secure_delete を有効にし、
# 全消去後は VACUUM する(D-7)。壊れた DB は作り直さず StoreError にする(§10 / C-8)。
from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from deskkit.modules.clipshelf.crypto import Cipher, CryptoError, Payload, decode_payload, encode_payload

SCHEMA_VERSION = "1"
KIND_HISTORY = "history"
KIND_SNIPPET = "snippet"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS items (
  id            INTEGER PRIMARY KEY,
  kind          TEXT NOT NULL CHECK (kind IN ('history', 'snippet')),
  created_at    TEXT NOT NULL,
  last_used_at  TEXT NOT NULL,
  pinned        INTEGER NOT NULL DEFAULT 0,
  payload       BLOB NOT NULL
);
"""


class StoreError(Exception):
    """DB を開けない・書けない。メッセージは種類だけ(本文を含めない)。"""


@dataclass
class Item:
    id: int
    kind: str
    created_at: datetime
    last_used_at: datetime
    pinned: bool
    text: str
    source_exe: str | None
    name: str | None
    norm: str = field(default="", repr=False)  # search.py が使う正規化済みの文字列(メモリのみ)

    def __repr__(self) -> str:  # 本文を repr に出さない(例外・ログに混ざるのを防ぐ)
        return f"Item(id={self.id}, kind={self.kind}, pinned={self.pinned})"


def to_iso(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="milliseconds")


def from_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo is not None else dt.astimezone()


def _now() -> datetime:
    return datetime.now().astimezone()


class Store:
    def __init__(self, path: Path, cipher: Cipher, now: Callable[[], datetime] = _now) -> None:
        self.path = path
        self._cipher = cipher
        self._now = now
        self._conn: sqlite3.Connection | None = None
        self._items: dict[int, Item] = {}
        self.undecryptable = 0
        self.load_ms = 0.0

    # ------------------------------------------------------------ 開閉
    def open(self) -> None:
        try:
            conn = sqlite3.connect(str(self.path), isolation_level=None)
            conn.execute("PRAGMA secure_delete = ON")
            conn.execute("PRAGMA temp_store = MEMORY")  # 一時ファイルを作らない
            conn.executescript(_SCHEMA)
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if row is None:
                conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
            elif row[0] != SCHEMA_VERSION:
                conn.close()
                raise StoreError(f"未対応のスキーマ版です({row[0]})")
        except sqlite3.DatabaseError as e:
            raise StoreError(f"DB を開けません({type(e).__name__})") from None
        self._conn = conn
        self._load()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
        self._items.clear()

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreError("DB が開かれていません")
        return self._conn

    def _load(self) -> None:
        t0 = time.perf_counter()
        self._items.clear()
        self.undecryptable = 0
        rows = self._db().execute("SELECT id, kind, created_at, last_used_at, pinned, payload FROM items").fetchall()
        for rid, kind, created, used, pinned, blob in rows:
            try:
                p = decode_payload(self._cipher, bytes(blob))
                item = Item(int(rid), str(kind), from_iso(created), from_iso(used), bool(pinned), p.text, p.source_exe, p.name)
            except (CryptoError, ValueError):
                self.undecryptable += 1  # 表示しない・自動削除しない(§10)
                continue
            self._items[item.id] = item
        self.load_ms = (time.perf_counter() - t0) * 1000.0

    # ------------------------------------------------------------ 参照
    def items(self) -> list[Item]:
        return list(self._items.values())

    def get(self, item_id: int) -> Item | None:
        return self._items.get(item_id)

    def history(self) -> list[Item]:
        return [i for i in self._items.values() if i.kind == KIND_HISTORY]

    def snippets(self) -> list[Item]:
        return [i for i in self._items.values() if i.kind == KIND_SNIPPET]

    def latest_history(self) -> Item | None:
        hist = self.history()
        return max(hist, key=lambda i: (i.last_used_at, i.id)) if hist else None

    def counts(self) -> dict[str, int]:
        hist = self.history()
        return {
            "history": len(hist),
            "pinned": sum(1 for i in hist if i.pinned),
            "snippets": sum(1 for i in self._items.values() if i.kind == KIND_SNIPPET),
            "undecryptable": self.undecryptable,
        }

    def history_created_dates(self) -> list[date]:
        """履歴の作成日(平文の created_at 列だけを読む。payload は復号しない)。"""
        out: list[date] = []
        for (created,) in self._db().execute("SELECT created_at FROM items WHERE kind = 'history'"):
            try:
                out.append(from_iso(str(created)).date())
            except ValueError:
                continue
        return out

    def row_count(self) -> int:
        return int(self._db().execute("SELECT COUNT(*) FROM items").fetchone()[0])

    # ------------------------------------------------------------ 書き込み
    def _insert(self, kind: str, text: str, source_exe: str | None, name: str | None) -> Item:
        blob = encode_payload(self._cipher, Payload(text, source_exe, name))  # 失敗時は CryptoError(平文で保存しない)
        now = self._now()
        iso = to_iso(now)
        try:
            cur = self._db().execute(
                "INSERT INTO items (kind, created_at, last_used_at, pinned, payload) VALUES (?, ?, ?, 0, ?)",
                (kind, iso, iso, blob),
            )
        except sqlite3.DatabaseError as e:
            raise StoreError(f"DB に書けません({type(e).__name__})") from None
        item = Item(int(cur.lastrowid or 0), kind, now, now, False, text, source_exe, name)
        self._items[item.id] = item
        return item

    def add_history(self, text: str, source_exe: str | None) -> Item:
        return self._insert(KIND_HISTORY, text, source_exe, None)

    def add_snippet(self, name: str, text: str) -> Item:
        return self._insert(KIND_SNIPPET, text, None, name)

    def update_snippet(self, item_id: int, name: str, text: str) -> bool:
        item = self._items.get(item_id)
        if item is None or item.kind != KIND_SNIPPET:
            return False
        blob = encode_payload(self._cipher, Payload(text, None, name))
        self._exec("UPDATE items SET payload = ? WHERE id = ?", (blob, item_id))
        item.name, item.text, item.norm = name, text, ""
        return True

    def touch(self, item_id: int) -> bool:
        item = self._items.get(item_id)
        if item is None:
            return False
        now = self._now()
        self._exec("UPDATE items SET last_used_at = ? WHERE id = ?", (to_iso(now), item_id))
        item.last_used_at = now
        return True

    def set_pinned(self, item_id: int, pinned: bool) -> bool:
        item = self._items.get(item_id)
        if item is None:
            return False
        self._exec("UPDATE items SET pinned = ? WHERE id = ?", (1 if pinned else 0, item_id))
        item.pinned = pinned
        return True

    def delete(self, item_id: int) -> bool:
        if item_id not in self._items:
            return False
        self._exec("DELETE FROM items WHERE id = ?", (item_id,))
        del self._items[item_id]
        return True

    def _exec(self, sql: str, params: tuple[object, ...]) -> None:
        try:
            self._db().execute(sql, params)
        except sqlite3.DatabaseError as e:
            raise StoreError(f"DB に書けません({type(e).__name__})") from None

    def _delete_ids(self, ids: list[int]) -> int:
        if not ids:
            return 0
        db = self._db()
        try:
            db.execute("BEGIN")
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                db.execute(f"DELETE FROM items WHERE id IN ({','.join('?' * len(chunk))})", chunk)
            db.execute("COMMIT")
        except sqlite3.DatabaseError as e:
            try:
                db.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                pass
            raise StoreError(f"DB から削除できません({type(e).__name__})") from None
        for i in ids:
            self._items.pop(i, None)
        return len(ids)

    # ------------------------------------------------------------ 保持上限・全消去
    def trim(self, max_items: int, max_days: int) -> int:
        """ピン以外の履歴を、max_items 件・max_days 日を超えた分だけ古い順に削除する(0 は無制限)。"""
        cands = sorted((i for i in self._items.values() if i.kind == KIND_HISTORY and not i.pinned),
                       key=lambda i: (i.last_used_at, i.id), reverse=True)
        doomed: set[int] = set()
        if max_items > 0:
            doomed.update(i.id for i in cands[max_items:])
        if max_days > 0:
            limit = self._now() - timedelta(days=max_days)
            doomed.update(i.id for i in cands if i.last_used_at < limit)
        return self._delete_ids(sorted(doomed))

    def clear_all(self, include_pins: bool) -> int:
        """kind=history を削除(ピンは include_pins のときだけ)。定型文は消さない。その後 VACUUM とキャッシュ破棄。"""
        ids = [i.id for i in self._items.values() if i.kind == KIND_HISTORY and (include_pins or not i.pinned)]
        n = self._delete_ids(ids)
        try:
            self._db().execute("VACUUM")
        except sqlite3.DatabaseError as e:
            raise StoreError(f"VACUUM に失敗しました({type(e).__name__})") from None
        self._load()  # メモリキャッシュを捨てて読み直す(残るのはピンと定型文)
        return n
