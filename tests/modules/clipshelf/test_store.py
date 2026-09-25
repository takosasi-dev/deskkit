# AC-10(保持上限: ピンと定型文は残り、ops.jsonl に retention_trim)と AC-11(全消去: ピンと定型文は不変、clear_all)、
# スキーマ(§9.3)・secure_delete・暗号化・壊れた DB を作り直さないこと・復号できない行を表示しないことの検査。
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from deskkit.modules.clipshelf import policy
from deskkit.modules.clipshelf.fakes import FakeCipher, FakeClock
from deskkit.modules.clipshelf.ops import OpsLog
from deskkit.modules.clipshelf.store import Store, StoreError


def _record_n(env: Any, n: int, prefix: str = "item") -> list[int]:
    ids: list[int] = []
    for i in range(n):
        env.clock.advance(minutes=1)
        env.api.put(f"{prefix} {i}")
        d = env.mon.process()
        assert d is not None and d.reason == policy.RECORDED
        ids.append(d.item_id)
    return ids


# ------------------------------------------------------------------ AC-10
def test_retention_max_items_keeps_pins_and_snippets(make_env: Any) -> None:
    max_items = 20
    env = make_env(retention={"max_items": max_items, "max_days": 0})
    first = _record_n(env, 2, "pinned")
    for i in first:
        env.store.set_pinned(i, True)
    env.store.add_snippet("署名", "よろしくお願いします")
    env.store.add_snippet("住所", "〒000-0000")
    _record_n(env, max_items + 10)
    hist = env.store.history()
    unpinned = [h for h in hist if not h.pinned]
    assert len(unpinned) <= max_items
    assert sum(1 for h in hist if h.pinned) == 2
    assert len(env.store.snippets()) == 2
    trims = [o for o in env.ops.read() if o.get("op") == "retention_trim"]
    assert trims and all(isinstance(o["deleted"], int) and o["deleted"] > 0 for o in trims)
    # 残っているのは新しい方
    texts = {h.text for h in unpinned}
    assert f"item {max_items + 9}" in texts and "item 0" not in texts


def test_retention_max_days(make_env: Any) -> None:
    env = make_env(retention={"max_items": 0, "max_days": 30})
    old = _record_n(env, 3, "old")
    env.store.set_pinned(old[0], True)
    env.clock.advance(days=31)
    _record_n(env, 1, "new")
    left = {h.text for h in env.store.history()}
    assert left == {"old 0", "new 0"}  # ピンは期限の対象外


# ------------------------------------------------------------------ AC-11
def test_clear_all_without_pins(make_env: Any) -> None:
    env = make_env()
    ids = _record_n(env, 6)
    env.store.set_pinned(ids[0], True)
    env.store.set_pinned(ids[1], True)
    env.store.add_snippet("a", "b")
    n = env.store.clear_all(include_pins=False)
    env.ops.write("clear_all", deleted=n, pins_deleted=False)
    assert n == 4
    db = sqlite3.connect(env.store.path)
    try:
        assert db.execute("SELECT COUNT(*) FROM items WHERE kind='history' AND pinned=0").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM items WHERE kind='history' AND pinned=1").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM items WHERE kind='snippet'").fetchone()[0] == 1
    finally:
        db.close()
    assert env.store.counts() == {"history": 2, "pinned": 2, "snippets": 1, "undecryptable": 0}
    ops = [o for o in env.ops.read() if o.get("op") == "clear_all"]
    assert ops == [{"ts": ops[0]["ts"], "op": "clear_all", "deleted": 4, "pins_deleted": False}]


def test_clear_all_with_pins_keeps_snippets(make_env: Any) -> None:
    env = make_env()
    ids = _record_n(env, 3)
    env.store.set_pinned(ids[0], True)
    env.store.add_snippet("a", "b")
    assert env.store.clear_all(include_pins=True) == 3
    assert env.store.counts()["history"] == 0 and env.store.counts()["snippets"] == 1


# ------------------------------------------------------------------ スキーマ・暗号化
def test_schema_is_exactly_as_spec(tmp_path: Path) -> None:
    st = Store(tmp_path / "c.db", FakeCipher(), FakeClock())
    st.open()
    st.close()
    db = sqlite3.connect(tmp_path / "c.db")
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {"meta", "items"}
        cols = [r[1] for r in db.execute("PRAGMA table_info(items)")]
        assert cols == ["id", "kind", "created_at", "last_used_at", "pinned", "payload"]
        assert [r[0] for r in db.execute("SELECT key FROM meta")] == ["schema_version"]
    finally:
        db.close()


