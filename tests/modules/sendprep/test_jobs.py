# ジョブの流れ(画像): 出力名・保存先・検証・失敗の扱い・キュー・伏せ字の焼き込み・操作記録。
# AC-1(元の SHA-256 が変わらない)・AC-5・AC-6(HEIC)・AC-8・AC-12(ファイル名がログと ops.jsonl に出ない)。
from __future__ import annotations

import base64
import errno
import hashlib
import io
import logging
import struct
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from deskkit.modules.sendprep import jobs as J
from deskkit.modules.sendprep import metadata as M
from deskkit.modules.sendprep.oplog import OpsLog
from deskkit.modules.sendprep.redact import burn, mosaic_block
from deskkit.modules.sendprep.video import FfmpegManager

from .conftest import ListHandler, jpeg_with_meta, png_with_meta, run_all

# 64x48 の HEIC(kvazaar で作った HEVC の1枚 + Make=SecretCam と GPS の Exif アイテム)
HEIC_B64 = (
    "AAAAGGZ0eXBoZWljAAAAAG1pZjFoZWljAAABbm1ldGEAAAAAAAAAIWhkbHIAAAAAAAAAAHBpY3QAAAAAAAAAAAAAAAAAAAAADnBpdG0AAAAAAAEAAAAsaWxvYwAAAABE"
    "AAACAAEAAAABAAABjgAAA50AAgAAAAEAAAUrAAAATAAAADhpaW5mAAAAAAACAAAAFWluZmUCAAAAAAEAAGh2YzEAAAAAFWluZmUCAAAAAAIAAEV4aWYAAAAAGmlyZWYA"
    "AAAAAAAADmNkc2MAAgABAAEAAAC1aXBycAAAAJhpcGNvAAAAfGh2Y0MBAWAAAACAAAAAAAC68AD8/fj4AAAPA6AAAQAbQAEMAv//AWAAAAMAgAAAAwAAAwC6AAAEAhAk"
    "oQABACtCAQIBYAAAAwCAAAADAAADALoAAKAggxbBAIZJMqwEBAAAAwAEAAADAAQgogABAAhEAcBiRgpkgAAAABRpc3BlAAAAAAAAAEAAAAAwAAAAFWlwbWEAAAAAAAAA"
    "AQABAoECAAAD8W1kYXQAAAOZJgGvPmrGWgNU1Bd/1oNIVbeHLgBosD7YuQAXuEl+ll0yrRAYt///fbouT/NWFbZC+sm2bpwH/96c4AlF75lCU9fUap/GV/8wBX2lPMz6"
    "Uc+qyOh+kPlsa8gIAVf5jvi1QNuz72WPH6jOPYcrj2JyPoEZFBNkvVnQAU2ZZJDc8Hbqo/3BJtms68KHEWILq/3KL+v34oMjv/rWv9rIN/+agqPxZg4hYUQJ7x0FOSbD"
    "KLVCy4yguyxYrF/OLzmXM9qO7Z+38BuHkAAn73jR1gkv4fNLbcd/4jSIfuaV4QzoVS7eq1HpOLE/dGIgxkOUytTUBPYsdZRRTZ1oiYGRenLv6mIX///9twzVOFy6El1P"
    "X1Iuf0pomanq8lSvFufcgyFb2vb/sJSqLbacxP60JEp/v/LIkYk/XYkb3q9KZdXCe0b3uzOQxOtAAuQv148KiTlGgib8Q3lc/01lMkkSoAnDuux0k4KCRB1SQgJWaJtI"
    "0WRt4pSMKztWWA9s8BPzqmOV2UVUKK4i3KhFpkor1R75cWXyVRVb3D6mih4+NjB+EFGSGYzaXgBXUlLrEGJCJxWiOTwGVZDwjzJAGnBY8fM5pCzWS6xYqT2eqy0wUTyf"
    "8ndWuWvROWtE3PhtfavJoCds7a3yT2+4s5lwsLy3qHqVdN0t8vXrbIU+61t2bGSO3iaItvoai1ab1rw5e7rqLNKf1/8KXaeHdR7V307/YgSjWgLYhyzFdAPyrHhMYMWP"
    "ePth0Xz2t2WAF7Rd0FvW6UMJp7F7nyKmbggja6XKokrLk9HXIxR84XnB1VvOXul7Oc4/qLAhzQDhMjcs1SIV/vaQjM9rRHCdrdD8rhfrASiGijroMA6c4/RVQzaVWYX+"
    "A23VV/fKvsJJ/OozKD//WLMn1xqdjWigOnlQ4L4s+l/pP9SXlS9ufW+1lIJPVrJ4H/Km2msXNQz6f0/1WvJCYr1hkNWxdZmHmv1KkWwfB+jKXig05hli2ksEwmY6N+VD"
    "Ax1klIRTa00Afp61/P96nXsKXvZnc+yDVpZtmePkDlbW0Gz/eaefbhGHlmwEoLzq8tsthohcPMM5Q2Ek5l0/LdsQQBDVwtY8Had1U4fKzHOJgciBAdcXfLsYrn9n1i0N"
    "nCQrh4vb2cpOWP/X7wct8wrszF595L8SjASbTS0QaLaUgNCLTVGCgM8QWUu0+tHgxsh4n98VawCcjOmA2oVIl4ZE8EtqsGnvfcP4AAAABkV4aWYAAElJKgAIAAAAAgAP"
    "AQIACgAAADgAAAAliAQAAQAAACYAAAAAAAAAAQABAAIAAgAAAE4AAAAAAAAAU2VjcmV0Q2FtAA=="
)
SECRET_NAME = "山田花子の自宅_GPS検体"


