# 書式・ファイル名の検証(AC-7・AC-8)と、追記・取り消し・照合の決まり(AC-1〜3・AC-5・AC-6・AC-12・J-9〜J-12・J-18・J-19)。
# ファイルは偽の Win32(メモリの中)に書く。
from __future__ import annotations

import hashlib
from datetime import datetime

import pytest

from deskkit.modules.jotdrop import compose, target, writer
from deskkit.modules.jotdrop._win32 import (
    CREATE_NEW,
    FILE_APPEND_DATA,
    FILE_SHARE_READ,
    FILE_WRITE_DATA,
    GENERIC_WRITE,
    OPEN_EXISTING,
)
from deskkit.modules.jotdrop.fakes import FakeWin32, norm

D = "C:\\Notes"
P = D + "\\2026-09-26.md"
WHEN = datetime(2026, 9, 26, 14, 5)


def no_sleep(_s: float) -> bool:
    return False


def job(path: str = P, line: str = "- 14:05 メモ", jid: str = "j1") -> writer.Job:
    return writer.Job(jid, WHEN, path, line)


def api_with(data: bytes | None, path: str = P) -> FakeWin32:
    a = FakeWin32()
    a.add_dir(D)
    if data is not None:
        a.put(path, data)
    return a


def append(a: FakeWin32, fr: writer.Framing | None = None, j: writer.Job | None = None) -> writer.WriteResult:
    return writer.append_line(a, j or job(), fr or writer.Framing(), sleep=no_sleep)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ------------------------------------------------------------------ AC-7・AC-8
@pytest.mark.parametrize("fmt", ["- {time}", "{text} {text}", "{name} {text}", "a\n{text}", "a\r{text}",
                                 "{text:x}", "{text", "}{text}", "{date:{x}} {text}", "{text.__class__}"])
def test_ac7_bad_formats(fmt: str) -> None:
    assert compose.validate_line_format(fmt) is not None


@pytest.mark.parametrize("fmt", ["- {time} {text}", "### [{time}] {text}", "{text}", "{{x}} {text} {date:YYYY}"])
def test_good_formats(fmt: str) -> None:
    assert compose.validate_line_format(fmt) is None


def test_ac7_expand_shapes() -> None:
    assert compose.expand("{date:YYYY年MM月DD日} {time:HH時mm分} {text}", WHEN, "x") == "2026年09月26日 14時05分 x"
    assert compose.expand("{date} {time} {text}", WHEN, "x") == "2026-09-26 14:05 x"
    assert compose.expand("{{{text}}}", WHEN, "a{b}") == "{a{b}}"
    # 5 つの記号以外はそのまま(ss や yyyy は置き換えない)
    assert compose.expand("{time:HH:mm:ss yyyy} {text}", WHEN, "x") == "14:05:ss yyyy x"


@pytest.mark.parametrize("pattern", ["a:b.md", "CON.md", "con.txt", "LPT1.md", "x.", "x ", "{date}.json", "a/b.md",
                                     "a\\b.md", "{time}.md", "a*b.md", "", ".md?"])
def test_ac8_bad_patterns(pattern: str) -> None:
    assert compose.validate_pattern(pattern, WHEN) is not None


@pytest.mark.parametrize("pattern", ["{date}.md", "memo.txt", "{date:YYYY-MM}.md", "日報 {date}.md"])
def test_good_patterns(pattern: str) -> None:
    assert compose.validate_pattern(pattern, WHEN) is None


def test_header_and_separator_validation() -> None:
    assert compose.validate_header("# {date} 開発ログ") is None
    assert compose.validate_header("# {text}") is not None
    assert compose.validate_separator("---") is None
    assert compose.validate_separator("a\nb") is not None


def test_j14_clean_text_keeps_markdown() -> None:
    assert compose.clean_text("[[リンク]] #タグ **太字**", 100) == "[[リンク]] #タグ **太字**"
    assert compose.clean_text("  a\r\nb\nc\u2028d\u2029e\x00\x1bf\tg  ", 100) == "a b c d ef\tg"
    assert compose.clean_text("x" * 50, 20) == "x" * 20


