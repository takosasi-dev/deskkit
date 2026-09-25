# 自己検査(FR-23)。偽 Win32Api で policy・monitor・writer・保持上限・定型文展開(v0.2 の入力欄・変換・短命記録も)を通し、
# 実 DPAPI で目印文字列を一時フォルダの DB に暗号化保存した後、DB・ログ・ops.jsonl のバイト列(UTF-8 / UTF-16LE)に
# 目印が無いことを確かめる。実機のクリップボード・%LOCALAPPDATA%\DeskKit には触れない。run() は 0=合格 / 1=不合格。
from __future__ import annotations

import logging
import secrets
import tempfile
from collections.abc import Callable
from pathlib import Path

from deskkit.modules.clipshelf import config as cfgmod
from deskkit.modules.clipshelf import policy, snippets, transforms
from deskkit.modules.clipshelf._win32 import (
    FMT_CAN_INCLUDE_HISTORY,
    FMT_CAN_UPLOAD_CLOUD,
    FMT_EXCLUDE_MONITOR,
    FMT_ORIGIN,
)
from deskkit.modules.clipshelf.crypto import Cipher
from deskkit.modules.clipshelf.fakes import FakeCipher, FakeClock, FakeWin32
from deskkit.modules.clipshelf.monitor import ClipMonitor
from deskkit.modules.clipshelf.ops import OpsLog
from deskkit.modules.clipshelf.store import Store
from deskkit.modules.clipshelf.writer import ClipWriter, PlainResult, encode_origin


class _Result:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, name: str, ok: bool) -> None:
        print(f"  [{'OK' if ok else 'NG'}] {name}")
        if not ok:
            self.failed += 1


