# ClipTrim のテスト共通部品: 偽 ctx・偽のショートカット API・同梱 ffmpeg の展開(無ければ skip)・testsrc の合成動画。
# 本番のデータ(%LOCALAPPDATA%\DeskKit)・本物の「送る」フォルダ・利用者の動画には触れない(Qt は offscreen)。
from __future__ import annotations

import copy
import logging
import os
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DESKKIT_HOME", str(tmp_path / "home"))


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def text(self) -> str:
        out = []
        for r in self.records:
            out.append(r.getMessage())
            if r.exc_info:
                out.append(logging.Formatter().formatException(r.exc_info))
        return "\n".join(out)


@pytest.fixture
def logs() -> Iterator[ListHandler]:
    h = ListHandler()
    lg = logging.getLogger("deskkit.cliptrim")
    lg.addHandler(h)
    old = lg.level
    lg.setLevel(logging.DEBUG)
    yield h
    lg.removeHandler(h)
    lg.setLevel(old)


# ------------------------------------------------------------------ 偽 ctx
class FakeCtx:
    def __init__(self, data_dir: Path, section: dict[str, Any] | None = None) -> None:
        self.name = "cliptrim"
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.log = logging.getLogger("deskkit.cliptrim")
        self._section: dict[str, Any] = {"enabled": True, **(section or {})}
        self.tray: list[tuple[str, Callable[[], None]]] = []
        self.notifications: list[tuple[str, str, str]] = []
        self.status = ""
        self.writes: list[dict[str, Any]] = []
        self.shown = 0
        self.pending: list[Callable[[], None]] = []
        self.parent: Any = None
        self.errors: list[str] = []

    def settings(self) -> dict[str, Any]:
        return dict(self._section)

    def settings_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._section)

    def write_settings(self, section: dict[str, Any], *, restart: bool = False) -> None:
        self._section = {**copy.deepcopy(section), "enabled": True}
        self.writes.append(copy.deepcopy(section))

    def add_tray_action(self, label: str, cb: Callable[[], None], **_k: Any) -> None:
        self.tray.append((label, cb))

    def set_tray_status(self, text: str) -> None:
        self.status = text

    def notify(self, title: str, text: str, on_click: Callable[[], None] | None = None, *, level: str = "info",
               replace_key: str | None = None) -> None:
        self.notifications.append((title, text, level))

    def call_soon(self, fn: Callable[[], None]) -> None:
        self.pending.append(fn)  # テストでは pump() で GUI スレッド役として実行する

    def drain(self) -> None:
        while self.pending:
            fn = self.pending.pop(0)
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{type(e).__name__}: {e}")

    def show_page(self) -> None:
        self.shown += 1

    def window_parent(self) -> Any:
        return self.parent

    def is_snoozed(self) -> bool:
        return False


def pump(ctx: FakeCtx, until: Callable[[], bool], timeout: float = 60.0) -> None:
    """条件がそろうまで、裏のスレッドから積まれた処理を GUI スレッド役として流す。"""
    t0 = time.monotonic()
    while True:
        ctx.drain()
        if until():
            return
        if time.monotonic() - t0 > timeout:
            raise TimeoutError("condition not met")
        time.sleep(0.02)


class FakeLinks:
    """ShellLinkApi の偽物。ファイルに JSON で (target, args) を書く。"""

    def __init__(self) -> None:
        self.created: list[Path] = []

    def create(self, path: Path, target: str, args: str, workdir: str, icon: str, description: str) -> None:
        import json

        path.write_text(json.dumps([target, args]), encoding="utf-8")
        self.created.append(path)

    def read(self, path: Path) -> tuple[str, str] | None:
        import json

        try:
            t, a = json.loads(path.read_text(encoding="utf-8"))
            return str(t), str(a)
        except (OSError, ValueError):
            return None


@pytest.fixture(scope="session")
def qapp() -> Any:
    from PySide6.QtWidgets import QApplication

    from deskkit.ui import theme

    app = QApplication.instance() or QApplication([])
    theme.apply(app)  # type: ignore[arg-type]
    return app


# ------------------------------------------------------------------ ffmpeg(同梱物が無ければ skip)
@pytest.fixture(scope="session")
def ff_manager(tmp_path_factory: pytest.TempPathFactory) -> Any:
    from deskkit import ffmpeg as F

    bd = F.bundled_dir()
    if bd is None:
        pytest.skip("同梱の ffmpeg(deskkit/_bundled)がありません。tools/make_ffmpeg_bundle.py で作ります")
    return F.FfmpegManager(tmp_path_factory.mktemp("ffcache"), lambda: bd)


