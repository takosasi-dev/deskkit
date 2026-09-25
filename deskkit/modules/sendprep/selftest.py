# SendPrep の自己検査(AC-13)。一時フォルダの合成画像で、メタデータ除去・検証・縮小・伏せ字・候補の判定・出力名・
# 操作記録にファイル名が出ないことを確かめる。同梱の ffmpeg があれば展開と照合も確かめる。実機の設定・「送る」には触れない。
# run() は 0=合格 / 1=不合格。
from __future__ import annotations

import io
import logging
import struct
import tempfile
from pathlib import Path


class _Result:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, name: str, ok: bool) -> None:
        print(f"  [{'OK' if ok else 'NG'}] {name}")
        if not ok:
            self.failed += 1


def _gps_jpeg(orientation: int) -> bytes:
    from PIL import Image

    tiff = b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", 3)
    tiff += struct.pack("<HHI", 0x010F, 2, 4) + b"Cam\x00"
    tiff += struct.pack("<HHI", 0x0112, 3, 1) + struct.pack("<HH", orientation, 0)
    tiff += struct.pack("<HHI", 0x8825, 4, 1) + struct.pack("<I", 50) + struct.pack("<I", 0)
    tiff += struct.pack("<H", 1) + struct.pack("<HHI", 1, 2, 2) + b"N\x00\x00\x00" + struct.pack("<I", 0)
    buf = io.BytesIO()
    Image.effect_noise((320, 240), 40).convert("RGB").save(buf, "JPEG", exif=b"Exif\x00\x00" + tiff, comment=b"memo")
    return buf.getvalue()


def _checks(r: _Result, tmp: Path) -> None:
    from PIL import Image

    from deskkit.modules.sendprep import jobs as J
    from deskkit.modules.sendprep import metadata as M
    from deskkit.modules.sendprep import ocr, shrink, video
    from deskkit.modules.sendprep.oplog import OpsLog
    from deskkit.modules.sendprep.redact import burn

    src_bytes = _gps_jpeg(6)
    stripped = M.strip_jpeg(src_bytes)
    ex = Image.open(io.BytesIO(stripped)).getexif()
    r.check("JPEG: GPS・機種・コメントが消え、Orientation だけが残る", dict(ex) == {0x0112: 6} and b"memo" not in stripped)
    a = Image.open(io.BytesIO(src_bytes)).tobytes()
    b = Image.open(io.BytesIO(stripped)).tobytes()
    r.check("JPEG: 画素が変わらない(再エンコードしない)", a == b and M.verify_jpeg(stripped) and not M.verify_jpeg(src_bytes))

    from PIL import PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "secret")
    buf = io.BytesIO()
    Image.new("RGB", (40, 30)).save(buf, "PNG", pnginfo=info)
    png = M.strip_png(buf.getvalue())
    r.check("PNG: tEXt が消える", M.verify_png(png) and not M.verify_png(buf.getvalue()))

    big = Image.effect_noise((1600, 1200), 80).convert("RGB")
    enc = shrink.fit_jpeg(big, 300_000, None)
    r.check("縮小: 上限以下になる", enc is not None and len(enc.data) <= 300_000 and M.verify_jpeg(enc.data))
    r.check("縮小: 640px を下回るなら諦める", shrink.fit_jpeg(Image.effect_noise((640, 480), 90).convert("RGB"), 5_000, None) is None)

    out = burn(big, [(10, 10, 40, 30)], "fill")
    r.check("伏せ字: 囲んだ範囲が塗りつぶしの色になる", {c for _n, c in (out.crop((10, 10, 50, 40)).getcolors(1 << 24) or [])} == {(0, 0, 0)})

    kinds = {k for k, _s, _e in ocr.find_spans("a@example.com 090-1234-5678 〒100-0001 @alice 山田", ["山田"])}
    r.check("候補: メール・電話・郵便番号・@ユーザー名・登録した言葉", kinds == {"email", "phone", "postal", "user", "word"})

    secret = "SELFTEST-名前-7f3"
    src = tmp / f"{secret}.jpg"
    src.write_bytes(src_bytes)
    log = logging.getLogger("deskkit.sendprep.selftest")
    log.propagate = False
    log.setLevel(logging.DEBUG)
    h = logging.FileHandler(tmp / "sendprep.log", encoding="utf-8")
    log.addHandler(h)
    try:
        env = J.Env(video.FfmpegManager(tmp / "ff", lambda: None), OpsLog(tmp / "ops.jsonl"), log,
                    fallback_dir=lambda: tmp / "fallback")
        job = J.Job(1, src, "image", preset_id="meta")
        J.process(job, env)
        (tmp / f"{secret}_share (2).jpg").write_bytes(b"x")
        job2 = J.Job(2, src, "image", preset_id="meta")
        J.process(job2, env)
    finally:
        h.close()
        log.removeHandler(h)
    r.check("処理: 隣に _share で書き、元のファイルは変わらない",
            job.state == J.STATE_DONE and job.out_path == tmp / f"{secret}_share.jpg" and src.read_bytes() == src_bytes)
    r.check("処理: 同じ名前があれば番号を足す", job2.out_path == tmp / f"{secret}_share (3).jpg")
    texts = (tmp / "ops.jsonl").read_text(encoding="utf-8") + (tmp / "sendprep.log").read_text(encoding="utf-8")
    r.check("ログと ops.jsonl にファイル名が出ない", secret not in texts and "result=ok" in texts)

    bd = video.bundled_dir()
    if bd is None:
        print("  [--] 同梱の ffmpeg が無いので動画の検査は飛ばします")
    else:
        mgr = video.FfmpegManager(tmp / "ffx", lambda: bd)
        try:
            exe = mgr.ensure()
            r.check("動画: 同梱の ffmpeg を展開して照合できる", exe.is_file() and mgr.state() == video.STATE_EXTRACTED)
        except video.VideoError as e:
            r.check(f"動画: 同梱の ffmpeg を展開して照合できる({e.code})", False)


def run() -> int:
    r = _Result()
    print("SendPrep 自己検査")
    try:
        with tempfile.TemporaryDirectory(prefix="sendprep-selftest-") as td:
            _checks(r, Path(td))
    except Exception as e:  # noqa: BLE001 - 自己検査は例外も不合格として返す
        print(f"  [NG] 例外: {type(e).__name__}")
        r.failed += 1
    print("合格" if r.failed == 0 else f"不合格({r.failed} 件)")
    return 0 if r.failed == 0 else 1
