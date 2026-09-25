# AC-1: 偽の probes で §6.2〜6.4 の各しきい値の境界(ちょうど・1 つ下)を与えると、表どおりの status になる(全チェック)。
from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from deskkit.modules.pccheckup.checks import net, perf, storage
from deskkit.modules.pccheckup.checks.base import Cancel, Finding, GiB, MiB
from deskkit.modules.pccheckup.fakes import WIFI_ID, FakeProbes, _adapter
from deskkit.modules.pccheckup.probes import (
    COST_FIXED,
    COST_UNKNOWN,
    COST_UNRESTRICTED,
    COST_VARIABLE,
    LEVEL_CONSTRAINED,
    LEVEL_INTERNET,
    LEVEL_LOCAL,
    LEVEL_NONE,
    OVERLAY_BALANCED,
    OVERLAY_BEST_EFFICIENCY,
    OVERLAY_BEST_PERFORMANCE,
    ConnInfo,
    CpuSample,
    DiskInfo,
    FolderSize,
    MemInfo,
    PowerInfo,
    ProcUsage,
    ProxyInfo,
    SizeInfo,
    StartupInfo,
)
from deskkit.modules.pccheckup.runner import ALL_IDS, BY_CATEGORY, run_category

LOG = logging.getLogger("deskkit.pccheckup.test.checks")


def run_one(check_id: str, fp: FakeProbes) -> Finding:
    for chk in BY_CATEGORY[{"P": "perf", "N": "net", "S": "storage"}[check_id[0]]]:
        if chk.check_id == check_id:
            return chk.run(fp, Cancel())
    raise KeyError(check_id)


# ------------------------------------------------------------------ P
@pytest.mark.parametrize(("pct", "want"), [(85.0, "bad"), (84.99, "warn"), (60.0, "warn"), (59.99, "good"), (100.0, "bad"), (0.0, "good")])
def test_p1_cpu(pct: float, want: str) -> None:
    f = run_one("P1", FakeProbes(cpu_value=CpuSample(pct, (ProcUsage("chrome.exe", pct / 2),))))
    assert f.status == want
    if want != "good":
        assert "chrome.exe" in f.actions[0].text and f.actions[0].kind == "taskmgr"


@pytest.mark.parametrize(("pct", "want"), [(90.0, "bad"), (89.99, "warn"), (80.0, "warn"), (79.99, "good")])
def test_p2_memory(pct: float, want: str) -> None:
    f = run_one("P2", FakeProbes(mem=MemInfo(pct, 16 * GiB, (ProcUsage("chrome.exe", 3.1 * GiB),))))
    assert f.status == want
    assert "chrome.exe" in f.detail and "3.1GB" in f.detail


@pytest.mark.parametrize(("total", "free", "want"), [
    (1000 * GiB, 50 * GiB, "warn"),         # ちょうど 5% → bad でない(10% 未満で warn)
    (1000 * GiB, 50 * GiB - 1, "bad"),       # 5% 未満
    (1000 * GiB, 100 * GiB, "good"),        # ちょうど 10% かつ 15GB 以上
    (1000 * GiB, 100 * GiB - 1, "warn"),
    (40 * GiB, 5 * GiB, "warn"),             # ちょうど 5GB → bad でない(15GB 未満で warn)
    (40 * GiB, 5 * GiB - 1, "bad"),
    (100 * GiB, 15 * GiB, "good"),           # ちょうど 15GB・15%
    (100 * GiB, 15 * GiB - 1, "warn"),
    (0, 0, "unknown"),
])
def test_p3_and_s1_disk(total: int, free: int, want: str) -> None:
    d = DiskInfo("C:\\", total, free)
    assert run_one("P3", FakeProbes(sysdrive=d)).status == want
    if total:
        s1 = run_one("S1", FakeProbes(drives=[DiskInfo("D:\\", 1000 * GiB, 900 * GiB), d]))
        assert s1.status == want
        assert len(s1.rows) == 2