def test_payload_is_ciphertext_and_secure_delete_on(tmp_path: Path) -> None:
    clock = FakeClock()
    st = Store(tmp_path / "c.db", FakeCipher(), clock)
    st.open()
    st.add_history("PLAINTEXT-MARKER", "editor.exe")
    assert st._db().execute("PRAGMA secure_delete").fetchone()[0] == 1
    st.close()
    raw = (tmp_path / "c.db").read_bytes()
    assert b"PLAINTEXT-MARKER" not in raw and "PLAINTEXT-MARKER".encode("utf-16-le") not in raw
    assert b"editor.exe" not in raw  # コピー元 exe も payload の中(平文列を作らない)


def test_undecryptable_rows_are_hidden_not_deleted(tmp_path: Path) -> None:
    st = Store(tmp_path / "c.db", FakeCipher(), FakeClock())
    st.open()
    st.add_history("a", None)
    st._db().execute("INSERT INTO items (kind, created_at, last_used_at, pinned, payload) VALUES "
                     "('history', '2026-01-01T00:00:00+09:00', '2026-01-01T00:00:00+09:00', 0, x'00112233')")
    st.close()
    st.open()
    assert st.undecryptable == 1 and len(st.items()) == 1 and st.row_count() == 2
    st.trim(1, 0)
    assert st.row_count() == 2  # 自動削除しない
    st.close()


def _insert_undecryptable(st: Store, kind: str, pinned: int) -> None:
    st._db().execute("INSERT INTO items (kind, created_at, last_used_at, pinned, payload) VALUES "
                     "(?, '2026-01-01T00:00:00+09:00', '2026-01-01T00:00:00+09:00', ?, x'00112233')", (kind, pinned))


@pytest.mark.parametrize("include_pins", [False, True])
def test_clear_all_removes_undecryptable_history_rows(tmp_path: Path, include_pins: bool) -> None:
    """全消去は、復号できずメモリに載らなかった履歴の行も DB から消す(警告が消えないバグの回帰テスト)。"""
    st = Store(tmp_path / "c.db", FakeCipher(), FakeClock())
    st.open()
    st.add_history("a", None)
    pin = st.add_history("p", None)
    st.set_pinned(pin.id, True)
    st.add_snippet("s", "t")
    _insert_undecryptable(st, "history", 0)
    _insert_undecryptable(st, "history", 1)  # 復号できない行はピンの列に関係なく消す
    _insert_undecryptable(st, "snippet", 0)  # 定型文は消さない
    st.close()
    st.open()
    assert st.undecryptable == 3 and st.undecryptable_counts() == {"history": 2, "snippets": 1}
    n = st.clear_all(include_pins=include_pins)
    assert n == (4 if include_pins else 3)
    assert st.undecryptable == 1 and st.undecryptable_counts() == {"history": 0, "snippets": 1}
    db = sqlite3.connect(st.path)
    try:
        assert db.execute("SELECT COUNT(*) FROM items WHERE kind='history'").fetchone()[0] == (0 if include_pins else 1)
        assert db.execute("SELECT COUNT(*) FROM items WHERE kind='snippet'").fetchone()[0] == 2
    finally:
        db.close()
    st.close()


def test_corrupt_db_is_not_recreated(tmp_path: Path) -> None:
    p = tmp_path / "c.db"
    p.write_bytes(b"this is not a sqlite database at all" * 100)
    before = p.read_bytes()
    st = Store(p, FakeCipher(), FakeClock())
    with pytest.raises(StoreError) as ei:
        st.open()
    assert "開けません" in str(ei.value) or "スキーマ" in str(ei.value)
    assert p.read_bytes() == before


def test_ops_rejects_non_scalar(tmp_path: Path) -> None:
    ops = OpsLog(tmp_path / "ops.jsonl")
    with pytest.raises(TypeError):
        ops.write("x", data=["list"])  # type: ignore[arg-type]


def test_history_created_dates_works_from_another_thread(make_env: Any) -> None:
    # 利用状況は集計スレッドから usage() を呼ぶ(v0.2)。GUI スレッドの接続を使うと ProgrammingError になる。
    import threading

    env = make_env()
    _record_n(env, 3)
    env.store.add_snippet("署名", "よろしく")
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["dates"] = env.store.history_created_dates()
        except Exception as e:  # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=work)
    t.start()
    t.join(10)
    assert "error" not in box, box.get("error")
    assert len(box["dates"]) == 3
