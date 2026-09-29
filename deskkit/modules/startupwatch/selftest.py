# StartupWatch の自己検査。偽の Win32(fakes.FakeApi)で、8か所の読み取り・初回は覚えるだけ・増えたら1回だけ知らせる・
# 合図のまとめ(FR-3)・RunOnce と DeskKit 自身は知らせない・記録に名前とコマンドが残らない・stop で全部閉じる、を確かめる。
# 本物のレジストリ・スタートアップ フォルダ・%LOCALAPPDATA%\DeskKit には触れない。run() は 0=合格 / 1=不合格。
from __future__ import annotations

import copy
import logging
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from deskkit.modules.startupwatch import cmdline, sources
from deskkit.modules.startupwatch.fakes import FakeApi, Signal, sample_api

SECRET_NAME = "SelftestSecretApp"
SECRET_CMD = r"C:\Users\sample\AppData\Local\SelftestSecretApp\secret-app.exe --tray"


class _Ctx:
    def __init__(self, data_dir: Path) -> None:
        self.name = "startupwatch"
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.log = logging.getLogger(f"deskkit.startupwatch.selftest.{id(self)}")
        self.log.propagate = False
        self.log.addHandler(logging.NullHandler())
        self.notes: list[tuple[str, str]] = []
        self.snoozed = False
        self._section: dict[str, Any] = {"enabled": True}

    def settings_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._section)

    def write_settings(self, section: dict[str, Any], *, restart: bool = False) -> None:
        self._section = {**section, "enabled": True}

    def is_snoozed(self) -> bool:
        return self.snoozed

    def notify(self, title: str, text: str, on_click: Callable[[], None] | None = None, *, level: str = "info") -> None:
        self.notes.append((title, text))

    def call_soon(self, fn: Callable[[], None]) -> None:
        fn()


class _Result:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, name: str, ok: bool) -> None:
        print(f"  [{'OK' if ok else 'NG'}] {name}")
        if not ok:
            self.failed += 1


def _module(tmp: Path, api: FakeApi, name: str) -> tuple[Any, _Ctx]:
    from deskkit.modules.startupwatch.module import StartupWatchModule

    ctx = _Ctx(tmp / name)
    m = StartupWatchModule(ctx, api=api, threaded=False, clock=api.clock, startfile=lambda _t: None,
                           executable=r"C:\Tools\DeskKit\DeskKit.exe")
    return m, ctx


def _cmdline(r: _Result) -> None:
    print("コマンドの行からプログラムの名前(AC-11)")
    env = {"ProgramFiles": r"C:\Program Files"}

    def expand(s: str) -> str:
        return s.replace("%ProgramFiles%", env["ProgramFiles"])

    r.check('"C:\\A B\\x.exe" --y → x.exe', cmdline.exe_name('"C:\\A B\\x.exe" --y') == "x.exe")
    r.check("C:\\A\\x.exe /s → x.exe", cmdline.exe_name("C:\\A\\x.exe /s") == "x.exe")
    r.check("rundll32.exe a.dll,Run → rundll32.exe", cmdline.exe_name("rundll32.exe a.dll,Run") == "rundll32.exe")
    r.check("%ProgramFiles%\\A\\x.exe → x.exe", cmdline.exe_name("%ProgramFiles%\\A\\x.exe", expand) == "x.exe")


