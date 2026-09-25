# v0.3.0 の本体の変更のテスト: CLI 転送で 200 個のパスを運ぶ(H-8)、`<module> open` で host を起動してから渡す(H-B)、
# ライセンス表示(H-5)、ffmpeg の同梱物づくり(H-6)、テーマの 7 色(H-C)、版数(H-D)。
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- H-8
def _paths_200() -> list[str]:
    """200 個・合計ちょうど 32,000 文字(日本語・空白・記号を含む)。"""
    out = []
    for i in range(200):
        head = f"C:\\Users\\利用者\\Pictures\\旅行 2026 (夏)\\写真_{i:03d}_"
        out.append(head + "x" * (160 - len(head) - 4) + ".jpg")
    assert sum(len(p) for p in out) == 32000
    return out


_SERVER = """
import json, sys
from pathlib import Path
from PySide6.QtCore import QCoreApplication, QTimer
from deskkit import ipc
app = QCoreApplication([])
out = Path(sys.argv[2])
def handler(a):
    out.write_text(json.dumps(a, ensure_ascii=False), encoding="utf-8")
    return 0, f"queued {len(a) - 2}"
s = ipc.IpcServer(handler)
assert s.listen(sys.argv[1])
print("ready", flush=True)
QTimer.singleShot(30000, app.quit)
app.exec()
"""