def test_p3_action_leads_to_storage() -> None:
    f = run_one("P3", FakeProbes(sysdrive=DiskInfo("C:\\", 100 * GiB, 1 * GiB)))
    assert f.actions[0].kind == "category" and f.actions[0].target == "storage"


@pytest.mark.parametrize(("sec", "want"), [(7 * 86400, "warn"), (7 * 86400 - 1, "good"), (30 * 86400, "warn")])
def test_p4_uptime(sec: float, want: str) -> None:
    f = run_one("P4", FakeProbes(uptime=sec))
    assert f.status == want
    if want == "warn":
        assert "再起動" in f.actions[0].text and "シャットダウン" in f.actions[0].text


@pytest.mark.parametrize(("n", "want"), [(25, "bad"), (24, "warn"), (15, "warn"), (14, "good")])
def test_p5_startup(n: int, want: str) -> None:
    f = run_one("P5", FakeProbes(startup_info=StartupInfo(n, 3)))
    assert f.status == want
    assert f.actions[0].target == "ms-settings:startupapps"


@pytest.mark.parametrize(("power", "want"), [
    (PowerInfo(True, True, True, OVERLAY_BALANCED, 40), "warn"),          # 節約機能オン
    (PowerInfo(True, False, False, OVERLAY_BEST_EFFICIENCY, 90), "warn"),  # 最適な電力効率
    (PowerInfo(True, True, False, OVERLAY_BALANCED, 40), "good"),         # バッテリー駆動だけでは注意にしない
    (PowerInfo(True, False, False, OVERLAY_BEST_PERFORMANCE, 90), "good"),
    (PowerInfo(False, False, True, OVERLAY_BALANCED), "good"),            # バッテリーの無い PC は電源モードだけで判定
    (PowerInfo(False, False, False, OVERLAY_BEST_EFFICIENCY), "warn"),
    (PowerInfo(False, False, False, None), "good"),
])
def test_p6_power(power: PowerInfo, want: str) -> None:
    f = run_one("P6", FakeProbes(power_info=power))
    assert f.status == want
    if want == "warn":
        assert any(a.target == "ms-settings:powersleep" for a in f.actions)
        assert any("バランス" in a.text for a in f.actions)
        assert any("電源につな" in a.text for a in f.actions) == power.on_battery


@pytest.mark.parametrize(("pending", "want"), [(True, "warn"), (False, "good")])
def test_p7_reboot(pending: bool, want: str) -> None:
    assert run_one("P7", FakeProbes(reboot=pending)).status == want


# ------------------------------------------------------------------ N
@pytest.mark.parametrize(("conn", "want", "word"), [
    (ConnInfo(False), "bad", "どこにも"),
    (ConnInfo(True, LEVEL_NONE), "bad", "どこにも"),
    (ConnInfo(True, LEVEL_LOCAL), "bad", "ルーター"),
    (ConnInfo(True, LEVEL_CONSTRAINED), "warn", "ログイン"),
    (ConnInfo(True, LEVEL_INTERNET), "good", "インターネット"),
])
def test_n1_connectivity(conn: ConnInfo, want: str, word: str) -> None:
    f = run_one("N1", FakeProbes(conn=conn))
    assert f.status == want
    assert word in f.title + f.detail
    if want != "good":
        assert f.actions[0].target == "ms-settings:network-status"


@pytest.mark.parametrize(("adapter", "want"), [
    (replace(_adapter(), ipv4=("169.254.10.2",)), "bad"),
    (replace(_adapter(), ipv4=("169.253.255.255",)), "good"),
    (replace(_adapter(), gateways=()), "bad"),
    (replace(_adapter(), dns=()), "warn"),
    (_adapter(), "good"),
])
def test_n2_addresses(adapter: object, want: str) -> None:
    f = run_one("N2", FakeProbes(adapter_list=[adapter]))  # type: ignore[list-item]
    assert f.status == want