def test_ac13_date_rollover() -> None:
    assert target.resolve(D, "{date}.md", datetime(2026, 9, 26, 23, 59, 59)) == D + "\\2026-09-26.md"
    assert target.resolve(D, "{date}.md", datetime(2026, 9, 27, 0, 0, 1)) == D + "\\2026-09-27.md"


def test_folder_validation() -> None:
    assert target.validate_folder("") is None
    assert target.validate_folder("C:\\Notes") is None
    assert target.validate_folder("\\\\server\\share") is not None
    assert target.validate_folder("Notes") is not None


# ------------------------------------------------------------------ AC-1・AC-2
def test_ac1_mixed_newlines_no_trailing_newline() -> None:
    old = b"a\r\nb\nc\r\nlast line"
    a = api_with(old)
    res = append(a)
    data = a.get(P) or b""
    assert res.ok and sha(data[:len(old)]) == sha(old)
    assert data[len(old):] == "\r\n- 14:05 メモ\r\n".encode()
    old2 = b"a\r\nb\nlast\n"
    a2 = api_with(old2)
    append(a2)
    assert (a2.get(P) or b"")[len(old2):] == "- 14:05 メモ\n".encode()


def test_ac2_append_access_rights() -> None:
    a = api_with(b"x\n")
    append(a)
    appends = [o for o in a.opens if o.path == P]
    assert appends and all(o.access & FILE_APPEND_DATA for o in appends)
    assert all(not o.access & FILE_WRITE_DATA and not o.access & GENERIC_WRITE for o in appends)
    assert all(o.share == FILE_SHARE_READ for o in appends)
    # 新しいファイル: OPEN_EXISTING → CREATE_NEW の順で、どちらも同じ権限
    b = api_with(None)
    append(b)
    assert [o.disposition for o in b.opens] == [OPEN_EXISTING, CREATE_NEW]
    assert all(not o.access & (FILE_WRITE_DATA | GENERIC_WRITE) for o in b.opens)


def test_create_new_race_falls_back_to_open_existing() -> None:
    class Racy(FakeWin32):
        """1回目の OPEN_EXISTING のあと、CREATE_NEW の前にほかのアプリがファイルを作った。"""

        def create_file(self, path: str, access: int, share: int, disposition: int) -> int:
            if disposition == CREATE_NEW and self.get(path) is None:
                self.put(path, b"other\n")
            return super().create_file(path, access, share, disposition)

    a = Racy()
    a.add_dir(D)
    res = append(a)
    assert res.ok and not res.created_file
    assert [o.disposition for o in a.opens] == [OPEN_EXISTING, CREATE_NEW, OPEN_EXISTING]
    assert a.get(P) == "other\n- 14:05 メモ\n".encode()


# ------------------------------------------------------------------ AC-3・J-12
DEVLOG = writer.Framing(blank_line_before=True, separator="---", header="# {date} 開発ログ")
HEAD = "### \U0001F552 [14:05] 本文"


def test_ac3_devlog_existing() -> None:
    old = "# 2026-09-26 開発ログ\n\n### \U0001F552 [13:00] 前の項目\n本文\n\n---\n".encode()
    a = api_with(old)
    append(a, DEVLOG, job(line=HEAD))
    assert (a.get(P) or b"")[len(old):] == ("\n---\n\n" + HEAD + "\n").encode()
    old2 = "### x\n本文\n".encode()
    a2 = api_with(old2)
    append(a2, DEVLOG, job(line=HEAD))
    assert (a2.get(P) or b"")[len(old2):] == ("\n---\n\n" + HEAD + "\n").encode()


def test_ac3_devlog_new_file() -> None:
    a = api_with(None)
    res = append(a, DEVLOG, job(line=HEAD))
    assert res.created_file
    assert a.get(P) == ("# 2026-09-26 開発ログ\n\n" + HEAD + "\n").encode()