@pytest.fixture(scope="session")
def ffmpeg_exe(ff_manager: Any) -> Path:
    exe: Path = ff_manager.ensure()
    return exe


def run_ff(exe: Path, *args: str, timeout: float = 120) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([str(exe), "-hide_banner", "-nostdin", *args], capture_output=True, timeout=timeout,
                          creationflags=0x08000000, check=False)


def make_video(exe: Path, out: Path, *, seconds: float, codec: str, fps: int = 30, size: str = "320x240",
               gop: int = 60, audio: int = 1, bframes: bool = False, extra_in: list[str] | None = None,
               extra_out: list[str] | None = None, fmt: str | None = None, timeout: float = 120) -> Path:
    args: list[str] = ["-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate={fps}:duration={seconds}"]
    for i in range(audio):
        args += ["-f", "lavfi", "-i", f"sine=frequency={300 + 200 * i}:duration={seconds}"]
    args += extra_in or []
    args += ["-map", "0:v"]
    for i in range(audio):
        args += ["-map", f"{i + 1}:a"]
    if codec == "h264":
        args += ["-c:v", "h264_mf", "-g", str(gop), "-b:v", "600k"]
    elif codec == "mpeg4":
        args += ["-c:v", "mpeg4", "-g", str(gop), "-bf", "2" if bframes else "0", "-q:v", "5"]
    elif codec == "hevc":
        args += ["-c:v", "libkvazaar", "-kvazaar-params", f"period={gop},gop=8,preset=ultrafast,open-gop=0"]
    if audio:
        args += ["-c:a", "aac", "-b:a", "64k"]
    args += extra_out or []
    if fmt:
        args += ["-f", fmt]
    args.append("file:" + str(out))
    r = run_ff(exe, *args, timeout=timeout)
    if r.returncode != 0 or not out.is_file():
        raise RuntimeError("could not make sample: " + r.stderr.decode("utf-8", "replace")[-400:])
    return out


def h264_or_mpeg4(ff_manager: Any, exe: Path) -> str:
    return "h264" if ff_manager.check_h264(exe) else "mpeg4"


@pytest.fixture(scope="session")
def samples(ffmpeg_exe: Path, ff_manager: Any, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """テストの検体(testsrc)。仕様書 §4・§11 の形。"""
    d = tmp_path_factory.mktemp("samples")
    exe = ffmpeg_exe
    s: dict[str, Path] = {}
    s["h264"] = make_video(exe, d / "h264.mp4", seconds=120, codec=h264_or_mpeg4(ff_manager, exe), gop=60)
    s["hevc_mp4"] = make_video(exe, d / "hevc.mp4", seconds=30, codec="hevc", gop=64)
    s["hevc_mkv"] = make_video(exe, d / "hevc.mkv", seconds=30, codec="hevc", gop=64)
    base = make_video(exe, d / "rotbase.mov", seconds=12, codec="mpeg4", size="192x108", gop=30,
                      extra_out=["-metadata", "location=+35.6586+139.7454/"], fmt="mov")
    rot = d / "rot.mov"
    r = run_ff(exe, "-loglevel", "error", "-y", "-display_rotation", "90", "-i", "file:" + str(base), "-c", "copy",
               "-map_metadata", "0", "-f", "mov", "file:" + str(rot))
    assert r.returncode == 0
    s["rot"] = rot
    srt = d / "sub.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:03,000\nhello\n", encoding="utf-8")
    s["multi"] = make_video(exe, d / "multi.mkv", seconds=12, codec="mpeg4", gop=25, fps=25, audio=2,
                            extra_in=["-i", "file:" + str(srt)], extra_out=["-map", "3:s", "-c:s", "srt"])
    return s


@pytest.fixture
def make_module(tmp_path: Path, ff_manager: Any) -> Iterator[Callable[..., tuple[Any, FakeCtx]]]:
    made: list[Any] = []

    def factory(section: dict[str, Any] | None = None, **kw: Any) -> tuple[Any, FakeCtx]:
        from deskkit.modules.cliptrim.module import ClipTrimModule

        ctx = FakeCtx(tmp_path / f"data{len(made)}", section)
        kw.setdefault("ffmpeg_mgr", ff_manager)
        kw.setdefault("shell_link", FakeLinks())
        kw.setdefault("sendto_folder", tmp_path / "SendTo")
        kw.setdefault("fallback_dir", lambda: tmp_path / "Videos" / "ClipTrim")
        kw.setdefault("confirm", lambda _t, _x: True)
        kw.setdefault("attributes", lambda _p: 0)
        m = ClipTrimModule(ctx, **kw)
        m.start()
        made.append(m)
        return m, ctx

    yield factory
    for m in made:
        m.stop()
