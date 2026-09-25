# SendPrep のテスト共通部品: 偽 ctx・偽のショートカット API・合成画像・同梱 ffmpeg の形の zip(開発用の配布物から作る)。
# 本番のデータ(%LOCALAPPDATA%\DeskKit)・本物の「送る」フォルダ・本物のクリップボードには触れない(Qt は offscreen)。
from __future__ import annotations

import copy
import hashlib
import io
import logging
import os
import struct
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO = Path(__file__).resolve().parents[3]
DEV_ZIP_NAME = "ffmpeg-n8.1-latest-win64-lgpl-8.1.zip"


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
            if r.exc_text:
                out.append(r.exc_text)
        return "\n".join(out)


@pytest.fixture
def logs() -> Iterator[ListHandler]:
    h = ListHandler()
    lg = logging.getLogger("deskkit.sendprep")
    lg.addHandler(h)
    old = lg.level
    lg.setLevel(logging.DEBUG)
    yield h
    lg.removeHandler(h)
    lg.setLevel(old)


# ------------------------------------------------------------------ 偽 ctx
class FakeTrayItem:
    def __init__(self, label: str, cb: Callable[[], None]) -> None:
        self.label = label
        self.cb = cb


class FakeCtx:
    def __init__(self, data_dir: Path, section: dict[str, Any] | None = None) -> None:
        self.name = "sendprep"
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.log = logging.getLogger("deskkit.sendprep")
        self._section: dict[str, Any] = {"enabled": True, **(section or {})}
        self.tray: list[FakeTrayItem] = []
        self.notifications: list[tuple[str, str, str]] = []
        self.status = ""
        self.writes: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.shown = 0
        self.pending: list[Callable[[], None]] = []
        self.parent: Any = None

    def settings(self) -> dict[str, Any]:
        return dict(self._section)

    def settings_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._section)

    def write_settings(self, section: dict[str, Any], *, restart: bool = False) -> None:
        self._section = {**copy.deepcopy(section), "enabled": True}
        self.writes.append(copy.deepcopy(section))

    def add_tray_action(self, label: str, cb: Callable[[], None], **_k: Any) -> FakeTrayItem:
        it = FakeTrayItem(label, cb)
        self.tray.append(it)
        return it

    def set_tray_status(self, text: str) -> None:
        self.status = text

    def notify(self, title: str, text: str, on_click: Callable[[], None] | None = None, *, level: str = "info") -> None:
        self.notifications.append((title, text, level))

    def safe(self, fn: Callable[..., Any], label: str | None = None) -> Callable[..., Any]:
        def w(*a: Any, **k: Any) -> Any:
            try:
                return fn(*a, **k)
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{label}: {type(e).__name__}: {e}")
                return None

        return w

    def call_soon(self, fn: Callable[[], None]) -> None:
        self.pending.append(fn)  # テストでは drain() で GUI スレッド役として実行する

    def drain(self) -> None:
        while self.pending:
            self.safe(self.pending.pop(0), "call_soon")()

    def show_page(self) -> None:
        self.shown += 1

    def window_parent(self) -> Any:
        return self.parent

    def is_snoozed(self) -> bool:
        return False


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


@pytest.fixture
def make_module(tmp_path: Path) -> Iterator[Callable[..., tuple[Any, FakeCtx]]]:
    made: list[Any] = []

    def factory(section: dict[str, Any] | None = None, data_dir: Path | None = None, **kw: Any) -> tuple[Any, FakeCtx]:
        from deskkit.modules.sendprep.module import SendPrepModule

        ctx = FakeCtx(data_dir or (tmp_path / f"data{len(made)}"), section)
        kw.setdefault("shell_link", FakeLinks())
        kw.setdefault("sendto_folder", tmp_path / "SendTo")
        kw.setdefault("fallback_dir", lambda: tmp_path / "Pictures" / "SendPrep")
        kw.setdefault("bundle", lambda: None)
        kw.setdefault("ocr_find", lambda _img, _w: [])
        m = SendPrepModule(ctx, **kw)
        m.start()
        made.append(m)
        return m, ctx

    yield factory
    for m in made:
        m.stop()