def test_blank_line_rules() -> None:
    fr = writer.Framing(blank_line_before=True)
    a = api_with(b"x\n\n")        # 末尾が空行ならもう1つは足さない
    append(a, fr)
    assert a.get(P) == "x\n\n- 14:05 メモ\n".encode()
    b = api_with(b"x")            # 改行なし → 改行 + 空行
    append(b, fr)
    assert b.get(P) == "x\n\n- 14:05 メモ\n".encode()
    c = api_with(b"")             # 空のファイルには空行も区切りも入れない
    append(c, DEVLOG)
    assert c.get(P) == "- 14:05 メモ\n".encode()
    d = api_with(b"x\n")          # 区切りの前は必ず空行(blank_line_before が偽でも)
    append(d, writer.Framing(separator="***"))
    assert d.get(P) == "x\n\n***\n\n- 14:05 メモ\n".encode()


def test_newline_setting_fixed() -> None:
    a = api_with(b"a\nb\n")
    append(a, writer.Framing(newline="crlf"))
    assert (a.get(P) or b"").endswith("- 14:05 メモ\r\n".encode())
    b = api_with(b"a\r\n")
    append(b, writer.Framing(newline="lf"))
    assert (b.get(P) or b"").endswith("a\r\n- 14:05 メモ\n".encode())
    c = api_with(None)
    append(c)
    assert c.get(P) == "- 14:05 メモ\n".encode()   # 新しいファイルは LF


def test_utf8_bom_and_multibyte_tail_ok() -> None:
    old = b"\xef\xbb\xbf" + ("あ" * 30000).encode()   # 90KB: 末尾 64KB の切れ目が文字の途中になる
    a = api_with(old)
    assert append(a).ok


# ------------------------------------------------------------------ AC-5・AC-6・FR-9
def test_ac5_not_utf8() -> None:
    old = "メモ帳の文".encode("cp932")
    a = api_with(old)
    assert append(a).reason == target.REASON_NOT_UTF8 and a.get(P) == old
    b = api_with(b"\xff\xfeh\x00i\x00")
    assert append(b).reason == target.REASON_NOT_UTF8
    c = api_with(b"\xfe\xff\x00h")
    assert append(c).reason == target.REASON_NOT_UTF8


def test_ac6_network() -> None:
    a = FakeWin32()
    a.remote.add("Z:\\")
    a.add_dir("Z:\\memo")
    assert append(a, j=job("Z:\\memo\\a.md")).reason == target.REASON_NETWORK
    assert append(a, j=job("\\\\server\\share\\a.md")).reason == target.REASON_NETWORK
    assert append(a, j=job("//server/share/a.md")).reason == target.REASON_NETWORK
    assert not a.opens   # 開きもしない


def test_refusals() -> None:
    a = api_with(b"x\n")
    assert append(a, j=job(D + "\\a.json")).reason == target.REASON_BAD_TARGET
    assert append(a, j=job("C:\\Missing\\a.md")).reason == target.REASON_FOLDER_MISSING
    assert not a.is_dir("C:\\Missing")
    a.add_dir(D + "\\dir.md")
    assert append(a, j=job(D + "\\dir.md")).reason == target.REASON_OPEN_FAILED
    a.readonly.add(norm(P))
    assert append(a).reason == target.REASON_DENIED and a.get(P) == b"x\n"
    b = api_with(None)
    assert append(b, writer.Framing(create_file=False)).reason == target.REASON_NO_FILE and b.get(P) is None


def test_j9_busy_retries_then_pending() -> None:
    a = api_with(b"x\n")
    a.locked.add(norm(P))
    waits: list[float] = []
    res = writer.append_line(a, job(), writer.Framing(), sleep=lambda s: bool(waits.append(s)))
    assert res.reason == target.REASON_BUSY and res.retries == 4
    assert waits == [0.2] * 4 and a.get(P) == b"x\n"
    b = api_with(b"x\n")
    b.fail_append.add(norm(P))     # WriteFile の失敗も使用中として扱う
    assert append(b).reason == target.REASON_BUSY
    c = api_with(b"x\n")
    c.locked.add(norm(P))
    assert writer.append_line(c, job(), writer.Framing(), sleep=lambda _s: True).retries == 0  # 中止ですぐ抜ける


