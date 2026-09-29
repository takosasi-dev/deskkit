# 子プロセスの止め方(FR-13・AC-9 の偽の Runner)と、書き出しのジョブの ffmpeg を使わない部分:
# 空き容量・FAT32 の断り(AC-11)・.part の片づけ(INV-5・§10)・名前の確定(T-11)・書けないフォルダの代わり(§10)・同じ実体(INV-1)。
from __future__ import annotations

import io
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from deskkit.modules.cliptrim import jobs, plan, probe, runner
from deskkit.modules.cliptrim.oplog import OpsLog


class _In(io.BytesIO):
    def close(self) -> None:
        self.data = self.getvalue()
        super().close()


class FakeProc:
    """ffmpeg の偽物。stdout に進捗を数行出したあと、終わらせられるまで黙る。"""

    def __init__(self, lines: list[bytes], exit_code: int = 0, finish: bool = False) -> None:
        self._killed = threading.Event()
        self.killed = False
        self.returncode: int | None = None
        self._lines = lines
        self._exit = exit_code
        self._finish = finish
        self.stdin = _In()
        self.stdout = self._out()
        self.stderr = self._err()
        if finish:
            self.returncode = exit_code

    def _out(self) -> Any:
        yield from self._lines
        if not self._finish:
            self._killed.wait(10)

    def _err(self) -> Any:
        if not self._finish:
            self._killed.wait(10)
        yield b"last line\n"

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._killed.set()

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self._killed.wait(timeout)
        return self.returncode if self.returncode is not None else -1


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += 5.0  # 1回で 5 秒進む(120 秒の止まりを速く試す)
        time.sleep(0.001)


def test_runner_stall_kills_process() -> None:
    procs: list[FakeProc] = []

    def popen(args: list[str], **_k: Any) -> FakeProc:
        p = FakeProc([b"out_time_us=1000000\n", b"progress=continue\n"])
        procs.append(p)
        return p

    clk = Clock()
    got: list[float] = []
    r = runner.Runner(popen=popen, clock=clk, sleep=clk.sleep)
    res = r.run(["ffmpeg"], duration=10.0, on_progress=got.append, cancelled=lambda: False)
    assert res.stalled and not res.cancelled and procs[0].killed
    assert got == [pytest.approx(0.1)]
    assert clk.t > runner.STALL_S


def test_runner_cancel_and_stop() -> None:
    procs: list[FakeProc] = []

    def popen(args: list[str], **_k: Any) -> FakeProc:
        p = FakeProc([])
        procs.append(p)
        return p

    flag = {"c": False}
    r = runner.Runner(popen=popen, poll_s=0.01)
    th = threading.Timer(0.1, lambda: flag.__setitem__("c", True))
    th.start()
    res = r.run(["ffmpeg"], duration=None, on_progress=lambda _f: None, cancelled=lambda: flag["c"])
    assert res.cancelled and procs[0].killed
    r.stop()
    assert r.run(["ffmpeg"], duration=None, on_progress=lambda _f: None, cancelled=lambda: False).cancelled


def test_runner_passes_stdin_and_exit_code() -> None:
    seen: dict[str, Any] = {}

    def popen(args: list[str], **k: Any) -> FakeProc:
        seen.update(k)
        p = FakeProc([b"out_time_us=5000000\n"], exit_code=0, finish=True)
        seen["proc"] = p
        return p

    res = runner.Runner(popen=popen).run(["ffmpeg"], duration=5.0, on_progress=lambda _f: None,
                                         cancelled=lambda: False, stdin=b"ffconcat version 1.0\n")
    assert res.returncode == 0 and not res.stalled and res.last_line == "last line"
    assert seen["proc"].stdin.data == b"ffconcat version 1.0\n"


def test_token_cancel_kills_attached() -> None:
    p = FakeProc([])
    tok = runner.Token()
    assert tok.attach(p)
    tok.cancel()
    assert p.killed and tok.cancelled
    p2 = FakeProc([])
    assert not tok.attach(p2) and p2.killed


# ------------------------------------------------------------------ ジョブ(ffmpeg を呼ばない部分)
INFO = probe.VideoInfo(120.0, 0.0, ("mov", "mp4"), True, "h264", 320, 240, fps=30.0, video_kbps=600, audio_count=1,
                       audio_kbps=(64,))


