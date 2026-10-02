# PlugSave の自己検査。一時フォルダの中にコピー元と偽のドライブ(fakes.FakeDriveApi)を作り、登録 → 初回の計画 → 始める →
# 書き換えて2回目(古い版が _以前の版 へ)→ WM_DEVICECHANGE の構造体の読み取り、を確かめる。コピー元が変わらないこと、
# ops.jsonl・ログ・通知・状態の行にファイル名・ラベル・PC の名前が出ないことも見る。本物のドライブと %LOCALAPPDATA%\DeskKit には触れない。
from __future__ import annotations

import copy
import ctypes
import hashlib
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

SECRET_FILE = "selftest-secret-report.txt"
SECRET_LABEL = "SELFTESTLABEL"
SECRET_PC = "SELFTESTPC"


class _Timer:
    def __init__(self, cb: Callable[[], None]) -> None:
        self.cb = cb
        self.active = True

    def stop(self) -> None:
        self.active = False


class _Ctx:
    def __init__(self, data_dir: Path) -> None:
        self.name = "plugsave"
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.log = logging.getLogger(f"deskkit.plugsave.selftest.{id(self)}")
        self.log.propagate = False
        self.records: list[str] = []
        records = self.records

        class _H(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record.getMessage())

        self.log.addHandler(_H())
        self.log.setLevel(logging.DEBUG)
        self.notes: list[str] = []
        self.status: list[str] = []
        self.native: dict[int, Callable[[int, int], None]] = {}
        self._section: dict[str, Any] = {"enabled": True}

    def settings_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._section)

    def write_settings(self, section: dict[str, Any], *, restart: bool = False) -> None:
        self._section = {**copy.deepcopy(section), "enabled": True}

    def is_snoozed(self) -> bool:
        return False

    def foreground(self) -> Any:
        return SimpleNamespace(is_game=False, is_fullscreen=False)

    def notify(self, title: str, text: str, on_click: Callable[[], None] | None = None, *, level: str = "info") -> None:
        self.notes.append(f"{title} {text}")

    def call_soon(self, fn: Callable[[], None]) -> None:
        fn()

    def on_native(self, msg: int, handler: Callable[[int, int], None]) -> None:
        self.native[msg] = handler

    def add_tray_action(self, label: str, cb: Callable[[], None], **_kw: Any) -> Any:
        return None

    def set_tray_status(self, text: str) -> None:
        self.status.append(text)

    def start_timer(self, ms: int, cb: Callable[[], None], *, single_shot: bool = False) -> _Timer:
        return _Timer(cb)

    def show_page(self) -> None:
        pass


def _hashes(root: Path) -> dict[str, tuple[str, int, int]]:
    out: dict[str, tuple[str, int, int]] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p)] = (hashlib.sha256(p.read_bytes()).hexdigest(), st.st_size, st.st_mtime_ns)
    return out


def _check(ok: bool, what: str, fails: list[str]) -> None:
    print(("  OK  " if ok else "  NG  ") + what)
    if not ok:
        fails.append(what)