# ------------------------------------------------------------------ AC-12・J-18・INV-2
def test_ac12_undo_restores_exact_bytes() -> None:
    old = b"a\r\nb"
    a = api_with(old)
    res = append(a, DEVLOG)
    assert res.record is not None
    assert writer.undo(a, res.record) == writer.UNDO_OK
    assert sha(a.get(P) or b"") == sha(old)
    opened = a.opens[-1]
    assert opened.share == 0 and opened.access & FILE_WRITE_DATA   # 取り消しだけが排他・切り詰めの権限で開く


def test_ac12_undo_refused_after_change() -> None:
    a = api_with(b"a\n")
    res = append(a)
    assert res.record is not None
    a.files[norm(P)] += b"x"
    snapshot = a.get(P)
    assert writer.undo(a, res.record) == writer.UNDO_CHANGED and a.get(P) == snapshot
    # 大きさが同じでも中身が違えば取り消さない
    b = api_with(b"a\n")
    res2 = append(b)
    assert res2.record is not None
    data = bytearray(b.get(P) or b"")
    data[-2] = ord("X")
    b.files[norm(P)] = data
    assert writer.undo(b, res2.record) == writer.UNDO_CHANGED and b.get(P) == bytes(data)


def test_undo_new_file_keeps_header() -> None:
    a = api_with(None)
    res = append(a, DEVLOG, job(line=HEAD))
    assert res.record is not None
    assert writer.undo(a, res.record) == writer.UNDO_OK
    assert a.get(P) == "# 2026-09-26 開発ログ\n".encode()   # ファイルは消さない(INV-7)


def test_undo_busy_when_open_elsewhere() -> None:
    a = api_with(b"a\n")
    res = append(a)
    assert res.record is not None
    a.locked.add(norm(P))
    assert writer.undo(a, res.record) == writer.UNDO_BUSY
    a.locked.clear()
    a.files.pop(norm(P))
    assert writer.undo(a, res.record) == writer.UNDO_CHANGED


# ------------------------------------------------------------------ J-19
def test_verify() -> None:
    a = api_with(b"a\n")
    append(a)
    assert writer.verify(a, P, "- 14:05 メモ") == writer.VERIFY_FOUND
    a.put(P, "- 14:05 メモ\n(総括の後ろに移った)\n".encode())
    assert writer.verify(a, P, "- 14:05 メモ") == writer.VERIFY_FOUND   # 位置が動いても見つかる
    a.put(P, b"a\n")
    assert writer.verify(a, P, "- 14:05 メモ") == writer.VERIFY_MISSING
    a.put(P, b"x" * (writer.VERIFY_MAX_BYTES + 1))
    assert writer.verify(a, P, "- 14:05 メモ") == writer.VERIFY_SKIPPED
    a.locked.add(norm(P))
    assert writer.verify(a, P, "x") == writer.VERIFY_SKIPPED
    assert writer.verify(a, D + "\\gone.md", "x") == writer.VERIFY_MISSING


# ------------------------------------------------------------------ ワーカー(FR-8・§10)
def test_worker_keeps_order_and_stop_returns_left() -> None:
    import logging
    import threading

    a = api_with(b"")
    gate = threading.Event()
    w = writer.Worker(a, writer.Framing, logging.getLogger("t"), threaded=True)
    w.start()
    done: list[object] = []
    w.submit(writer.Task("call", fn=lambda: gate.wait(5)))
    for i in range(3):
        w.submit(writer.Task("write", job=job(line=f"- {i}", jid=str(i)), done=done.append))
    gate.set()
    import time

    t0 = time.monotonic()
    while len(done) < 3 and time.monotonic() - t0 < 5:
        time.sleep(0.01)
    assert a.get(P) == b"- 0\n- 1\n- 2\n"
    gate2 = threading.Event()
    w.submit(writer.Task("call", fn=lambda: gate2.wait(0.3)))
    w.submit(writer.Task("write", job=job(jid="late")))
    left = w.stop(2.0)
    assert [t.job.id for t in left if t.job is not None] == ["late"]