def test_n2_picks_profile_adapter_then_lowest_metric() -> None:
    vpn = replace(_adapter(), adapter_id="vpn", metric=5, ipv4=("169.254.1.1",))
    wifi = _adapter()
    # N1 のプロファイルのアダプタ(Wi-Fi)を見る
    assert run_one("N2", FakeProbes(adapter_list=[vpn, wifi])).status == "good"
    # 対応が取れなければ、ゲートウェイを持つメトリック最小(VPN)
    fp = FakeProbes(adapter_list=[vpn, wifi], conn=ConnInfo(True, LEVEL_INTERNET, True, 4, 100.0, "nomatch"))
    assert run_one("N2", fp).status == "bad"
    # winrt が読めなくても続ける(§10)
    fp = FakeProbes(adapter_list=[replace(wifi, metric=50), replace(vpn, ipv4=("10.0.0.2",))], raise_on={"connectivity"})
    assert run_one("N2", fp).value == "10.0.0.2"
    assert net.pick_adapter([], None) is None
    assert run_one("N2", FakeProbes(adapter_list=[])).status == "bad"


@pytest.mark.parametrize(("bars", "mbps", "want"), [
    (1, 100.0, "bad"), (0, 100.0, "bad"), (2, 100.0, "warn"), (3, 100.0, "good"),
    (5, 20.0, "good"), (5, 19.9, "warn"), (1, 5.0, "bad"), (None, None, "unknown"), (None, 50.0, "good"),
])
def test_n3_signal(bars: int | None, mbps: float | None, want: str) -> None:
    fp = FakeProbes(conn=ConnInfo(True, LEVEL_INTERNET, True, bars, mbps, "nomatch"), adapter_list=[])
    assert run_one("N3", fp).status == want


def test_n3_wired_and_link_fallback() -> None:
    f = run_one("N3", FakeProbes(conn=ConnInfo(True, LEVEL_INTERNET, False, None, 1000.0, WIFI_ID)))
    assert f.status == "info" and "有線" in f.title
    # winrt のリンク速度が無ければ GetAdaptersAddresses の値を使う
    slow = replace(_adapter(), link_bps=10_000_000)
    f = run_one("N3", FakeProbes(conn=ConnInfo(True, LEVEL_INTERNET, True, 4, None, WIFI_ID), adapter_list=[slow]))
    assert f.status == "warn"
    assert run_one("N3", FakeProbes(conn=ConnInfo(False))).status == "info"


@pytest.mark.parametrize(("cost", "want"), [(COST_FIXED, "info"), (COST_VARIABLE, "info"), (COST_UNRESTRICTED, "good"),
                                             (COST_UNKNOWN, "unknown")])
def test_n4_cost(cost: int, want: str) -> None:
    f = run_one("N4", FakeProbes(conn=ConnInfo(True, LEVEL_INTERNET, cost_type=cost)))
    assert f.status == want
    if want == "info":
        assert "従量制接続に設定されています" in f.detail and f.actions[0].target == "ms-settings:network-wifi"


@pytest.mark.parametrize(("p", "want"), [(ProxyInfo(True, False), "info"), (ProxyInfo(False, True), "info"),
                                         (ProxyInfo(True, True), "info"), (ProxyInfo(False, False), "good")])
def test_n5_proxy(p: ProxyInfo, want: str) -> None:
    f = run_one("N5", FakeProbes(proxy_info=p))
    assert f.status == want
    if want == "info":
        assert f.actions[0].target == "ms-settings:network-proxy"


# ------------------------------------------------------------------ S
@pytest.mark.parametrize(("n", "want"), [(GiB, "info"), (GiB - 1, "good"), (0, "good")])
def test_s2_recycle(n: int, want: str) -> None:
    f = run_one("S2", FakeProbes(recycle=n))
    assert f.status == want
    if want == "info":
        assert "1.0GB 空きます" in f.title and f.actions[0].target == "shell:RecycleBinFolder"