def run_all(m: Any, ctx: FakeCtx, timeout: float = 120.0) -> None:
    """ワーカーが終わるまで待ち、call_soon で積まれた画面側の処理を流す。"""
    import time

    t0 = time.monotonic()
    time.sleep(0.05)
    while m.busy():
        if time.monotonic() - t0 > timeout:
            raise TimeoutError("worker did not finish")
        time.sleep(0.05)
    time.sleep(0.05)
    ctx.drain()


# ------------------------------------------------------------------ 合成画像
def tiff_with_gps(orientation: int | None = None, make: bytes = b"SecretCam") -> bytes:
    """IFD0 {Make, Model, DateTime, (Orientation), GPS IFD ポインタ} + GPS IFD の TIFF(II)。"""
    entries: list[tuple[int, int, int, bytes]] = []  # tag, type, count, data
    entries.append((0x010F, 2, len(make) + 1, make + b"\x00"))
    entries.append((0x0110, 2, 6, b"Model\x00"))
    if orientation is not None:
        entries.append((0x0112, 3, 1, struct.pack("<HH", orientation, 0)))
    entries.append((0x0132, 2, 20, b"2026:09:25 10:00:00\x00"))
    entries.append((0x8825, 4, 1, b"\x00\x00\x00\x00"))  # 後で埋める
    n = len(entries)
    ifd0 = 8
    data_off = ifd0 + 2 + n * 12 + 4
    blobs = b""
    raw_entries = []
    gps_index = None
    for i, (tag, typ, cnt, data) in enumerate(entries):
        if tag == 0x8825:
            gps_index = i
            raw_entries.append([tag, typ, cnt, b""])
            continue
        if len(data) <= 4:
            raw_entries.append([tag, typ, cnt, data.ljust(4, b"\x00")])
        else:
            raw_entries.append([tag, typ, cnt, struct.pack("<I", data_off + len(blobs))])
            blobs += data + (b"\x00" if len(data) & 1 else b"")
    gps_off = data_off + len(blobs)
    assert gps_index is not None
    raw_entries[gps_index][3] = struct.pack("<I", gps_off)
    gps_entries = [(0x0001, 2, 2, b"N\x00\x00\x00"), (0x0003, 2, 2, b"E\x00\x00\x00")]
    gps = struct.pack("<H", len(gps_entries)) + b"".join(struct.pack("<HHI", t, ty, c) + d for t, ty, c, d in gps_entries)
    gps += struct.pack("<I", 0)
    t = b"II*\x00" + struct.pack("<I", ifd0) + struct.pack("<H", n)
    for tag, typ, cnt, val in sorted(raw_entries, key=lambda e: e[0]):
        t += struct.pack("<HHI", tag, typ, cnt) + val
    t += struct.pack("<I", 0) + blobs + gps
    return t


