# JotDrop の自己検査。偽の Win32(メモリの中のファイル)で、追記の権限・改行の形・区切り・文字コード・ネットワーク・取り消し・照合と、
# 書式とファイル名の検証を確かめる。実機のファイル・%LOCALAPPDATA%\DeskKit には触れない。run() は 0=合格 / 1=不合格。
from __future__ import annotations

import hashlib
from datetime import datetime

from deskkit.modules.jotdrop import compose, target, writer
from deskkit.modules.jotdrop._win32 import FILE_APPEND_DATA, FILE_WRITE_DATA, GENERIC_WRITE
from deskkit.modules.jotdrop.fakes import FakeWin32, norm

DIR = "C:\\Notes"
WHEN = datetime(2026, 9, 26, 14, 5)


class _Result:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, name: str, ok: bool) -> None:
        print(f"  [{'OK' if ok else 'NG'}] {name}")
        if not ok:
            self.failed += 1


def _job(path: str, text: str = "メモ") -> writer.Job:
    return writer.Job("j1", WHEN, path, compose.expand("- {time} {text}", WHEN, text))


def _no_sleep(_s: float) -> bool:
    return False


def _append(r: _Result) -> None:
    print("追記(INV-1・J-8・J-11・J-12)")
    api = FakeWin32()
    path = DIR + "\\a.md"
    old = b"one\r\ntwo\nthree\r\nlast"
    api.put(path, old)
    res = writer.append_line(api, _job(path), writer.Framing(), sleep=_no_sleep)
    data = api.get(path) or b""
    r.check("書く前の内容は1バイトも変わらない", hashlib.sha256(data[:len(old)]).digest() == hashlib.sha256(old).digest())
    r.check("最後の改行の形(CRLF)で改行+1行+改行", data[len(old):] == "\r\n- 14:05 メモ\r\n".encode())
    acc = [o.access for o in api.opens if o.path == path]
    r.check("追記の権限は FILE_APPEND_DATA だけ(FILE_WRITE_DATA・GENERIC_WRITE なし)",
            res.ok and bool(acc) and all(a & FILE_APPEND_DATA and not a & (FILE_WRITE_DATA | GENERIC_WRITE) for a in acc))
    log = DIR + "\\log.md"
    api.put(log, "### x\n本文\n".encode())
    fr = writer.Framing(blank_line_before=True, separator="---", header="# {date} 開発ログ")
    writer.append_line(api, writer.Job("j2", WHEN, log, "### [14:05] 見出し"), fr, sleep=_no_sleep)
    r.check("区切りは「空行・---・空行・見出し・改行」", (api.get(log) or b"").endswith("本文\n\n---\n\n### [14:05] 見出し\n".encode()))
    new = DIR + "\\new.md"
    writer.append_line(api, writer.Job("j3", WHEN, new, "### [14:05] 見出し"), fr, sleep=_no_sleep)
    r.check("新しいファイルは「ヘッダー・改行・空行・見出し・改行」で区切りなし",
            api.get(new) == "# 2026-09-26 開発ログ\n\n### [14:05] 見出し\n".encode())