def _req(src: Path, mode: str = "copy", output: str = "separate", n: int = 1, size: int = 10_000_000) -> jobs.ExportRequest:
    segs = tuple(plan.Seg(10.0 + 20 * i, 20.0 + 20 * i, 10.0 + 20 * i, 10.0 + 20 * i) for i in range(n))
    return jobs.ExportRequest(src, INFO, size, mode, output, True, segs, 600, (".mp4", "mp4"))


def _env(tmp: Path, **kw: Any) -> jobs.Env:
    kw.setdefault("free_bytes", lambda _p: 10**12)
    kw.setdefault("fs_name", lambda _p: "NTFS")
    kw.setdefault("fallback_dir", lambda: tmp / "fallback")
    return jobs.Env(tmp / "ffmpeg.exe", logging.getLogger("deskkit.cliptrim"), OpsLog(tmp / "data" / "ops.jsonl"), **kw)


def _no_runner() -> runner.Runner:
    raise AssertionError("ffmpeg を呼んではいけない")


@pytest.fixture
def src(tmp_path: Path) -> Path:
    d = tmp_path / "videos"
    d.mkdir()
    p = d / "録画.mp4"
    p.write_bytes(b"\x00" * 1000)
    return p


def _files(d: Path) -> set[str]:
    return {p.name for p in d.iterdir()}


def test_no_space_refuses_before_ffmpeg(tmp_path: Path, src: Path) -> None:
    env = _env(tmp_path, free_bytes=lambda _p: 50_000_000, runner_factory=_no_runner)
    res = jobs.run_export(jobs.Job(_req(src)), env)
    assert res.result == "no_space" and res.message.startswith("空き容量が足りません(あと約 ")
    assert _files(src.parent) == {"録画.mp4"}
    row = OpsLog(tmp_path / "data" / "ops.jsonl").read()[-1]
    assert row["result"] == "no_space" and row["files_ok"] == 0


def test_fat32_refuses_big_output(tmp_path: Path, src: Path) -> None:
    env = _env(tmp_path, fs_name=lambda _p: "FAT32", runner_factory=_no_runner)
    res = jobs.run_export(jobs.Job(_req(src, size=100 * 1024**3)), env)
    assert res.message == "このドライブには 4GB を超えるファイルを書けません"
    assert _files(src.parent) == {"録画.mp4"}


class StallRunner(runner.Runner):
    def run(self, args: list[str], **_k: Any) -> runner.RunResult:
        out = Path(args[-1][len("file:"):])
        out.write_bytes(b"partial")  # 書きかけの出力
        return runner.RunResult(-9, "", stalled=True)


def test_stall_reports_timeout_and_leaves_nothing(tmp_path: Path, src: Path) -> None:
    env = _env(tmp_path, runner_factory=StallRunner)
    res = jobs.run_export(jobs.Job(_req(src, n=2)), env)
    assert [o.message for o in res.outputs] == ["応答が無くなったため止めました"] * 2
    assert res.result == "timeout"
    assert _files(src.parent) == {"録画.mp4"}


def test_join_stall_fails_all_and_leaves_nothing(tmp_path: Path, src: Path) -> None:
    env = _env(tmp_path, runner_factory=StallRunner)
    res = jobs.run_export(jobs.Job(_req(src, output="join", n=3)), env)
    assert res.result == "timeout" and res.message == "応答が無くなったため止めました"
    assert _files(src.parent) == {"録画.mp4"}


class OkRunner(runner.Runner):
    def run(self, args: list[str], **_k: Any) -> runner.RunResult:
        Path(args[-1][len("file:"):]).write_bytes(b"video")
        return runner.RunResult(0, "")