def _roundtrip(tmp_path: Path, args: list[str]) -> tuple[tuple[int, str], list[str] | None]:
    """別プロセスで IPC サーバー(本物の IpcServer)を開き、このプロセスから forward する(実際の CLI と同じ形)。"""
    import json

    from deskkit import ipc

    name = f"DeskKit-test-{uuid.uuid4().hex[:8]}"
    script, got_file = tmp_path / "srv.py", tmp_path / "got.json"
    script.write_text(_SERVER, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    p = subprocess.Popen([sys.executable, str(script), name, str(got_file)], cwd=ROOT, env=env, stdout=subprocess.PIPE)
    try:
        assert p.stdout is not None and p.stdout.readline().strip() == b"ready"
        res = ipc.forward(args, 10000, name=name)
    finally:
        p.kill()
        p.wait(10)
    got = json.loads(got_file.read_text(encoding="utf-8")) if got_file.exists() else None
    return res, got


def test_cli_forward_carries_200_paths_32000_chars(qapp: Any, tmp_path: Path) -> None:
    from deskkit import ipc

    paths = _paths_200()
    assert len(paths) == ipc.MAX_FORWARD_PATHS and sum(map(len, paths)) == ipc.MAX_FORWARD_CHARS
    args = ["sendprep", "open", *paths]
    res, got = _roundtrip(tmp_path, args)
    assert res == (0, "queued 200")
    assert got == args  # 順番も中身も欠けずに届く


def test_cli_forward_rejects_oversized_request(qapp: Any, tmp_path: Path) -> None:
    from deskkit import ipc

    huge = ["x" * (ipc.MAX_REQUEST_BYTES + 10)]
    res, got = _roundtrip(tmp_path, huge)
    assert got is None  # ハンドラまで届かない
    assert res[0] != 0


def test_server_name_separates_deskkit_home(monkeypatch: pytest.MonkeyPatch) -> None:
    from deskkit import ipc

    monkeypatch.delenv("DESKKIT_HOME", raising=False)
    real = ipc.server_name()
    monkeypatch.setenv("DESKKIT_HOME", "C:\\tmp\\a")
    a = ipc.server_name()
    monkeypatch.setenv("DESKKIT_HOME", "C:\\tmp\\b")
    b = ipc.server_name()
    assert len({real, a, b}) == 3 and a.startswith(real)  # テストの host は本番の常駐に繋がらない


# ---------------------------------------------------------------- H-B
@pytest.mark.parametrize(("args", "want"), [
    (["sendprep", "open", "a.jpg"], True),
    (["sendprep", "open"], True),
    (["sendprep", "status"], False),
    (["--quit"], False),
    (["--show", "open"], False),
    (["sendprep"], False),
])
def test_is_open_command(args: list[str], want: bool) -> None:
    from deskkit import ipc

    assert ipc.is_open_command(args) is want


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def test_open_when_running_does_not_launch() -> None:
    from deskkit import ipc

    launched: list[int] = []
    res = ipc.forward_or_launch(["sendprep", "open", "a"], lambda: launched.append(1), forward_fn=lambda a: (0, "queued 1"))
    assert res == (0, "queued 1") and launched == []


def test_open_launches_host_and_waits_until_it_answers() -> None:
    from deskkit import ipc

    clock = _FakeClock()
    launched: list[float] = []
    calls: list[float] = []

    def fwd(a: list[str]) -> tuple[int, str]:
        calls.append(clock.t)
        if not launched or clock.t < 3.0:  # 起動から 3 秒で IPC が開く
            return ipc.EXIT_NOT_RUNNING, "not running"
        return 0, f"queued {len(a) - 2}"

    res = ipc.forward_or_launch(["sendprep", "open", "a", "b"], lambda: launched.append(clock.t), forward_fn=fwd,
                                clock=clock.now, sleep=clock.sleep)
    assert res == (0, "queued 2")
    assert launched == [0.0]  # 1回だけ起動する
    assert 3.0 <= calls[-1] < 3.5


def test_open_gives_up_after_15_seconds() -> None:
    from deskkit import ipc

    clock = _FakeClock()
    launched: list[int] = []
    res = ipc.forward_or_launch(["sendprep", "open", "a"], lambda: launched.append(1),
                                forward_fn=lambda a: (ipc.EXIT_NOT_RUNNING, ""), clock=clock.now, sleep=clock.sleep)
    assert res[0] == ipc.EXIT_NOT_RUNNING and "15 秒" in res[1]
    assert launched == [1] and 15.0 <= clock.t < 15.5


def test_open_reports_launch_failure() -> None:
    from deskkit import ipc

    def boom() -> None:
        raise FileNotFoundError("no exe")

    res = ipc.forward_or_launch(["sendprep", "open", "a"], boom, forward_fn=lambda a: (ipc.EXIT_NOT_RUNNING, ""))
    assert res == (ipc.EXIT_NOT_RUNNING, "DeskKit を起動できませんでした")


def test_open_passes_through_module_disabled() -> None:
    from deskkit import ipc

    res = ipc.forward_or_launch(["sendprep", "open", "a"], lambda: None,
                                forward_fn=lambda a: (11, "sendprep は無効または停止中です"))
    assert res[0] == 11


def test_launch_spec_source_run() -> None:
    from deskkit import app, paths

    cmd, cwd = app.host_launch_spec()
    assert cmd[-3:] == ["-m", "deskkit", app.LAUNCHED_FLAG]
    assert Path(cmd[0]).name.lower() in ("pythonw.exe", "python.exe")
    assert cwd == str(ROOT) == str(paths.source_root())  # pip install していないので cwd から import させる


def test_launch_spec_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    from deskkit import app, paths

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "C:\\Apps\\DeskKit.exe")
    cmd, cwd = app.host_launch_spec()
    assert cmd == ["C:\\Apps\\DeskKit.exe", app.LAUNCHED_FLAG] and cwd is None
    assert paths.child_env()["PYINSTALLER_RESET_ENVIRONMENT"] == "1"  # onefile の展開先を親と共有しない