def _env(tmp: Path, **kw: Any) -> J.Env:
    kw.setdefault("fallback_dir", lambda: tmp / "fallback")
    return J.Env(FfmpegManager(tmp / "ff", lambda: None), OpsLog(tmp / "ops.jsonl"), logging.getLogger("deskkit.sendprep"), **kw)


def _job(path: Path, limit: int | None = None, preset: str = "meta") -> J.Job:
    j = J.Job(1, path, J.kind_of(path) or "image")
    j.limit_bytes, j.preset_id = limit, preset
    return j


def _colors(im: Image.Image) -> set[object]:
    return {c for _n, c in (im.getcolors(1 << 24) or [])}


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_end_to_end_meta_and_ac12(make_module: Any, tmp_path: Path, logs: ListHandler) -> None:
    src = tmp_path / f"{SECRET_NAME}.jpg"
    src.write_bytes(jpeg_with_meta(orientation=6))
    png = tmp_path / f"{SECRET_NAME}.png"
    png.write_bytes(png_with_meta())
    before = _sha(src), _sha(png)
    m, ctx = make_module()
    assert m.add_paths([src, png]).added == 2
    assert m.start_processing("meta") == 2
    run_all(m, ctx)
    j1, j2 = m.jobs
    assert j1.state == J.STATE_DONE and j2.state == J.STATE_DONE, (j1.message, j2.message)
    assert j1.out_path == tmp_path / f"{SECRET_NAME}_share.jpg" and j2.out_path == tmp_path / f"{SECRET_NAME}_share.png"
    assert (_sha(src), _sha(png)) == before  # INV-1
    out = j1.out_path.read_bytes()
    assert M.verify_jpeg(out) and dict(Image.open(io.BytesIO(out)).getexif()) == {0x0112: 6}
    assert j1.result_text().startswith("位置情報なし・")
    assert m.summary is not None and m.summary.done == 2 and m.summary.failed == 0
    # AC-12: ログと ops.jsonl にファイル名・フォルダ名が出ない
    ops_text = (ctx.data_dir / "ops.jsonl").read_text(encoding="utf-8")
    rows = m.ops.read()
    assert len(rows) == 2 and {r["result"] for r in rows} == {"ok"} and rows[0]["kind"] == "image"
    assert set(rows[0]) == {"ts", "kind", "preset", "result", "in_bytes", "out_bytes", "redactions", "ms"}
    log_text = logs.text()
    for needle in (SECRET_NAME, tmp_path.name, str(tmp_path)):
        assert needle not in ops_text and needle not in log_text
    assert "result=ok" in log_text
    d = m.diagnostics()
    assert all(SECRET_NAME not in str(v) and str(tmp_path) not in str(v) for v in d.values())


def test_ac6_heic_to_jpeg(tmp_path: Path) -> None:
    src = tmp_path / "IMG_0001.HEIC"
    src.write_bytes(base64.b64decode(HEIC_B64))
    j = _job(src)
    J.process(j, _env(tmp_path))
    assert j.state == J.STATE_DONE, j.message
    assert j.out_path is not None and j.out_path.name == "IMG_0001_share.jpg"
    data = j.out_path.read_bytes()
    assert M.detect(data) == "jpeg" and M.verify_jpeg(data) and b"SecretCam" not in data
    im = Image.open(io.BytesIO(data))
    assert im.size == (64, 48) and not im.getexif()


def test_ac5_too_large_leaves_nothing(tmp_path: Path) -> None:
    src = tmp_path / "noise.png"
    Image.effect_noise((640, 480), 90).convert("RGB").save(src)
    j = _job(src, limit=10_000, preset="small")
    env = _env(tmp_path)
    J.process(j, env)
    assert j.state == J.STATE_FAILED and j.result == "too_large" and j.message == "上限に収まりません"
    assert sorted(p.name for p in tmp_path.iterdir() if p.suffix in (".png", ".jpg")) == ["noise.png"]
    assert env.ops.read()[-1]["result"] == "too_large" and env.ops.read()[-1]["out_bytes"] is None