def jpeg_with_meta(size: tuple[int, int] = (320, 240), orientation: int | None = None, *, xmp: bool = True,
                   comment: bool = True, iptc: bool = True, icc: bool = True, trailer: bool = False,
                   noise: bool = False, quality: int = 90) -> bytes:
    """GPS・Make・Model・DateTime の Exif、XMP・IPTC・COM・ICC を持つ JPEG。"""
    from PIL import Image, ImageDraw

    if noise:
        im = Image.effect_noise(size, 90).convert("RGB")
    else:
        im = Image.new("RGB", size, (200, 120, 40))
        d = ImageDraw.Draw(im)
        d.rectangle((10, 10, size[0] // 2, size[1] // 2), fill=(20, 60, 200))
    buf = io.BytesIO()
    kw: dict[str, Any] = {"quality": quality, "exif": b"Exif\x00\x00" + tiff_with_gps(orientation)}
    if icc:
        from PIL import ImageCms

        kw["icc_profile"] = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    if comment:
        kw["comment"] = b"secret comment"
    im.save(buf, "JPEG", **kw)
    data = buf.getvalue()
    extra = b""
    if xmp:
        p = b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta>GPS secret</x:xmpmeta>"
        extra += b"\xff\xe1" + struct.pack(">H", len(p) + 2) + p
    if iptc:
        p = b"Photoshop 3.0\x008BIM\x04\x04\x00\x00\x00\x00\x00\x05secret"
        extra += b"\xff\xed" + struct.pack(">H", len(p) + 2) + p
    mpf = b"MPF\x00secretthumb"
    extra += b"\xff\xe2" + struct.pack(">H", len(mpf) + 2) + mpf
    data = data[:2] + extra + data[2:]
    if trailer:
        data += b"\xff\xd8\xff\xe1secret-embedded-image"
    return data


def png_with_meta(size: tuple[int, int] = (200, 120), alpha: bool = False, noise: bool = False) -> bytes:
    from PIL import Image, PngImagePlugin

    mode = "RGBA" if alpha else "RGB"
    if noise:
        base = Image.effect_noise(size, 100).convert(mode)
    else:
        base = Image.new(mode, size, (30, 160, 90, 128) if alpha else (30, 160, 90))
    if alpha and noise:
        base.putalpha(Image.linear_gradient("L").resize(size))
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "secret text")
    info.add_itxt("XML:com.adobe.xmp", "<x:xmpmeta>secret</x:xmpmeta>")
    buf = io.BytesIO()
    base.save(buf, "PNG", pnginfo=info, exif=b"Exif\x00\x00" + tiff_with_gps())
    data = buf.getvalue()
    # tIME を IEND の前に足す
    import zlib

    body = b"tIME" + struct.pack(">HBBBBB", 2026, 9, 25, 10, 0, 0)
    chunk = struct.pack(">I", 7) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    return data[:-12] + chunk + data[-12:]


# ------------------------------------------------------------------ ffmpeg(開発用の配布物から、同梱と同じ形の zip を作る)
def find_dev_zip() -> Path | None:
    env = os.environ.get("DESKKIT_TEST_FFMPEG_ZIP")
    cands = [Path(env)] if env else []
    cands.append(REPO / "third_party" / "ffmpeg" / DEV_ZIP_NAME)
    gitfile = REPO / ".git"
    if gitfile.is_file():  # git worktree: 本体の作業フォルダの third_party も見る
        try:
            line = gitfile.read_text(encoding="utf-8").strip()
            if line.startswith("gitdir:"):
                gd = Path(line.split(":", 1)[1].strip())
                main = gd.parents[2] if gd.parent.name == "worktrees" else None
                if main is not None:
                    cands.append(main / "third_party" / "ffmpeg" / DEV_ZIP_NAME)
        except OSError:
            pass
    return next((c for c in cands if c.is_file()), None)


@pytest.fixture(scope="session")
def ffmpeg_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """同梱と同じ形(ffmpeg.zip + ffmpeg.sha256)のフォルダ。開発用の zip が無ければ skip。"""
    src = find_dev_zip()
    if src is None:
        pytest.skip("開発用の ffmpeg の zip がありません")
    d = tmp_path_factory.mktemp("bundle")
    with zipfile.ZipFile(src) as zf:
        name = next(n for n in zf.namelist() if n.endswith("/bin/ffmpeg.exe"))
        lic = next((n for n in zf.namelist() if n.endswith("LICENSE.txt")), None)
        h = hashlib.sha256()
        with zipfile.ZipFile(d / "ffmpeg.zip", "w", zipfile.ZIP_STORED) as out:
            with zf.open(name) as s, out.open("ffmpeg.exe", "w", force_zip64=True) as o:
                while True:
                    b = s.read(4 * 1024 * 1024)
                    if not b:
                        break
                    h.update(b)
                    o.write(b)
            if lic:
                out.writestr("LICENSE.txt", zf.read(lic))
    (d / "ffmpeg.sha256").write_text(h.hexdigest() + "\n", encoding="ascii")
    return d


@pytest.fixture(scope="session")
def ffmpeg_exe(ffmpeg_bundle: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """テスト用の素材を作るための ffmpeg(同梱の形から一度だけ展開したもの)。"""
    from deskkit.modules.sendprep.video import FfmpegManager

    mgr = FfmpegManager(tmp_path_factory.mktemp("ffcache"), lambda: ffmpeg_bundle)
    return mgr.ensure()


@pytest.fixture(scope="session")
def shared_ffmpeg(ffmpeg_bundle: Path, tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    return SimpleNamespace(bundle=ffmpeg_bundle, data=tmp_path_factory.mktemp("ffdata"))


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # 合成した JPEG に MPF(APP2)を入れているため Pillow が出す警告は想定どおり
    for it in items:
        if "sendprep" in str(it.fspath):
            it.add_marker(pytest.mark.filterwarnings("ignore:Image appears to be a malformed MPO file"))