def test_launch_host_detached_uses_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    from deskkit import app

    seen: dict[str, Any] = {}

    def fake_popen(cmd: list[str], **kw: Any) -> None:
        seen["cmd"] = cmd
        seen.update(kw)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    app.launch_host_detached()
    assert seen["cmd"][-1] == app.LAUNCHED_FLAG
    assert seen["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert seen["creationflags"] & 0x00000008  # DETACHED_PROCESS(コンソールを持たない)


def test_open_cli_for_disabled_module_notifies(qapp: Any, home: Path) -> None:
    from deskkit.host import Host

    h = Host(qapp, "per_monitor_aware_v2", start_ipc=False, show_tray=False)  # type: ignore[arg-type]
    h.start()
    notes: list[tuple[str, str]] = []
    h.notify = lambda src, title, text, cb, level="info", **kw: notes.append((title, level))  # type: ignore[method-assign]
    try:
        code, _ = h.handle_cli(["sendprep", "open", "a.jpg"])
        assert code == 11
        assert notes and "SendPrep" in notes[0][0] and notes[0][1] == "warn"
        notes.clear()
        code, _ = h.handle_cli(["sendprep", "status"])  # open 以外は今までどおり黙って 11
        assert code == 11 and notes == []
    finally:
        h.loader.stop_all()
        h.hotkeys.unregister_all()
        h.native.close_native()


@pytest.mark.win32_real
def test_open_starts_real_host_from_source(tmp_path: Path) -> None:
    """実際に `python -m deskkit sendprep open` を実行し、host が起動して受け付けることを確かめる(数秒かかる)。"""
    env = dict(os.environ, DESKKIT_HOME=str(tmp_path / "home"), QT_QPA_PLATFORM="offscreen")
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, "-m", "deskkit", "sendprep", "open", str(tmp_path / "a.jpg")], cwd=ROOT, env=env,
                       capture_output=True, timeout=60)
    elapsed = time.monotonic() - t0
    try:
        # 既定の設定では sendprep は無効なので、起動した host から 11 が返る(10 なら起動できていない)
        assert r.returncode == 11, (r.returncode, r.stdout, r.stderr)
        assert elapsed < 20
    finally:
        subprocess.run([sys.executable, "-m", "deskkit", "--quit"], cwd=ROOT, env=env, capture_output=True, timeout=30)


# ---------------------------------------------------------------- H-5
def test_third_party_licenses_file() -> None:
    text = (ROOT / "THIRD_PARTY_LICENSES.txt").read_text(encoding="utf-8-sig")
    for need in ("PySide6", "Pillow", "pi-heif", "libheif", "libde265", "NumPy", "psutil", "PyWinRT", "FFmpeg",
                 "n8.1.3-20260924", "https://ffmpeg.org/releases/ffmpeg-8.1.3.tar.xz", "https://github.com/BtbN/FFmpeg-Builds",
                 "GNU LESSER GENERAL PUBLIC LICENSE", "GNU GENERAL PUBLIC LICENSE"):
        assert need in text, need


def test_licenses_path_and_text_source_run() -> None:
    from deskkit import licenses

    p = licenses.licenses_path()
    assert p is not None and p.name == "THIRD_PARTY_LICENSES.txt"
    t = licenses.read_text()
    assert "FFmpeg" in t and not t.startswith("\ufeff")
    names = [e.name for e in licenses.summary(t)]
    assert len(names) >= 8 and any("FFmpeg" in n for n in names)