def _refuse(r: _Result) -> None:
    print("書かない場合(J-9・J-10・J-8)")
    api = FakeWin32()
    sj = DIR + "\\sjis.txt"
    api.put(sj, "日本語".encode("cp932"))
    r.check("Shift_JIS は not_utf8", writer.append_line(api, _job(sj), writer.Framing(), sleep=_no_sleep).reason
            == target.REASON_NOT_UTF8 and api.get(sj) == "日本語".encode("cp932"))
    u16 = DIR + "\\u16.md"
    api.put(u16, b"\xff\xfea\x00")
    r.check("UTF-16 の BOM は not_utf8", writer.append_line(api, _job(u16), writer.Framing(), sleep=_no_sleep).reason
            == target.REASON_NOT_UTF8)
    api.remote.add("Z:\\")
    api.add_dir("Z:\\n")
    r.check("ネットワークのドライブと UNC は network",
            writer.append_line(api, _job("Z:\\n\\a.md"), writer.Framing(), sleep=_no_sleep).reason == target.REASON_NETWORK
            and writer.append_line(api, _job("\\\\server\\share\\a.md"), writer.Framing(), sleep=_no_sleep).reason
            == target.REASON_NETWORK)
    busy = DIR + "\\busy.md"
    api.put(busy, b"x\n")
    api.locked.add(norm(busy))
    res = writer.append_line(api, _job(busy), writer.Framing(), sleep=_no_sleep)
    r.check("使用中は 5 回試して busy", res.reason == target.REASON_BUSY and res.retries == 4 and api.get(busy) == b"x\n")
    r.check("フォルダが無ければ folder_missing(作らない)",
            writer.append_line(api, _job("C:\\Nope\\a.md"), writer.Framing(), sleep=_no_sleep).reason
            == target.REASON_FOLDER_MISSING and not api.is_dir("C:\\Nope"))


def _undo_verify(r: _Result) -> None:
    print("取り消しと照合(J-18・J-19)")
    api = FakeWin32()
    path = DIR + "\\u.md"
    old = b"keep\n"
    api.put(path, old)
    res = writer.append_line(api, _job(path), writer.Framing(), sleep=_no_sleep)
    r.check("照合: 足した行が見つかる", writer.verify(api, path, _job(path).line) == writer.VERIFY_FOUND)
    r.check("取り消すと書く前と同じ", res.record is not None and writer.undo(api, res.record) == writer.UNDO_OK
            and api.get(path) == old)
    res2 = writer.append_line(api, _job(path), writer.Framing(), sleep=_no_sleep)
    api.files[norm(path)] += b"added later\n"
    before = api.get(path)
    r.check("そのあと変わったファイルは取り消さない", res2.record is not None
            and writer.undo(api, res2.record) == writer.UNDO_CHANGED and api.get(path) == before)
    api.put(path, b"keep\n")
    r.check("照合: 消された行は missing", writer.verify(api, path, _job(path).line) == writer.VERIFY_MISSING)


def _formats(r: _Result) -> None:
    print("書式とファイル名(J-13・§9)")
    bad = ["- {time}", "{text} {text}", "{name} {text}", "a\n{text}"]
    r.check("{text} なし・2つ・知らない変数・改行は保存しない", all(compose.validate_line_format(f) for f in bad))
    r.check("{date:YYYY年MM月DD日} {time:HH時mm分}",
            compose.expand("{date:YYYY年MM月DD日} {time:HH時mm分} {text}", WHEN, "x") == "2026年09月26日 14時05分 x")
    r.check("{{ }} は括弧", compose.expand("{{{text}}}", WHEN, "x") == "{x}")
    pats = ["a:b.md", "CON.md", "x.", "{date}.json"]
    r.check(": ・CON.md・. で終わる・.json は保存しない", all(compose.validate_pattern(p, WHEN) for p in pats))
    r.check("23:59:59 と翌日 0:00:01 でファイルが替わる",
            target.resolve(DIR, "{date}.md", datetime(2026, 9, 26, 23, 59, 59)).endswith("2026-09-26.md")
            and target.resolve(DIR, "{date}.md", datetime(2026, 9, 27, 0, 0, 1)).endswith("2026-09-27.md"))
    r.check("本文の改行は空白・制御文字は除く・前後の空白は除く",
            compose.clean_text("  a\r\nb\u2028c\x07\td  ", 100) == "a b c\td")


def run() -> int:
    print("JotDrop 自己検査")
    r = _Result()
    _append(r)
    _refuse(r)
    _undo_verify(r)
    _formats(r)
    print("合格" if r.failed == 0 else f"不合格({r.failed} 件)")
    return 0 if r.failed == 0 else 1