def run() -> int:
    from deskkit.modules.plugsave import device, drives
    from deskkit.modules.plugsave.fakes import FakeDriveApi
    from deskkit.modules.plugsave.module import PlugSaveModule

    print("PlugSave 自己検査")
    fails: list[str] = []
    with tempfile.TemporaryDirectory(prefix="plugsave-selftest-") as tmp:
        base = Path(tmp)
        src = base / "src" / "書類"
        (src / "sub").mkdir(parents=True)
        (src / SECRET_FILE).write_bytes(b"first version")
        (src / "sub" / "b.bin").write_bytes(os.urandom(3000))
        (src / "~$lock.docx").write_bytes(b"x")
        drive_root = base / "drive"
        drive_root.mkdir()
        api = FakeDriveApi()
        api.add("E", str(drive_root), label=SECRET_LABEL, serial=0x55AA)
        env = {"COMPUTERNAME": SECRET_PC, "SystemDrive": "C:"}
        ctx = _Ctx(base / "home" / "DeskKit" / "plugsave")
        m = PlugSaveModule(ctx, api=api, env=env, threaded=False, opener=lambda _p: None, probe_wait_s=0)
        m.start()
        _check(m.add_source(str(src)) is None, "コピー元を足せる", fails)
        errs: list[str | None] = []
        m.register_drive("E", done=errs.append)
        did = m.config["drives"][0]["id"] if m.config["drives"] else ""
        _check(errs == [None] and bool(did), "ドライブを登録できる(印を書く)", fails)
        _check((drive_root / drives.BACKUP_DIR / drives.MARKER).is_file(), "印のファイルがある", fails)
        m.make_preview(did)
        pv = m.previews.get(did)
        _check(pv is not None and pv.plan_new == 2 and pv.reasons.get("excluded_name") == 1, "初回の計画が作れる", fails)
        m.start_backup(did, trigger="first")
        dest = drive_root / drives.BACKUP_DIR / SECRET_PC / "書類"
        _check(m.last is not None and m.last.result == "ok" and m.last.new == 2, "初回のバックアップが終わる", fails)
        _check((dest / SECRET_FILE).read_bytes() == b"first version", "コピーした中身が同じ", fails)
        work = drive_root / drives.BACKUP_DIR / SECRET_PC / "_作業中"
        _check(not any(work.rglob("*")), "_作業中 が空", fails)
        (src / SECRET_FILE).write_bytes(b"second version!")
        before = _hashes(src.parent)
        m.start_backup(did, trigger="manual")
        olds = list((drive_root / drives.BACKUP_DIR / SECRET_PC / drives.OLD_DIR).rglob(SECRET_FILE))
        _check(m.last is not None and m.last.changed == 1 and len(olds) == 1 and olds[0].read_bytes() == b"first version",
               "変わったファイルの古い版が _以前の版 に移る", fails)
        _check((dest / SECRET_FILE).read_bytes() == b"second version!", "新しい版が最終の場所にある", fails)
        _check(_hashes(src.parent) == before, "コピー元が変わっていない", fails)
        # v0.4.1: 落ちた回(回の終わりの処理が走らない)のあとで、中身の欠けた小さいファイルを直す
        (src / "c.txt").write_bytes(b"written in a run that stopped")
        from deskkit.modules.plugsave.copier import BackupRun

        finish = BackupRun._finish_markers
        try:
            BackupRun._finish_markers = lambda self: None  # type: ignore[method-assign]
            m.start_backup(did, trigger="manual")
        finally:
            BackupRun._finish_markers = finish  # type: ignore[method-assign]
        _check(sum(1 for p in work.iterdir() if p.is_dir()) == 1, "止まった回の印が残る", fails)
        broken = dest / "c.txt"
        st = broken.stat()
        broken.write_bytes(b"\0" * st.st_size)
        os.utime(broken, ns=(st.st_atime_ns, st.st_mtime_ns))
        m.start_backup(did, trigger="manual")
        _check(m.last is not None and m.last.prev_unfinished and m.last.recopied == 1
               and broken.read_bytes() == b"written in a run that stopped" and not any(work.iterdir()),
               "止まった回のあとで、欠けた小さいファイルを直して印を片づける", fails)
        # WM_DEVICECHANGE の構造体(E: と F:)
        vol = device.DEV_BROADCAST_VOLUME()
        vol.dbcv_size = ctypes.sizeof(vol)
        vol.dbcv_devicetype = device.DBT_DEVTYP_VOLUME
        vol.dbcv_unitmask = (1 << 4) | (1 << 5)
        ev = device.parse(device.DBT_DEVICEARRIVAL, ctypes.addressof(vol))
        _check(ev is not None and set(ev.letters) == {"E", "F"}, "DEV_BROADCAST_VOLUME から文字を読める", fails)
        _check(device.parse(device.DBT_DEVNODES_CHANGED, ctypes.addressof(vol)) is None, "ほかの wParam は無視する", fails)
        m.stop()
        text = "\n".join([*ctx.records, *ctx.notes, *ctx.status, (m.data_dir / "ops.jsonl").read_text(encoding="utf-8"),
                          repr(m.diagnostics()), repr(m.usage(7))])
        _check(all(s not in text for s in (SECRET_FILE, SECRET_LABEL, SECRET_PC, "書類")),
               "記録・通知・状態の行に名前が出ない", fails)
    print("合格" if not fails else f"不合格 {len(fails)} 件")
    return 0 if not fails else 1