def test_limit_preset_shrinks_and_fr8_keeps_small(tmp_path: Path) -> None:
    big = tmp_path / "big.jpg"
    big.write_bytes(jpeg_with_meta((2400, 1800), noise=True, quality=95))
    assert big.stat().st_size > 1_000_000
    j = _job(big, limit=1_000_000, preset="custom-1")
    J.process(j, _env(tmp_path))
    assert j.state == J.STATE_DONE and j.out_bytes is not None and j.out_bytes <= 1_000_000
    small = tmp_path / "small.jpg"
    data = jpeg_with_meta()
    small.write_bytes(data)
    j2 = _job(small, limit=1_000_000)
    J.process(j2, _env(tmp_path))
    assert j2.out_path is not None and j2.out_path.read_bytes() == M.strip_jpeg(data)  # FR-8: 縮めない


def test_names_date_and_collisions(tmp_path: Path) -> None:
    src = tmp_path / "a.jpg"
    src.write_bytes(jpeg_with_meta())
    (tmp_path / "a_share.jpg").write_bytes(b"x")
    j = _job(src)
    J.process(j, _env(tmp_path))
    assert j.out_path == tmp_path / "a_share (2).jpg"
    env = _env(tmp_path, rename_to_date=lambda: True, now=lambda: datetime(2026, 9, 25, 10, 15, 0))
    j2 = _job(src)
    J.process(j2, env)
    assert j2.out_path == tmp_path / "share_20260925_101500.jpg"
    # 1000 個まで埋まっていたら失敗
    d = tmp_path / "full"
    d.mkdir()
    s2 = d / "b.png"
    s2.write_bytes(png_with_meta())
    (d / "b_share.png").write_bytes(b"x")
    for n in range(2, 1001):
        (d / f"b_share ({n}).png").write_bytes(b"x")
    j3 = _job(s2)
    J.process(j3, _env(tmp_path))
    assert j3.state == J.STATE_FAILED and j3.message == J.MSG_NAMES


def test_fallback_to_pictures_when_denied(tmp_path: Path) -> None:
    src_dir = tmp_path / "ro"
    src_dir.mkdir()
    src = src_dir / "c.jpg"
    src.write_bytes(jpeg_with_meta())

    def create(p: Path) -> None:
        if p.parent == src_dir:
            raise PermissionError(13, "denied")
        J._create_excl(p)

    j = _job(src)
    J.process(j, _env(tmp_path, create_file=create))
    assert j.state == J.STATE_DONE and j.fallback and j.out_path == tmp_path / "fallback" / "c_share.jpg"
    assert "ピクチャ\\SendPrep" in j.result_text()


def test_disk_full_removes_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "d.jpg"
    src.write_bytes(jpeg_with_meta())

    def boom(_fd: int) -> None:
        raise OSError(errno.ENOSPC, "No space")

    monkeypatch.setattr(J.os, "fsync", boom)
    j = _job(src)
    J.process(j, _env(tmp_path))
    assert j.state == J.STATE_FAILED and j.message == "空き容量が足りません"
    assert not (tmp_path / "d_share.jpg").exists()


