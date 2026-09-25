# v0.2 C3: アプリ別の短命記録。対象 exe からのコピーは期限つきで記録し、期限を過ぎたら掃除で消す(ピンは消さない)。
# 判定は exe 名だけ。期限は暗号化した payload の中に持ち、平文の列を足さない。ops.jsonl には件数だけ(expired)。
from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from deskkit.modules.clipshelf import config as cfgmod
from deskkit.modules.clipshelf import policy
from deskkit.modules.clipshelf.crypto import Payload, decode_payload, encode_payload
from deskkit.modules.clipshelf.fakes import FakeCipher, FakeClock
from deskkit.modules.clipshelf.store import Store

SECTION = {"mode": "record", "short_lived": {"exes": ["Vault.EXE"], "minutes": 5}}


def test_config_defaults_and_validation() -> None:
    sec, _ = cfgmod.merge_defaults({})
    assert sec["short_lived"] == {"exes": [], "minutes": 10} and sec["pause_in_modes"] == []
    cfg = cfgmod.parse({"short_lived": {"exes": ["C:\\x\\Vault.EXE"], "minutes": 3}, "pause_in_modes": [" a ", "a", "", "b"]})
    assert cfg.short_lived_exes == frozenset({"vault.exe"}) and cfg.short_lived_minutes == 3
    assert cfg.pause_in_modes == ("a", "b")
    for bad in ({"short_lived": {"exes": [], "minutes": 0}}, {"short_lived": []}, {"pause_in_modes": "a"},
                {"short_lived": {"exes": "x.exe", "minutes": 5}}):
        try:
            cfgmod.parse(bad)
        except ValueError:
            continue
        raise AssertionError(f"不正な設定を受け付けた: {bad}")


def test_payload_roundtrip_and_backward_compat() -> None:
    c = FakeCipher()
    t = FakeClock()()
    p = decode_payload(c, encode_payload(c, Payload("x", "a.exe", None, t)))
    assert p.expires_at == t
    old = decode_payload(c, c.protect(b'{"text": "x", "source_exe": null, "name": null}'))  # v0.1 の形
    assert old.expires_at is None
    broken = decode_payload(c, c.protect(b'{"text": "x", "source_exe": null, "name": null, "expires_at": "??"}'))
    assert broken.expires_at is not None and broken.expires_at < t  # 読めない期限は期限切れ扱い


def test_short_lived_items_expire(module_factory: Any) -> None:
    m, ctx, api, clock = module_factory(SECTION)
    api.put("from vault", owner_exe="C:\\Apps\\vault.exe")
    d1 = m.monitor.process()
    api.put("from editor")
    d2 = m.monitor.process()
    api.put("pinned vault", owner_exe="C:\\Apps\\vault.exe")
    d3 = m.monitor.process()
    assert {d1.reason, d2.reason, d3.reason} == {policy.RECORDED}
    m.store.set_pinned(d3.item_id, True)
    short = m.store.get(d1.item_id)
    assert short.expires_at == clock() + timedelta(minutes=5)
    assert m.store.get(d2.item_id).expires_at is None
    assert m.diagnostics()["short_lived_items"] == 2
    clock.advance(minutes=4, seconds=59)
    m._periodic()
    assert m.store.get(d1.item_id) is not None
    clock.advance(seconds=1)
    m._periodic()
    assert m.store.get(d1.item_id) is None  # 期限切れで消えた
    assert m.store.get(d2.item_id) is not None and m.store.get(d3.item_id) is not None  # 他のアプリとピンは残る
    ops = [o for o in m.ops.read() if o["op"] == "expired"]
    assert ops == [{"ts": ops[0]["ts"], "op": "expired", "deleted": 1}]


def test_expired_items_swept_at_palette_open(module_factory: Any) -> None:
    m, ctx, api, clock = module_factory(SECTION)
    api.put("from vault", owner_exe="C:\\Apps\\vault.exe")
    d = m.monitor.process()
    clock.advance(minutes=6)
    m.open_palette()  # 定期掃除を待たずに、開くときに消す
    assert m.store.get(d.item_id) is None and m._palette is not None and m._palette.model.rowCount() == 0
    m._palette.dismiss(animated=False)


