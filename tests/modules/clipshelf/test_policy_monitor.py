# AC-2(policy.decide の理由コード / 本文取得後の duplicate・empty・too_large)と AC-7(除外時に本文を読まない)、
# D-15(起動時の内容を記録しない)、observe モード(FR-18)、clipboard_busy(FR-2)の検査。
from __future__ import annotations

import inspect
from typing import Any

import pytest

from deskkit.modules.clipshelf import policy
from deskkit.modules.clipshelf._win32 import FMT_CAN_INCLUDE_HISTORY, FMT_CAN_UPLOAD_CLOUD, FMT_EXCLUDE_MONITOR, FMT_ORIGIN
from deskkit.modules.clipshelf.writer import encode_origin

TEXT = frozenset({"CF_UNICODETEXT", "CF_LOCALE", "CF_TEXT"})
PCFG = policy.PolicyConfig(frozenset({"blocked.exe"}), "skip")


# ------------------------------------------------------------------ AC-2: 純関数 decide
@pytest.mark.parametrize(
    ("formats", "owner", "paused", "expected"),
    [
        (TEXT, "editor.exe", False, policy.RECORD),
        (TEXT | {FMT_ORIGIN}, "editor.exe", False, policy.SELF_ORIGIN),
        (TEXT | {FMT_EXCLUDE_MONITOR}, "editor.exe", False, policy.EXCLUDE_FORMAT),
        (TEXT | {FMT_CAN_INCLUDE_HISTORY}, "editor.exe", False, policy.HISTORY_DISALLOWED),
        (TEXT, "BLOCKED.EXE", False, policy.EXCLUDED_APP),
        (TEXT, None, False, policy.OWNER_UNKNOWN),
        (frozenset({"CF_DIB", "CF_BITMAP"}), "paint.exe", False, policy.NOT_TEXT),
        (TEXT, "editor.exe", True, policy.PAUSED),
        # 除外形式は所有者や除外アプリより優先する
        (TEXT | {FMT_EXCLUDE_MONITOR}, None, False, policy.EXCLUDE_FORMAT),
        # CanUploadToCloudClipboard は判定に使わない(観測ログのみ)
        (TEXT | {FMT_CAN_UPLOAD_CLOUD}, "editor.exe", False, policy.RECORD),
    ],
)
def test_decide_reason_codes(formats: frozenset[str], owner: str | None, paused: bool, expected: str) -> None:
    assert policy.decide(policy.ClipFacts(formats, owner), PCFG, paused) == expected


def test_decide_unknown_owner_record_policy() -> None:
    cfg = policy.PolicyConfig(frozenset(), "record")
    assert policy.decide(policy.ClipFacts(TEXT, None), cfg, False) == policy.RECORD


def test_decide_does_not_take_text() -> None:
    # FR-3: 本文を引数にとらない
    params = list(inspect.signature(policy.decide).parameters)
    assert params == ["facts", "config", "paused"]
    fields = set(policy.ClipFacts.__dataclass_fields__)
    assert fields == {"formats", "owner_exe"}


def test_reason_vocabulary_is_fixed() -> None:
    assert set(policy.REASON_CODES) == {
        "recorded", "observe_only", "self_origin", "duplicate", "exclude_format", "history_disallowed", "excluded_app",
        "owner_unknown", "not_text", "empty", "too_large", "paused", "clipboard_busy",
    }


# ------------------------------------------------------------------ AC-7: 除外なら GetClipboardData(CF_UNICODETEXT) を呼ばない
@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (lambda e: e.api.put("secret", formats={FMT_EXCLUDE_MONITOR: b"\x00\x00\x00\x00"}), policy.EXCLUDE_FORMAT),
        (lambda e: e.api.put("secret", formats={FMT_EXCLUDE_MONITOR: b""}), policy.EXCLUDE_FORMAT),
        (lambda e: e.api.put("secret", formats={FMT_CAN_INCLUDE_HISTORY: b"\x00\x00\x00\x00"}), policy.HISTORY_DISALLOWED),
        (lambda e: e.api.put("secret", formats={FMT_CAN_INCLUDE_HISTORY: b"\x01\x00\x00\x00"}), policy.HISTORY_DISALLOWED),
        (lambda e: e.api.put("secret", formats={FMT_CAN_INCLUDE_HISTORY: b"\x07"}), policy.HISTORY_DISALLOWED),
        (lambda e: e.api.put("secret", owner_exe="C:\\Apps\\Blocked.exe"), policy.EXCLUDED_APP),
        (lambda e: e.api.put("secret", owner_exe=None), policy.OWNER_UNKNOWN),
        (lambda e: e.api.put(None, formats={"PNG": b"\x89PNG"}), policy.NOT_TEXT),
        (lambda e: (e.api.put("secret"), e.paused.__setitem__(0, True)), policy.PAUSED),
        (lambda e: e.api.put("secret", formats={FMT_ORIGIN: encode_origin(0)}), policy.SELF_ORIGIN),
    ],
)
def test_excluded_updates_never_read_text(make_env: Any, setup: Any, reason: str) -> None:
    env = make_env(exclude_exes=["blocked.exe"])
    setup(env)
    dec = env.mon.process()
    assert dec is not None and dec.reason == reason
    assert env.api.text_reads == 0
    assert env.store.row_count() == 0
    assert not env.api.is_open  # 閉じ忘れない