def _flow(r: _Result, tmp: Path) -> None:
    print("初回は覚えるだけ・増えたら1回だけ知らせる(AC-1・AC-2・AC-3)")
    api = sample_api()
    m, ctx = _module(tmp, api, "a")
    m.start()
    m.stop()
    r.check("初回は notify を呼ばない", ctx.notes == [])
    ev = (ctx.data_dir / "events.jsonl").read_text(encoding="utf-8")
    r.check("events.jsonl に baseline が1行", ev.count('"baseline"') == 1)

    api2 = sample_api()
    api2.script = [Signal(1.0 + 0.5 * i, "hkcu_run", (lambda: api2.add_value("hkcu_run", SECRET_NAME, SECRET_CMD)) if i == 0 else None)
                   for i in range(5)]
    m2 = _module_reuse(tmp, api2, ctx)
    reads_before = m2.scans
    m2.start()
    m2.stop()
    r.check("合図を 0.5 秒おきに5回: 読み直しは1回(起動時と合わせて2回)", m2.scans - reads_before == 2)
    r.check("notify は1回", len(ctx.notes) == 1)
    r.check("文面に名前とコマンドが無い", all(SECRET_NAME not in t and "secret-app" not in t for t in ctx.notes[0]))
    r.check("新しい物が1件", m2.new_count() == 1)
    blob = "".join(p.read_text(encoding="utf-8") for p in ctx.data_dir.iterdir() if p.is_file())
    r.check("known.json・events.jsonl に名前・コマンドが無い", SECRET_NAME.lower() not in blob.lower() and "secret-app" not in blob)
    r.check("diagnostics に名前・コマンドが無い",
            all(SECRET_NAME.lower() not in str(v).lower() and "secret" not in str(k) for k, v in m2.diagnostics().items()))


def _module_reuse(tmp: Path, api: FakeApi, ctx: _Ctx) -> Any:
    from deskkit.modules.startupwatch.module import StartupWatchModule

    return StartupWatchModule(ctx, api=api, threaded=False, clock=api.clock, startfile=lambda _t: None,
                              executable=r"C:\Tools\DeskKit\DeskKit.exe")


def _quiet_kinds(r: _Result, tmp: Path) -> None:
    print("RunOnce と DeskKit 自身は知らせない(AC-5・AC-6)")
    api = sample_api()
    m, ctx = _module(tmp, api, "b")
    m.start()
    m.stop()
    api.add_value("hkcu_runonce", "SetupFinish", r"C:\Temp\setup.exe /finish")
    api.add_value("hkcu_run", "DeskKit", '"c:\\tools\\deskkit\\DESKKIT.EXE" --autostart')
    m2 = _module_reuse(tmp, api, ctx)
    m2.start()
    m2.stop()
    r.check("RunOnce(notify_runonce=false)と DeskKit は知らせない", ctx.notes == [])
    dk = [v for v in m2.view_items() if v.item.name == "DeskKit"]
    r.check("DeskKit の印が付く", bool(dk) and "DeskKit" in dk[0].marks)


def _views(r: _Result) -> None:
    print("64 ビットと 32 ビットの見え方・開けない場所(AC-8・AC-9)")
    api = sample_api()
    api.set_values("hklm_run64", [("Only64", r"C:\A\sixty.exe")])
    api.set_values("hklm_run32", [("Only32", r"C:\A\thirty.exe")])
    res = sources.read_all(api, set())
    assert res is not None
    n64 = [it.name for it in res.locs["hklm_run64"].items]
    n32 = [it.name for it in res.locs["hklm_run32"].items]
    r.check("hklm_run64 と hklm_run32 に分かれて出る", n64 == ["Only64"] and n32 == ["Only32"])
    from deskkit.modules.startupwatch.fakes import reg_key_of

    api.reg_errors[reg_key_of("hklm_run64")] = 5
    res = sources.read_all(api, set())
    assert res is not None
    r.check("5 が返る場所は unreadable、ほかは読める",
            res.locs["hklm_run64"].status == "unreadable" and all(v.status == "ok" for k, v in res.locs.items() if k != "hklm_run64"))


def _closed(r: _Result, tmp: Path) -> None:
    print("stop で全部閉じる(AC-13)")
    api = sample_api()
    m, _ctx = _module(tmp, api, "c")
    m.start()
    m.stop()
    r.check("鍵・知らせ・event が全部閉じている", not api.open)


def run() -> int:
    print("StartupWatch 自己検査")
    r = _Result()
    _cmdline(r)
    _views(r)
    with tempfile.TemporaryDirectory(prefix="startupwatch-selftest-") as d:
        _flow(r, Path(d))
        _quiet_kinds(r, Path(d))
        _closed(r, Path(d))
    print("合格" if r.failed == 0 else f"不合格({r.failed} 件)")
    return 0 if r.failed == 0 else 1
