# PcCheckup の自己検査。偽の probes でしきい値の境界・例外時の unknown・コピー文字列の伏せ字・履歴の中身を確かめ、
# 一時フォルダに作った「古い/新しいファイルとジャンクション」で掃除の対象の絞り込みを確かめる(ごみ箱は偽物)。
# 実機の %TEMP%・ごみ箱・%LOCALAPPDATA%\DeskKit には触れない。run() は 0=合格 / 1=不合格。
from __future__ import annotations

import logging
import os
import tempfile
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from deskkit.fileops import RecycleResult
from deskkit.modules.pccheckup import cleanup
from deskkit.modules.pccheckup.checks import net, perf, storage
from deskkit.modules.pccheckup.checks.base import Cancel, GiB
from deskkit.modules.pccheckup.fakes import FakeProbes
from deskkit.modules.pccheckup.history import History
from deskkit.modules.pccheckup.probes import CpuSample, DiskInfo, MemInfo, ProcUsage, SizeInfo, StartupInfo
from deskkit.modules.pccheckup.report import Secrets, build, redact
from deskkit.modules.pccheckup.runner import ALL_IDS, run_category


class _Result:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, name: str, ok: bool) -> None:
        print(f"  [{'OK' if ok else 'NG'}] {name}")
        if not ok:
            self.failed += 1


def _thresholds(r: _Result) -> None:
    print("しきい値の境界")
    r.check("P1 85% は bad・84.9% は warn", perf.judge_cpu(CpuSample(85.0, ())).status == "bad"
            and perf.judge_cpu(CpuSample(84.9, ())).status == "warn")
    r.check("P1 60% は warn・59.9% は good", perf.judge_cpu(CpuSample(60.0, ())).status == "warn"
            and perf.judge_cpu(CpuSample(59.9, ())).status == "good")
    r.check("P2 90% は bad・80% は warn・79.9% は good",
            [perf.judge_memory(MemInfo(v, GiB, ())).status for v in (90.0, 80.0, 79.9)] == ["bad", "warn", "good"])
    r.check("P3 空き 4.9% は bad", perf.disk_status(DiskInfo("C:\\", 1000 * GiB, 49 * GiB)) == "bad")
    r.check("P3 空き 5GB ちょうどは bad でない", perf.disk_status(DiskInfo("C:\\", 50 * GiB, 5 * GiB)) == "warn")
    r.check("P4 7 日ちょうどは warn", perf.judge_uptime(7 * 86400).status == "warn"
            and perf.judge_uptime(7 * 86400 - 1).status == "good")
    r.check("P5 25 は bad・15 は warn・14 は good",
            [perf.judge_startup(StartupInfo(n, 0)).status for n in (25, 15, 14)] == ["bad", "warn", "good"])
    r.check("S2 1GB ちょうどは info", storage.judge_recycle_bin(GiB).status == "info"
            and storage.judge_recycle_bin(GiB - 1).status == "good")


def _runner(r: _Result) -> None:
    print("例外のチェックだけ unknown(FR-5)")
    log = logging.getLogger("deskkit.pccheckup.selftest")
    log.propagate = False  # 例外の型名のログは画面に出さない
    log.addHandler(logging.NullHandler())
    fp = FakeProbes(raise_on={"memory", "connectivity"})
    res = run_category("perf", fp, Cancel(), log)
    st = {f.check_id: f.status for f in res.findings}
    r.check("P2 だけ unknown で P1〜P7 がそろう", st.get("P2") == "unknown" and len(st) == 7
            and all(v != "unknown" for k, v in st.items() if k != "P2"))
    res = run_category("net", fp, Cancel(), log)
    st = {f.check_id: f.status for f in res.findings}
    r.check("winrt が読めないと N1・N3・N4 が unknown、N2・N5 は続く",
            [st[i] for i in ("N1", "N3", "N4")] == ["unknown"] * 3 and st["N2"] == "good" and st["N5"] == "good")
    r.check("N2 は既定のゲートウェイのアダプタで判定", net.pick_adapter(fp.adapter_list, None) is not None)