def test_fallback_when_folder_not_writable(tmp_path: Path, src: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = jobs._create_excl

    def excl(p: Path) -> None:
        if p.parent == src.parent:
            raise PermissionError(13, "denied")
        real(p)

    monkeypatch.setattr(jobs, "_create_excl", excl)
    monkeypatch.setattr(jobs, "verify", lambda *a, **k: 10.0)
    env = _env(tmp_path, runner_factory=OkRunner)
    res = jobs.run_export(jobs.Job(_req(src)), env)
    assert res.result == "ok" and res.fallback and res.folder == tmp_path / "fallback"
    assert _files(tmp_path / "fallback") == {"録画_clip.mp4"}


def test_names_do_not_overwrite(tmp_path: Path, src: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "verify", lambda *a, **k: 10.0)
    (src.parent / "録画_clip1.mp4").write_bytes(b"mine")
    (src.parent / "録画_clip1 (2).mp4").write_bytes(b"mine2")
    res = jobs.run_export(jobs.Job(_req(src, n=2)), _env(tmp_path, runner_factory=OkRunner))
    assert res.result == "ok"
    assert [o.path.name for o in res.outputs if o.path] == ["録画_clip1 (3).mp4", "録画_clip2.mp4"]
    assert (src.parent / "録画_clip1.mp4").read_bytes() == b"mine"


def test_verify_failure_removes_part(tmp_path: Path, src: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "verify", lambda *a, **k: None)
    res = jobs.run_export(jobs.Job(_req(src, n=2)), _env(tmp_path, runner_factory=OkRunner))
    assert res.result == "verify_failed" and res.outputs[0].message == "確かめられなかったため書き出しをやめました"
    assert _files(src.parent) == {"録画.mp4"}


def test_ffmpeg_failure_message_and_partial(tmp_path: Path, src: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "verify", lambda *a, **k: 10.0)
    calls = {"n": 0}

    class Flaky(runner.Runner):
        def run(self, args: list[str], **_k: Any) -> runner.RunResult:
            calls["n"] += 1
            Path(args[-1][len("file:"):]).write_bytes(b"v")
            if calls["n"] == 1:
                return runner.RunResult(1, "Invalid data found when processing input", stderr_text="x")
            return runner.RunResult(0, "")

    res = jobs.run_export(jobs.Job(_req(src, n=2)), _env(tmp_path, runner_factory=Flaky))
    assert res.result == "partial"  # 区間ごとは1本の失敗で残りを止めない(FR-14)
    assert res.outputs[0].message == "動画を書き出せませんでした: Invalid data found when processing input"
    assert res.outputs[1].ok and _files(src.parent) == {"録画.mp4", "録画_clip2.mp4"}


def test_precise_encoder_failure_message(tmp_path: Path, src: Path) -> None:
    class Fail(runner.Runner):
        def run(self, args: list[str], **_k: Any) -> runner.RunResult:
            return runner.RunResult(1, "Error", stderr_text="[h264_mf @ 0x1] could not set output type")

    res = jobs.run_export(jobs.Job(_req(src, mode="precise")), _env(tmp_path, runner_factory=Fail))
    assert res.outputs[0].message == "この大きさの動画は、この PC では作り直せません。画質そのままを使ってください"


def test_cancel_before_start(tmp_path: Path, src: Path) -> None:
    job = jobs.Job(_req(src))
    job.cancel()
    res = jobs.run_export(job, _env(tmp_path, runner_factory=_no_runner))
    assert res.result == "cancelled" and _files(src.parent) == {"録画.mp4"}


def test_source_gone(tmp_path: Path, src: Path) -> None:
    src.unlink()
    res = jobs.run_export(jobs.Job(_req(src)), _env(tmp_path, runner_factory=_no_runner))
    assert res.result == "error" and res.message == "元の動画が見つかりません"


def test_cleanup_stale_parts_only_own_old(tmp_path: Path) -> None:
    d = tmp_path / "v"
    d.mkdir()
    names = ["a_clip.mp4.cliptrim-0a1b2c3d.part", "a_clip.mp4.cliptrim-0a1b2c3d-3.part",
             "fresh_clip.mp4.cliptrim-11111111.part", "user.part", "a.cliptrim-XYZ.part", "a_clip.mp4"]
    for n in names:
        (d / n).write_bytes(b"x")
    old = time.time() - 7200
    for n in names:
        if not n.startswith("fresh"):
            os.utime(d / n, (old, old))
    assert jobs.cleanup_stale_parts(d) == 2
    assert _files(d) == {"fresh_clip.mp4.cliptrim-11111111.part", "user.part", "a.cliptrim-XYZ.part", "a_clip.mp4"}


def test_same_file(tmp_path: Path) -> None:
    a = tmp_path / "x.mp4"
    a.write_bytes(b"x")
    assert jobs.same_file(a, tmp_path / "." / "X.MP4")
    assert not jobs.same_file(a, tmp_path / "y.mp4")
    assert jobs.remove_part(a) is None and a.exists()  # .part でない物は消さない


def test_request_estimates() -> None:
    r = _req(Path("C:/v/a.mp4"), n=2, size=120_000_000)
    assert r.estimates() == [plan.estimate_copy(120_000_000, 10.0, 120.0, False)] * 2
    j = _req(Path("C:/v/a.mp4"), output="join", n=2, size=120_000_000)
    assert j.join and j.estimates() == [plan.estimate_copy(120_000_000, 20.0, 120.0, True)]
    one = _req(Path("C:/v/a.mp4"), output="join", n=1)
    assert not one.join