def test_failures_do_not_stop_others(make_module: Any, tmp_path: Path) -> None:
    gone = tmp_path / "gone.jpg"
    gone.write_bytes(jpeg_with_meta())
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"\xff\xd8\xff\xe0garbage" * 3)
    bomb = tmp_path / "bomb.png"

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    bomb.write_bytes(M.PNG_SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", 20000, 20000, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b""))
    ok = tmp_path / "ok.png"
    ok.write_bytes(png_with_meta())
    m, ctx = make_module()
    m.add_paths([gone, broken, bomb, ok])
    gone.unlink()
    m.start_processing("meta")
    run_all(m, ctx)
    msgs = [(j.path.name, j.state, j.message) for j in m.jobs]
    assert msgs[0][1:] == (J.STATE_FAILED, "読めませんでした")
    assert msgs[1][1:] == (J.STATE_FAILED, "画像を読めませんでした")
    assert msgs[2][1:] == (J.STATE_FAILED, "大きすぎる画像です")
    assert msgs[3][1] == J.STATE_DONE
    assert m.summary is not None and (m.summary.done, m.summary.failed) == (1, 3)
    assert ctx.notifications and ctx.notifications[-1][1] == "完了 1 件・失敗 3 件"


def test_animated_gif_first_frame(tmp_path: Path) -> None:
    src = tmp_path / "anim.gif"
    frames = [Image.new("RGB", (40, 30), (i * 80, 20, 0)) for i in range(3)]
    frames[0].save(src, save_all=True, append_images=frames[1:], duration=100, loop=0, comment=b"secret")
    j = _job(src)
    J.process(j, _env(tmp_path))
    assert j.state == J.STATE_DONE and j.out_path is not None and j.out_path.suffix == ".png"
    assert J.NOTE_ANIMATION in j.result_text() and b"secret" not in j.out_path.read_bytes()


def test_queue_rules(make_module: Any, tmp_path: Path) -> None:
    d = tmp_path / "folder"
    (d / "sub").mkdir(parents=True)
    for i in range(3):
        (d / f"p{i}.jpg").write_bytes(b"x")
    (d / "note.txt").write_text("x")
    (d / "sub" / "deep.jpg").write_bytes(b"x")
    m, ctx = make_module()
    r = m.add_paths([d])
    assert (r.added, r.unsupported) == (3, 1)  # サブフォルダは見ない
    assert m.add_paths([d / "p0.jpg"]).duplicates == 1 and len(m.jobs) == 3
    many = tmp_path / "many"
    many.mkdir()
    for i in range(205):
        (many / f"m{i:03d}.png").write_bytes(b"x")
    r2 = m.add_paths([many])
    assert r2.added == 197 and r2.over_limit == 8 and m.queue_full and len(m.active_jobs()) == 200
    assert m.unsupported == 1
    m.remove_job(m.jobs[0])
    assert len(m.active_jobs()) == 199 and not m.queue_full


def test_handle_cli_open(make_module: Any, tmp_path: Path) -> None:
    files = []
    for i in range(203):
        p = tmp_path / f"s{i}.jpg"
        p.write_bytes(b"x")
        files.append(str(p))
    m, ctx = make_module()
    code, out = m.handle_cli(["open", *files])
    assert (code, out) == (0, "queued 200") and ctx.shown == 1 and m.queue_full
    assert m.handle_cli(["nope"]) == (2, "unsupported")


def test_ac8_burn_fill_and_mosaic() -> None:
    im = Image.effect_noise((200, 150), 80).convert("RGB")
    out = burn(im, [(10, 20, 50, 40), (190, 140, 50, 50)], "fill")
    region = out.crop((10, 20, 60, 60))
    assert _colors(region) == {(0, 0, 0)}
    assert _colors(out.crop((190, 140, 200, 150))) == {(0, 0, 0)}
    assert out.getpixel((0, 0)) == im.getpixel((0, 0))
    assert mosaic_block(30, 90) == 12 and mosaic_block(90, 60) == 20
    mo = burn(im, [(0, 0, 60, 60)], "mosaic")
    blk = mo.crop((0, 0, 20, 20))
    assert len(_colors(blk)) == 1  # 20px のブロック1つは1色
    rgba = burn(Image.new("RGBA", (20, 20), (255, 0, 0, 0)), [(0, 0, 10, 10)], "fill")
    assert rgba.getpixel((5, 5)) == (0, 0, 0, 255)


def test_burn_output_png_and_usage(make_module: Any, tmp_path: Path) -> None:
    src = tmp_path / "shot.png"
    Image.new("RGB", (300, 200), (250, 250, 250)).save(src)
    m, ctx = make_module()
    m.add_paths([src])
    m.start_processing("meta")
    run_all(m, ctx)
    job = m.jobs[0]
    J.burn_output(job, [(20, 30, 100, 40)], "fill", m.env)
    assert job.redactions == 1 and job.out_path is not None and job.out_path.suffix == ".png"
    back = Image.open(job.out_path).convert("RGB")
    assert _colors(back.crop((20, 30, 120, 70))) == {(0, 0, 0)} and M.verify_png(job.out_path.read_bytes())
    assert "伏せ字 1 か所" in job.result_text()
    series = {s.key: s for s in m.usage(7)}
    assert series["prepared"].per_day[-1] == 1 and series["prepared"].primary and series["redacted"].per_day[-1] == 1
    job.out_path.unlink()
    with pytest.raises(J.JobError) as ei:
        J.burn_output(job, [(0, 0, 5, 5)], "fill", m.env)
    assert ei.value.message == "出力が見つかりません"


def test_burn_output_jpeg_orientation_baked(tmp_path: Path) -> None:
    src = tmp_path / "rot.jpg"
    src.write_bytes(jpeg_with_meta((320, 240), orientation=6))
    j = _job(src, limit=3_000_000, preset="small")
    env = _env(tmp_path)
    J.process(j, env)
    img = J.load_for_edit(j.out_path)  # type: ignore[arg-type]
    assert img.size == (240, 320)  # 見たままの向き
    J.burn_output(j, [(0, 0, 50, 50)], "fill", env)
    data = j.out_path.read_bytes()  # type: ignore[union-attr]
    assert M.verify_jpeg(data) and M.jpeg_orientation(data) == 1 and Image.open(io.BytesIO(data)).size == (240, 320)
    assert env.ops.read()[-1]["redactions"] == 1