def _report(r: _Result) -> None:
    print("結果をコピーの伏せ字(P-4)")
    fs = [perf.judge_cpu(CpuSample(90.0, (ProcUsage("heavy.exe", 70.0),))),
          storage.judge_downloads("C:\\Users\\taro-sample\\Downloads", SizeInfo(11 * GiB, 3))]
    text = build([("perf", fs[:1], 5000), ("storage", fs[1:], 1000)], datetime.now(),
                 Secrets("C:\\Users\\taro-sample", "taro-sample", "PC-SECRET01", ("HomeNet-5G",)))
    r.check("プロセス名は入る", "heavy.exe" in text)
    r.check("ユーザー名・PC 名・SSID は入らない",
            all(s.lower() not in text.lower() for s in ("taro-sample", "PC-SECRET01", "HomeNet-5G")))
    sec = Secrets("C:\\Users\\taro-sample", "taro-sample", None, ())
    r.check("ユーザーフォルダは %USERPROFILE%",
            redact("C:\\Users\\Taro-Sample\\Downloads", sec) == "%USERPROFILE%\\Downloads")


def _cleanup(r: _Result, tmp: Path) -> None:
    print("古い一時ファイルの絞り込み(INV-4。ごみ箱は偽物)")
    root = tmp / "temp"
    root.mkdir()
    now = time.time()
    old = root / "old.log"
    old.write_bytes(b"x" * 10)
    os.utime(old, (now - 8 * 86400, now - 8 * 86400))
    new = root / "new.log"
    new.write_bytes(b"y")
    os.utime(new, (now - 86400, now - 86400))
    outside = tmp / "outside"
    outside.mkdir()
    far = outside / "far.log"
    far.write_bytes(b"z")
    os.utime(far, (now - 30 * 86400, now - 30 * 86400))
    junction_ok = True
    try:
        import _winapi

        _winapi.CreateJunction(str(outside), str(root / "link"))
    except (ImportError, OSError, AttributeError):
        junction_ok = False
    scan = cleanup.scan_old_temp(str(root), now, lambda: False)
    got: list[Path] = []

    def fake(paths: Sequence[Path], _hwnd: int | None) -> RecycleResult:
        got.extend(paths)
        return RecycleResult(sent=list(paths))

    res = cleanup.recycle_old_temp(scan, now, fake, None, lambda: False)
    names = {p.name for p in got}
    r.check("8 日前のファイルは渡る", names == {"old.log"} and res.sent == 1 and res.bytes == 10)
    r.check("1 日前のファイルは渡らない", "new.log" not in names)
    r.check("ジャンクションの先は渡らない" + ("" if junction_ok else "(ジャンクションを作れず省略)"), "far.log" not in names)


def _history(r: _Result, tmp: Path) -> None:
    print("履歴は status だけ(INV-3)")
    h = History(tmp / "history.jsonl", ALL_IDS)
    for _ in range(205):
        h.append("perf", {"P1": "bad", "P2": "chrome.exe", "X9": "good"}, 10)
    text = (tmp / "history.jsonl").read_text(encoding="utf-8")
    r.check("最新 200 行に切り詰める", len(text.strip().splitlines()) == 200)
    r.check("status 以外の値・知らない ID は書かない", "chrome.exe" not in text and "X9" not in text)


def run() -> int:
    print("PcCheckup 自己検査")
    r = _Result()
    _thresholds(r)
    _runner(r)
    _report(r)
    with tempfile.TemporaryDirectory(prefix="pccheckup-selftest-") as d:
        _cleanup(r, Path(d))
        _history(r, Path(d))
    print("合格" if r.failed == 0 else f"不合格({r.failed} 件)")
    return 0 if r.failed == 0 else 1