def _logger(path: Path) -> tuple[logging.Logger, logging.Handler]:
    log = logging.getLogger(f"deskkit.clipshelf.selftest.{secrets.token_hex(4)}")
    log.setLevel(logging.DEBUG)
    log.propagate = False
    h = logging.FileHandler(path, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    log.addHandler(h)
    return log, h


def _close_logs() -> None:
    for lg in list(logging.Logger.manager.loggerDict.values()):
        if isinstance(lg, logging.Logger) and lg.name.startswith("deskkit.clipshelf.selftest."):
            for h in list(lg.handlers):
                h.flush()
                h.close()
                lg.removeHandler(h)


def _env(tmp: Path, cipher: Cipher, mode: str = "record", **over: object) -> tuple[FakeWin32, ClipMonitor, Store, OpsLog,
                                                                                  FakeClock, Callable[[], cfgmod.Config]]:
    api = FakeWin32()
    clock = FakeClock()
    sec, _ = cfgmod.merge_defaults({"mode": mode, **over})
    cfg = cfgmod.parse(sec)
    store = Store(tmp / "clipshelf.db", cipher, clock)
    store.open()
    ops = OpsLog(tmp / "ops.jsonl", clock)
    log, _ = _logger(tmp / "deskkit.log")
    mon = ClipMonitor(api, owner_hwnd=lambda: 0x99, config=lambda: cfg, store=lambda: store, paused=lambda: False,
                      log=log, ops=ops, now=clock, sleep=lambda _s: None)
    mon.mark_startup()
    return api, mon, store, ops, clock, lambda: cfg


def _fake_checks(r: _Result) -> None:
    print("偽 Win32Api による検査")
    pc = policy.PolicyConfig(frozenset({"blocked.exe"}), "skip")
    base = frozenset({"CF_UNICODETEXT"})
    cases = [
        (policy.ClipFacts(base, "editor.exe"), False, policy.RECORD),
        (policy.ClipFacts(base | {FMT_ORIGIN}, "editor.exe"), False, policy.SELF_ORIGIN),
        (policy.ClipFacts(base | {FMT_EXCLUDE_MONITOR}, "editor.exe"), False, policy.EXCLUDE_FORMAT),
        (policy.ClipFacts(base | {FMT_CAN_INCLUDE_HISTORY}, "editor.exe"), False, policy.HISTORY_DISALLOWED),
        (policy.ClipFacts(base, "blocked.exe"), False, policy.EXCLUDED_APP),
        (policy.ClipFacts(base, None), False, policy.OWNER_UNKNOWN),
        (policy.ClipFacts(frozenset({"CF_DIB"}), "editor.exe"), False, policy.NOT_TEXT),
        (policy.ClipFacts(base, "editor.exe"), True, policy.PAUSED),
    ]
    r.check("policy.decide の理由コード", all(policy.decide(f, pc, p) == want for f, p, want in cases))

    with tempfile.TemporaryDirectory(prefix="clipshelf-selftest-") as td:
        tmp = Path(td)
        api, mon, store, ops, clock, _cfg = _env(tmp, FakeCipher(), exclude_exes=["blocked.exe"],
                                                 retention={"max_items": 5, "max_days": 30})
        # 除外は本文を読まない(INV-3)
        excluded = [
            {FMT_EXCLUDE_MONITOR: b"\x00\x00\x00\x00"},
            {FMT_CAN_INCLUDE_HISTORY: b"\x00\x00\x00\x00"},
        ]
        for fm in excluded:
            api.put("秘密", formats=fm)
            mon.process()
        api.put("秘密", owner_exe="C:\\x\\blocked.exe")
        mon.process()
        api.put("秘密", owner_exe=None)
        mon.process()
        r.check("除外時に CF_UNICODETEXT を読まない", api.text_reads == 0 and store.row_count() == 0)
        # 記録・重複・保持上限
        for i in range(8):
            clock.advance(minutes=1)
            api.put(f"item {i}")
            mon.process()
        api.put("item 7")
        dup = mon.process()
        r.check("重複は新規行を作らない", dup is not None and dup.reason == policy.DUPLICATE)
        r.check("保持上限で件数が上限以下", len(store.history()) == 5
                and any(o.get("op") == "retention_trim" for o in ops.read()))
        # 自己印
        first = store.history()[0]
        api.put("item x", formats={FMT_ORIGIN: encode_origin(first.id)})
        rows = store.row_count()
        d = mon.process()
        r.check("自己印は last_used_at の更新だけ", d is not None and d.reason == policy.SELF_ORIGIN and store.row_count() == rows)
        # writer: 自己印と除外形式の引き継ぎ
        w = ClipWriter(api, lambda: 0x99, lambda: 3, sleep=lambda _s: None)
        api.put("pw", formats={FMT_EXCLUDE_MONITOR: b"\x01\x00\x00\x00", "HTML Format": b"<b>pw</b>",
                               FMT_CAN_UPLOAD_CLOUD: b"\x00\x00\x00\x00"})
        pr = w.plain_text()
        names = api.format_names_now()
        r.check("書式なし化で HTML が消え、除外形式と自己印が残る",
                pr == PlainResult.OK and "HTML Format" not in names and FMT_EXCLUDE_MONITOR in names
                and FMT_CAN_UPLOAD_CLOUD in names and FMT_ORIGIN in names and api.text_now() == "pw")
        store.close()
        _close_logs()
    # 定型文展開
    clock2 = FakeClock()
    exp = snippets.expand("{date} {time} {clipboard} {{x}} {unknown}", clock2(), "CLIP")
    r.check("定型文の展開", exp.text == "2026-10-01 10:00 CLIP {x} {unknown}" and exp.unknown == ["unknown"])
    tpl = "{input:宛名=山田}様 {select:至急|通常} {input:宛名}"
    got = snippets.expand(tpl, clock2(), None, values={"input:宛名": "佐藤{date}"}).text
    r.check("定型文の入力欄・選択欄(値は1回だけ置換、無い欄は既定値)",
            got == "佐藤{date}様 至急 佐藤{date}" and [f.label for f in snippets.fields(tpl)] == ["宛名", "選択 1"])
    r.check("変換して貼り付けの変換", [transforms.apply(t, " Ａb\n c ") for t in transforms.IDS]
            == ["Ａb\n c", " Ａb c ", " Ab\n c ", " ＡB\n C ", " ａb\n c ", " Ａb"])
    _v02_store_checks(r)


def _v02_store_checks(r: _Result) -> None:
    """短命記録(C3)の期限切れ削除と、全消去が復号できない履歴の行も消すこと。"""
    with tempfile.TemporaryDirectory(prefix="clipshelf-selftest-") as td:
        tmp = Path(td)
        api, mon, store, ops, clock, _cfg = _env(tmp, FakeCipher(), short_lived={"exes": ["vault.exe"], "minutes": 5})
        api.put("短命", owner_exe="C:\\x\\vault.exe")
        short = mon.process()
        api.put("普通")
        normal = mon.process()
        clock.advance(minutes=5)
        n = store.expire()
        r.check("短命記録は期限で消え、他は残る",
                n == 1 and short is not None and store.get(short.item_id or 0) is None
                and normal is not None and store.get(normal.item_id or 0) is not None)
        store._db().execute("INSERT INTO items (kind, created_at, last_used_at, pinned, payload) VALUES "
                            "('history', '2026-01-01T00:00:00+09:00', '2026-01-01T00:00:00+09:00', 1, x'00')")
        store.close()
        store.open()
        cleared = store.clear_all(include_pins=False)
        r.check("全消去で復号できない履歴の行も消える", cleared == 2 and store.undecryptable == 0 and store.row_count() == 0)
        store.close()
        _close_logs()


def _dpapi_canary(r: _Result) -> None:
    print("実 DPAPI による目印検査")
    from deskkit.modules.clipshelf._win32 import RealWin32
    from deskkit.modules.clipshelf.crypto import DpapiCipher

    canary = "CLIPSHELF-CANARY-" + secrets.token_hex(8)
    with tempfile.TemporaryDirectory(prefix="clipshelf-selftest-") as td:
        tmp = Path(td)
        cipher = DpapiCipher(RealWin32())
        api, mon, store, ops, clock, _cfg = _env(tmp, cipher, retention={"max_items": 1, "max_days": 30})
        api.put("先に消える項目")
        mon.process()
        clock.advance(minutes=1)
        api.put(f"前置き {canary} 後置き")
        d = mon.process()  # 保持上限で古い方が消え、ops.jsonl に retention_trim が出る
        store.add_snippet(f"名前 {canary}", f"本文 {canary} {{date}}")
        store.close()
        # 読み直して復号できること(実際に保存されたことの確認)
        st2 = Store(tmp / "clipshelf.db", cipher, clock)
        st2.open()
        found = any(canary in i.text for i in st2.items()) and st2.undecryptable == 0
        st2.close()
        _close_logs()
        r.check("目印が記録され、復号できる", d is not None and d.reason == policy.RECORDED and found)
        needles = [canary.encode("utf-8"), canary.encode("utf-16-le")]
        files = [p for p in tmp.rglob("*") if p.is_file()]
        hits = [p.name for p in files if any(n in p.read_bytes() for n in needles)]
        names = sorted(p.name for p in files)
        r.check(f"DB・ログ・ops.jsonl に目印が無い(検査 {len(files)} ファイル: {', '.join(names)})",
                not hits and {"clipshelf.db", "deskkit.log", "ops.jsonl"} <= set(names))
        log_text = (tmp / "deskkit.log").read_text(encoding="utf-8")
        r.check("ログに理由コードが出ている", "reason=recorded" in log_text)


def run() -> int:
    r = _Result()
    print("ClipShelf 自己検査")
    try:
        _fake_checks(r)
        _dpapi_canary(r)
    except Exception as e:  # noqa: BLE001 - 自己検査は例外も不合格として返す
        print(f"  [NG] 例外: {type(e).__name__}")
        r.failed += 1
    print("合格" if r.failed == 0 else f"不合格({r.failed} 件)")
    return 0 if r.failed == 0 else 1