def test_licenses_path_frozen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from deskkit import licenses

    (tmp_path / "THIRD_PARTY_LICENSES.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert licenses.licenses_path() == tmp_path / "THIRD_PARTY_LICENSES.txt"


def test_license_dialog_builds(qapp: Any) -> None:
    from deskkit.ui.license_dialog import LicenseDialog

    d = LicenseDialog(None)
    assert d.viewer.toPlainText().count("GNU LESSER GENERAL PUBLIC LICENSE") >= 1
    assert d.list.count() >= 8
    d.list.setCurrentRow(1)  # 一覧から選ぶと、その節へ飛ぶ
    d.search.setText("n8.1.3")
    d.find_next()
    assert "n8.1.3" in d.viewer.textCursor().selectedText()
    d.close()


# ---------------------------------------------------------------- H-6
def _fake_btbn_zip(tmp: Path, exe_bytes: bytes) -> Path:
    src = tmp / "ffmpeg-n8.1-latest-win64-lgpl-8.1.zip"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("ffmpeg-n8.1-latest-win64-lgpl-8.1/LICENSE.txt", "LGPL v3 text")
        z.writestr("ffmpeg-n8.1-latest-win64-lgpl-8.1/bin/ffmpeg.exe", exe_bytes)
        z.writestr("ffmpeg-n8.1-latest-win64-lgpl-8.1/bin/ffprobe.exe", b"probe")
        z.writestr("ffmpeg-n8.1-latest-win64-lgpl-8.1/doc/ffmpeg.html", "doc")
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    (tmp / "checksums.sha256").write_text(f"0000  other.zip\n{digest}  {src.name}\n", encoding="utf-8")
    return src


def test_make_ffmpeg_bundle(tmp_path: Path) -> None:
    from tools import make_ffmpeg_bundle as mb

    src = _fake_btbn_zip(tmp_path, b"MZ fake ffmpeg" * 1000)
    out = tmp_path / "_bundled"
    made, digest = mb.build(src, out, licenses_file=None)
    assert made and digest == hashlib.sha256(b"MZ fake ffmpeg" * 1000).hexdigest()
    with zipfile.ZipFile(out / "ffmpeg.zip") as z:
        assert sorted(z.namelist()) == ["LICENSE.txt", "ffmpeg.exe"]
        assert all(i.compress_type == zipfile.ZIP_LZMA for i in z.infolist())
        assert z.read("ffmpeg.exe") == b"MZ fake ffmpeg" * 1000
    assert (out / "ffmpeg.sha256").read_text(encoding="ascii") == digest + "\n"
    mtime = (out / "ffmpeg.zip").stat().st_mtime_ns
    made2, digest2 = mb.build(src, out, licenses_file=None)  # 同じ版なら作り直さない
    assert not made2 and digest2 == digest and (out / "ffmpeg.zip").stat().st_mtime_ns == mtime
    src2 = _fake_btbn_zip(tmp_path, b"MZ newer ffmpeg")  # 版が変われば作り直す
    made3, digest3 = mb.build(src2, out, licenses_file=None)
    assert made3 and digest3 != digest


def test_make_ffmpeg_bundle_rejects_bad_checksum(tmp_path: Path) -> None:
    from tools import make_ffmpeg_bundle as mb

    src = _fake_btbn_zip(tmp_path, b"MZ")
    (tmp_path / "checksums.sha256").write_text(f"{'0' * 64}  {src.name}\n", encoding="utf-8")
    with pytest.raises(mb.BundleError, match="SHA-256"):
        mb.build(src, tmp_path / "_bundled", licenses_file=None)
    assert not (tmp_path / "_bundled" / "ffmpeg.zip").exists()


def test_make_ffmpeg_bundle_requires_version_in_licenses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import make_ffmpeg_bundle as mb

    src = _fake_btbn_zip(tmp_path, b"MZ")
    lic = tmp_path / "THIRD_PARTY_LICENSES.txt"
    lic.write_text("FFmpeg n8.0.0-old", encoding="utf-8")
    monkeypatch.setattr(mb, "ffmpeg_version", lambda p: "n8.1.9-20270101")
    with pytest.raises(mb.BundleError, match="n8.1.9-20270101"):
        mb.build(src, tmp_path / "_bundled", licenses_file=lic)


def test_build_ps1_keeps_bom_crlf_and_guards_ffmpeg() -> None:
    raw = (ROOT / "build.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # v0.1 で BOM が無くて文字化けした
    text = raw.decode("utf-8-sig")
    assert "ffmpeg-n8.1-latest-win64-lgpl-8.1.zip" in text and "make_ffmpeg_bundle.py" in text
    assert "exit 1" in text


def test_spec_bundles_data_and_hidden_imports() -> None:
    spec = (ROOT / "deskkit.spec").read_text(encoding="utf-8")
    for need in ("THIRD_PARTY_LICENSES.txt", "deskkit/_bundled", "pi_heif", "winrt", "psutil", "numpy"):
        assert need in spec, need
    compile(spec, "deskkit.spec", "exec")  # 構文が正しい


# ---------------------------------------------------------------- H-C / H-D
def test_theme_has_colors_for_all_modules() -> None:
    from deskkit import catalog
    from deskkit.ui import theme

    for n in catalog.MODULE_NAMES:
        assert n in theme._LIGHT_MODULE_ACCENTS, n
        assert n in theme._CHART_DARK, n
    assert len(set(theme._LIGHT_MODULE_ACCENTS.values())) == 7
    assert len(set(theme._CHART_DARK.values())) == 7
    assert len({m.accent for m in catalog.MODULES}) == 7


def test_version_is_030() -> None:
    import deskkit

    assert deskkit.__version__ == "0.3.0"
    assert 'version = "0.3.0"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "## [0.3.0] - 2026-09-25" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 例外のログに本文・パスを書かない(v0.3 VINV-4)
_SECRET = "C:/Users/secret-user/Pictures/旅行/IMG_0001.jpg"


def _capture(logger_name: str) -> tuple[Any, Any]:
    import io
    import logging

    from deskkit.logging_setup import SafeFormatter

    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(SafeFormatter("%(levelname)s %(name)s %(message)s"))
    lg = logging.getLogger(logger_name)
    lg.addHandler(h)
    return buf, lambda: lg.removeHandler(h)


def test_describe_exception_has_type_and_own_location_only() -> None:
    from deskkit import paths
    from deskkit.logging_setup import describe_exception

    try:
        paths.Path(_SECRET).read_bytes()  # 存在しないファイル → FileNotFoundError(文にパスが入る)
    except OSError as e:
        assert _SECRET.split("/")[-1] in str(e)  # 前提: 例外の文にはファイル名が入っている
        text = describe_exception(e)
    assert text.startswith("FileNotFoundError(errno=2")
    assert "secret-user" not in text and "IMG_0001" not in text and "旅行" not in text


def test_ctx_safe_logs_type_and_location_without_message(qapp: Any, home: Path) -> None:
    from deskkit.context import ModuleContextImpl
    from deskkit.host import Host

    h = Host(qapp, "per_monitor_aware_v2", start_ipc=False, show_tray=False)  # type: ignore[arg-type]
    h.start()
    buf, done = _capture("deskkit.twinsweep")
    try:
        ctx = ModuleContextImpl(h, "twinsweep")

        def boom() -> None:
            raise PermissionError(13, "アクセスが拒否されました", _SECRET)

        assert ctx.safe(boom, "scan")() is None
        out = buf.getvalue()
        assert "ハンドラ scan で例外" in out
        assert "PermissionError(errno=13" in out and "deskkit/context.py:" in out  # 型名と DeskKit のソースの位置
        assert "secret-user" not in out and "IMG_0001" not in out and "アクセスが拒否" not in out
        assert "Traceback" not in out and str(ROOT).replace("\\", "/") not in out.replace("\\", "/")
    finally:
        done()
        h.loader.stop_all()
        h.hotkeys.unregister_all()
        h.native.close_native()


def test_log_file_formatter_hides_exception_text(tmp_path: Path) -> None:
    import logging

    from deskkit import logging_setup

    logging_setup.setup(tmp_path, 1)
    lg = logging.getLogger("deskkit.pccheckup")
    try:
        raise ValueError(f"壊れた設定: {_SECRET}")
    except ValueError:
        lg.exception("読み込みに失敗")
    for name in ("deskkit", "overlaykit"):  # 後のテストに残さない(一時フォルダのファイルも閉じる)
        lgr = logging.getLogger(name)
        for hd in list(lgr.handlers):
            hd.flush()
            hd.close()
            lgr.removeHandler(hd)
    text = (tmp_path / "deskkit.log").read_text(encoding="utf-8")
    assert "読み込みに失敗" in text and "例外: ValueError" in text
    assert "secret-user" not in text and "壊れた設定" not in text


def test_loader_stop_reason_has_no_message() -> None:
    from deskkit.loader import _short

    try:
        raise OSError(2, "見つかりません", _SECRET)
    except OSError as e:
        r = _short(e)
    assert r.startswith("OSError") or r.startswith("FileNotFoundError")
    assert "secret-user" not in r and "見つかりません" not in r