def test_busy_clipboard_is_reported_and_not_read(make_env: Any) -> None:
    env = make_env(open_retry=3)
    env.api.put("x")
    env.api.busy_opens = 3
    dec = env.mon.process()
    assert dec is not None and dec.reason == policy.CLIPBOARD_BUSY
    assert env.api.opens == 3 and env.api.text_reads == 0


def test_busy_then_success_within_retry(make_env: Any) -> None:
    env = make_env(open_retry=3)
    env.api.put("hello")
    env.api.busy_opens = 2
    dec = env.mon.process()
    assert dec is not None and dec.reason == policy.RECORDED


# ------------------------------------------------------------------ AC-2 後半: 本文取得後の判定
def test_record_then_duplicate(make_env: Any) -> None:
    env = make_env()
    env.api.put("hello world")
    d1 = env.mon.process()
    assert d1 is not None and d1.reason == policy.RECORDED and env.store.row_count() == 1
    env.clock.advance(minutes=5)
    env.api.put("hello world")
    d2 = env.mon.process()
    assert d2 is not None and d2.reason == policy.DUPLICATE and d2.item_id == d1.item_id
    assert env.store.row_count() == 1
    assert env.store.get(d1.item_id).last_used_at == env.clock()


@pytest.mark.parametrize("text", ["", "   ", "\r\n\t "])
def test_empty(make_env: Any, text: str) -> None:
    env = make_env()
    env.api.put(text)
    dec = env.mon.process()
    assert dec is not None and dec.reason == policy.EMPTY and env.store.row_count() == 0


def test_too_large_is_not_truncated(make_env: Any) -> None:
    env = make_env(max_chars=10)
    env.api.put("x" * 11)
    dec = env.mon.process()
    assert dec is not None and dec.reason == policy.TOO_LARGE and env.store.row_count() == 0
    env.api.put("y" * 10)
    dec = env.mon.process()
    assert dec is not None and dec.reason == policy.RECORDED


def test_self_origin_only_touches(make_env: Any) -> None:
    env = make_env()
    env.api.put("abc")
    d = env.mon.process()
    before = env.store.get(d.item_id).last_used_at
    env.clock.advance(minutes=3)
    env.api.put("abc", formats={FMT_ORIGIN: encode_origin(d.item_id)})
    d2 = env.mon.process()
    assert d2.reason == policy.SELF_ORIGIN and d2.item_id == d.item_id
    assert env.store.row_count() == 1
    assert env.store.get(d.item_id).last_used_at > before
    assert env.api.text_reads == 1  # 自己印の更新では本文を読まない


# ------------------------------------------------------------------ D-15 / FR-18
def test_startup_content_is_not_recorded(make_env: Any) -> None:
    env = make_env()
    env.api.put("left over secret")
    env.mon.mark_startup()  # この時点の内容は起動前からあったもの
    assert env.mon.process() is None
    assert env.api.text_reads == 0 and env.store.row_count() == 0


def test_observe_mode_logs_reason_without_reading(make_env: Any) -> None:
    env = make_env(mode="observe")
    for i in range(10):
        env.api.put(f"text {i}")
        env.mon.process()
    assert env.store.row_count() == 0
    assert env.api.text_reads == 0
    lines = [r.getMessage() for r in env.log_records if "clip decision" in r.getMessage()]
    assert 1 <= len(lines) <= 10
    assert all("reason=observe_only" in ln and "formats=[" in ln and "owner=" in ln for ln in lines)
    assert not any("text " in ln for ln in lines)


def test_logs_never_contain_text(make_env: Any) -> None:
    env = make_env()
    marker = "CLIPSHELF-CANARY-TEST-1234"
    env.api.put(marker)
    env.mon.process()
    env.api.put(marker + "x" * 5, formats={FMT_EXCLUDE_MONITOR: b"\x00"})
    env.mon.process()
    for r in env.log_records:
        msg = r.getMessage()
        assert marker not in msg
    if env.ops.path.exists():
        assert marker not in env.ops.path.read_text(encoding="utf-8")
