# 構成シグネチャ(§9.3)の単体テスト。
from __future__ import annotations

from deskkit.modules.layoutkeep import monitors
from deskkit.modules.layoutkeep.fake_win32 import fake_monitor

A = fake_monitor("\\\\.\\DISPLAY1", (0, 0, 1920, 1080), primary=True, dpi=96)
B = fake_monitor("\\\\.\\DISPLAY2", (1920, 0, 4480, 1440), dpi=144)


def test_stable_and_order_independent() -> None:
    s1 = monitors.compute([A, B], "device_interface").signature
    s2 = monitors.compute([B, A], "device_interface").signature
    assert s1 is not None and s1 == s2 and len(s1) == 12


def test_missing_id_gives_null() -> None:
    bad = fake_monitor("\\\\.\\DISPLAY3", (0, 0, 800, 600), primary=True, mid=None)
    r = monitors.compute([bad], "device_interface")
    assert r.signature is None and r.reason is not None and "DISPLAY3" in r.reason


def test_no_primary_gives_null() -> None:
    assert monitors.compute([B], "device_interface").signature is None


def test_dpi_and_resolution_change_signature() -> None:
    base = monitors.compute([A, B], "device_interface").signature
    b2 = fake_monitor("\\\\.\\DISPLAY2", (1920, 0, 4480, 1440), dpi=120)
    b3 = fake_monitor("\\\\.\\DISPLAY2", (1920, 0, 3840, 1080), dpi=144)
    assert monitors.compute([A, b2], "device_interface").signature != base
    assert monitors.compute([A, b3], "device_interface").signature != base


def test_id_source_changes_signature() -> None:
    assert monitors.compute([A, B], "edid").signature != monitors.compute([A, B], "device_interface").signature


def test_fields_subset_ignores_dpi() -> None:
    f = ("id", "w", "h", "x", "y", "primary")
    b2 = fake_monitor("\\\\.\\DISPLAY2", (1920, 0, 4480, 1440), dpi=120)
    assert (monitors.compute([A, B], "device_interface", f).signature
            == monitors.compute([A, b2], "device_interface", f).signature)


def test_positions_relative_to_primary() -> None:
    p = fake_monitor("\\\\.\\DISPLAY1", (1920, 0, 3840, 1080), primary=True)
    q = fake_monitor("\\\\.\\DISPLAY2", (0, 0, 1920, 1080))
    norm, _ = monitors.normalize([p, q], "device_interface")
    assert norm is not None
    assert sorted(d["x"] for d in norm) == [-1920, 0]