@pytest.mark.parametrize(("n", "want"), [(GiB, "warn"), (GiB - 1, "info"), (100 * MiB, "info"), (100 * MiB - 1, "good")])
def test_s3_temp(n: int, want: str) -> None:
    f = run_one("S3", FakeProbes(temp=SizeInfo(n, 4)))
    assert f.status == want
    assert f.actions[0].kind == "cleanup"


def test_s3_partial_and_empty() -> None:
    f = run_one("S3", FakeProbes(temp=SizeInfo(0, 0, denied=True, partial=True)))
    assert f.status == "good" and not f.actions and "途中まで" in (f.value or "")


@pytest.mark.parametrize(("n", "want"), [(10 * GiB, "info"), (10 * GiB - 1, "good")])
def test_s4_downloads(n: int, want: str) -> None:
    f = run_one("S4", FakeProbes(downloads_size=SizeInfo(n, 10)))
    assert f.status == want
    if want == "info":
        assert f.actions[0].kind == "folder" and f.actions[0].target == "C:\\Users\\someone\\Downloads"


def test_s5_folders_top10_and_marks() -> None:
    folders = [FolderSize(f"F{i:02d}", f"C:\\Users\\x\\F{i:02d}", SizeInfo(i * GiB, i)) for i in range(15)]
    folders.append(FolderSize("AppData", "C:\\Users\\x\\AppData", SizeInfo(99 * GiB, 1, denied=True)))
    folders.append(FolderSize("Late", "C:\\Users\\x\\Late", SizeInfo(0, 0, partial=True)))
    f = run_one("S5", FakeProbes(folders=folders))
    assert f.status == "info"
    assert len(f.rows) == 10
    assert f.rows[0].label == "AppData" and f.rows[0].note == "一部読めませんでした"
    assert [r.label for r in f.rows[1:4]] == ["F14", "F13", "F12"]
    assert all(r.path for r in f.rows)
    assert "途中まで" in f.title and "60 秒" in f.detail


@pytest.mark.parametrize(("on", "want"), [(True, "good"), (False, "info")])
def test_s6_storage_sense(on: bool, want: str) -> None:
    f = run_one("S6", FakeProbes(sense=on))
    assert f.status == want
    if want == "info":
        assert f.actions[0].target == "ms-settings:storagesense"


# ------------------------------------------------------------------ 全体
def test_all_checks_listed_and_defaults_good() -> None:
    assert ALL_IDS == ("P1", "P2", "P3", "P4", "P5", "P6", "P7", "N1", "N2", "N3", "N4", "N5",
                       "S1", "S2", "S6", "S3", "S4", "S5")
    fp = FakeProbes()
    for cat in BY_CATEGORY:
        res = run_category(cat, fp, Cancel(), LOG)
        assert all(f.status in ("good", "info") for f in res.findings), [(f.check_id, f.status) for f in res.findings]


def test_storage_runs_s5_last() -> None:
    fp = FakeProbes()
    run_category("storage", fp, Cancel(), LOG)
    assert fp.calls[-1] == "user_folders"


def test_thresholds_are_constants() -> None:
    # P-2: しきい値はコードの定数(設定では変えない)
    assert (perf.CPU_BAD, perf.CPU_WARN, perf.MEM_BAD, perf.MEM_WARN) == (85.0, 60.0, 90.0, 80.0)
    assert (perf.STARTUP_WARN, perf.STARTUP_BAD) == (15, 25)
    assert (net.BARS_BAD, net.BARS_WARN, net.LINK_WARN_MBPS) == (1, 2, 20.0)
    assert (GiB, GiB, 100 * MiB, 10 * GiB) == (storage.RECYCLE_INFO, storage.TEMP_WARN, storage.TEMP_INFO, storage.DOWNLOADS_INFO)