def test_expired_items_swept_at_startup(module_factory: Any) -> None:
    from deskkit.modules.clipshelf.module import ClipShelfModule

    m, ctx, api, clock = module_factory(SECTION)
    api.put("from vault", owner_exe="C:\\Apps\\vault.exe")
    m.monitor.process()
    m.stop()
    clock.advance(hours=1)  # 停止中に期限が過ぎた
    m2 = ClipShelfModule(ctx, api=api, cipher=FakeCipher(), now=clock)  # type: ignore[arg-type]
    m2.start()
    try:
        assert m2.counts()["history"] == 0
        assert [o["deleted"] for o in m2.ops.read() if o["op"] == "expired"] == [1]
    finally:
        m2.stop()


def test_store_expire_skips_pins_and_snippets(tmp_path: Path) -> None:
    clock = FakeClock()
    st = Store(tmp_path / "s.db", FakeCipher(), clock)
    st.open()
    past = clock() - timedelta(minutes=1)
    st.add_history("old", "vault.exe", past)
    pin = st.add_history("pinned", "vault.exe", past)
    st.set_pinned(pin.id, True)
    st.add_history("future", "vault.exe", clock() + timedelta(minutes=1))
    st.add_snippet("s", "t")
    assert st.expire() == 1 and st.row_count() == 3
    st.close()


def test_expiry_is_not_a_plaintext_column(tmp_path: Path) -> None:
    clock = FakeClock()
    st = Store(tmp_path / "c.db", FakeCipher(), clock)
    st.open()
    st.add_history("x", "vault.exe", clock())
    st.close()
    db = sqlite3.connect(tmp_path / "c.db")
    try:
        cols = [r[1] for r in db.execute("PRAGMA table_info(items)")]
    finally:
        db.close()
    assert cols == ["id", "kind", "created_at", "last_used_at", "pinned", "payload"]
    assert b"vault" not in (tmp_path / "c.db").read_bytes()


def test_observe_mode_and_unknown_owner_have_no_expiry(module_factory: Any) -> None:
    m, ctx, api, clock = module_factory({**SECTION, "unknown_owner_policy": "record"})
    api.put("no owner", owner_exe=None)
    d = m.monitor.process()
    assert d.reason == policy.RECORDED and m.store.get(d.item_id).expires_at is None


def test_diagnostics_has_no_content(module_factory: Any) -> None:
    m, ctx, api, clock = module_factory({**SECTION, "exclude_exes": ["secretapp.exe"], "pause_in_modes": ["meeting"]})
    api.put("DIAG-BODY", owner_exe="C:\\Apps\\vault.exe")
    m.monitor.process()
    ctx.fire("modeshift.switched", {"mode": "meeting", "run_id": "r", "failed": 0})
    m.set_paused(True)
    diag = m.diagnostics()
    assert all(isinstance(v, (str, int, bool)) for v in diag.values())
    flat = " ".join(f"{k}={v}" for k, v in diag.items())
    for secret in ("DIAG-BODY", "vault", "secretapp", "meeting"):
        assert secret not in flat
    assert diag["mode"] == "record" and diag["history"] == 1 and diag["short_lived_items"] == 1
    assert diag["recording_paused"] is True and diag["paused_reasons"] == "manual,mode"
    assert diag["short_lived_apps"] == 1 and diag["exclude_apps"] == 1 and diag["pause_in_modes"] == 1


def test_page_short_lived_settings(module_factory: Any, qapp: Any) -> None:
    m, ctx, api, clock = module_factory({"mode": "record"})
    page = m.create_page()
    page.short_editor.add_value("C:\\Apps\\Vault.exe")
    assert ctx.writes[-1][0]["short_lived"]["exes"] == ["vault.exe"]
    page.sp_short.setValue(30)
    assert ctx.writes[-1][0]["short_lived"] == {"exes": ["vault.exe"], "minutes": 30}
    assert m.config.short_lived_minutes == 30
    api.put("s", owner_exe="C:\\Apps\\vault.exe")
    m.monitor.process()
    page._refresh()
    assert "1 件" in page.short_state.text()
    page.close()
