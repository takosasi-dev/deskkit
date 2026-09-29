# 起動の安全(B1・B2)の追加(PcCheckup_SecureBoot証明書の確認_追加仕様書)。
# SB-AC-1〜4・9 は判定の純粋な関数で、SB-AC-5・7 はモジュールと画面で、SB-AC-6 は grep で、SB-AC-8 は実機(win32_real)で確かめる。
# 読み取り(read_reg)は winreg の偽物で denied / missing / bad_type / 想定外の OSError を確かめる。
from __future__ import annotations

import itertools
import json
import logging
import re
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from deskkit.modules.pccheckup.checks import boot
from deskkit.modules.pccheckup.checks.base import Action, Cancel, Finding
from deskkit.modules.pccheckup.fakes import FakeProbes, sb_raw
from deskkit.modules.pccheckup.probes import REG_DWORD, REG_SZ, RegRead, SecureBootRaw, read_reg, read_secure_boot
from deskkit.modules.pccheckup.runner import BY_CATEGORY, run_category

LOG = logging.getLogger("deskkit.pccheckup.test.boot")
SRC = Path(__file__).resolve().parents[3] / "deskkit" / "modules" / "pccheckup"
BEFORE = date(2026, 10, 18)

OK0, OK1 = RegRead("ok", 0), RegRead("ok", 1)
MISSING, DENIED = RegRead("missing"), RegRead("denied")


def st(s: str) -> RegRead:
    return RegRead("ok", s)


def b1(raw: SecureBootRaw) -> tuple[str, str]:
    f, reason = boot.judge_b1(raw)
    return f.status, reason


def b2(raw: SecureBootRaw, today: date = BEFORE) -> tuple[str, str]:
    reason1 = boot.judge_b1(raw)[1]
    f, reason = boot.judge_b2(raw, reason1, today)
    return f.status, reason


# ------------------------------------------------------------------ SB-AC-1: B1 の表の全行
@pytest.mark.parametrize("fw", ["uefi", "unknown"])
@pytest.mark.parametrize(("sb", "want"), [
    (OK1, ("good", "sb_on")),
    (RegRead("ok", 3), ("good", "sb_on")),
    (OK0, ("info", "sb_off")),
    (DENIED, ("unknown", "sb_denied")),
    (MISSING, ("unknown", "sb_unknown")),
    (RegRead("bad_type", "1"), ("unknown", "sb_unknown")),
])
def test_ac1_b1_table(fw: str, sb: RegRead, want: tuple[str, str]) -> None:
    assert b1(sb_raw(fw, sb=sb)) == want


@pytest.mark.parametrize("sb", [OK1, OK0, DENIED, MISSING, RegRead("bad_type", None)])
def test_ac1_bios_first(sb: RegRead) -> None:
    f, reason = boot.judge_b1(sb_raw("bios", sb=sb))
    assert (f.status, reason, f.value) == ("info", "legacy_bios", "BIOS")
    assert f.actions == ()  # ボタン無し


def test_b1_values_and_buttons() -> None:
    assert boot.judge_b1(sb_raw(sb=OK1))[0].value == "オン"
    off = boot.judge_b1(sb_raw(sb=OK0))[0]
    assert off.value == "オフ" and off.actions == ()
    for sb in (DENIED, MISSING):
        f = boot.judge_b1(sb_raw(sb=sb))[0]
        assert f.value is None and "読めませんでした" in f.title
        assert [a.target for a in f.actions] == [boot.URI_WINDOWS_SECURITY]
    assert "管理者の権限" in boot.judge_b1(sb_raw(sb=DENIED))[0].detail


# ------------------------------------------------------------------ SB-AC-2: B2 の表の全 11 行と順
@pytest.mark.parametrize(("raw", "want"), [
    (sb_raw("bios", status=DENIED), ("info", "cert_skipped_bios")),
    (sb_raw(status=st("Updated"), error=DENIED), ("unknown", "cert_denied")),
    (sb_raw(status=MISSING, error=MISSING, capable=MISSING), ("unknown", "cert_missing")),
    (sb_raw(status=st("Updated"), error=RegRead("ok", 5)), ("unknown", "cert_conflict")),
    (sb_raw(status=st("Updated"), error=OK0), ("good", "cert_updated")),
    (sb_raw(sb=OK0, status=st("NotStarted")), ("info", "cert_sb_off")),
    (sb_raw(status=st("InProgress"), error=RegRead("ok", 0x80070005)), ("warn", "cert_error")),
    (sb_raw(status=st("InProgress"), error=OK0), ("info", "cert_in_progress")),
    (sb_raw(status=st("NotStarted"), error=OK0, capable=OK1), ("warn", "cert_not_started")),
    (sb_raw(status=MISSING, error=MISSING, capable=RegRead("ok", 2)), ("info", "cert_boot_2023")),
    (sb_raw(status=st("Paused"), error=OK0), ("unknown", "cert_unexpected")),
])
def test_ac2_b2_table(raw: SecureBootRaw, want: tuple[str, str]) -> None:
    assert b2(raw) == want


def test_ac2_b2_order_and_normalization() -> None:
    # 先に当てはまる方: Updated かつ error 5 は conflict、sb_off かつ NotStarted は sb_off
    assert b2(sb_raw(status=st("Updated"), error=RegRead("ok", 5)))[1] == "cert_conflict"
    assert b2(sb_raw(sb=OK0, status=st("NotStarted")))[1] == "cert_sb_off"
    # sb_off でも Updated なら good が先
    assert b2(sb_raw(sb=OK0, status=st("Updated")))[1] == "cert_updated"
    # BIOS なら denied より先に skipped
    assert b2(sb_raw("bios", status=DENIED, error=DENIED, capable=DENIED))[1] == "cert_skipped_bios"
    # 前後の空白を除き、大文字小文字を区別しない
    assert b2(sb_raw(status=st("  updated \n")))[1] == "cert_updated"
    assert b2(sb_raw(status=st("NOTSTARTED"), capable=OK0))[1] == "cert_not_started"
    assert b2(sb_raw(status=st("inprogress")))[1] == "cert_in_progress"
    # 型が違う・capable が 0/1 だけ・知らない文字列は unexpected
    assert b2(sb_raw(status=RegRead("bad_type", 3)))[1] == "cert_unexpected"
    assert b2(sb_raw(status=MISSING, error=MISSING, capable=OK1))[1] == "cert_unexpected"
    assert b2(sb_raw(status=MISSING, error=OK0, capable=OK0))[1] == "cert_unexpected"
    # capable だけ denied でも cert_denied
    assert b2(sb_raw(capable=DENIED))[1] == "cert_denied"
    # error が 0 以外でも、status が Updated でなく sb_off なら参考
    assert b2(sb_raw(sb=OK0, status=st("InProgress"), error=RegRead("ok", 1)))[1] == "cert_sb_off"


def test_b2_values_and_actions() -> None:
    want_values = {"cert_updated": "更新済み", "cert_not_started": "まだ古い", "cert_in_progress": "更新中",
                   "cert_error": "うまく進んでいない", "cert_skipped_bios": "対象外", "cert_sb_off": "参考",
                   "cert_boot_2023": "参考"}
    raws = {
        "cert_updated": sb_raw(),
        "cert_not_started": sb_raw(status=st("NotStarted")),
        "cert_in_progress": sb_raw(status=st("InProgress")),
        "cert_error": sb_raw(status=st("NotStarted"), error=RegRead("ok", 9)),
        "cert_skipped_bios": sb_raw("bios"),
        "cert_sb_off": sb_raw(sb=OK0, status=st("NotStarted")),
        "cert_boot_2023": sb_raw(status=MISSING),
        "cert_denied": sb_raw(status=DENIED),
    }
    for reason, raw in raws.items():
        f, got = boot.judge_b2(raw, boot.judge_b1(raw)[1], BEFORE)
        assert got == reason
        assert f.value == want_values.get(reason), reason
        targets = [a.target for a in f.actions if a.button]
        if reason == "cert_updated":
            assert f.actions == ()
        elif reason in ("cert_skipped_bios", "cert_sb_off"):
            assert targets == [boot.URI_WINDOWS_SECURITY]
        else:
            assert targets == [boot.URI_WINDOWS_UPDATE, boot.URI_WINDOWS_SECURITY]
        if f.status != "good":
            texts = " ".join(a.text for a in f.actions)
            assert "Windows セキュリティの方を正としてください" in texts  # B-8
            assert "メーカー" in texts


# ------------------------------------------------------------------ SB-AC-3: 期限の前後
@pytest.mark.parametrize(("today", "before"), [(date(2026, 10, 18), True), (date(2026, 10, 19), False),
                                                (date(2026, 10, 20), False), (date(2027, 1, 1), False)])
def test_ac3_expiry_text(today: date, before: bool) -> None:
    raw = sb_raw(status=st("NotStarted"), error=OK0)
    f, reason = boot.judge_b2(raw, "sb_on", today)
    assert reason == "cert_not_started" and f.status == "warn"
    assert f.detail == (boot.NOT_STARTED_BEFORE if before else boot.NOT_STARTED_AFTER)
    assert ("期限が来ます" in f.detail) is before and ("期限が過ぎました" in f.detail) is (not before)
    assert date(2026, 10, 19) == boot.PCA2011_EXPIRY


# ------------------------------------------------------------------ SB-AC-4・SB-AC-9: 全組み合わせ
SB_VARIANTS = [OK0, OK1, MISSING, DENIED, RegRead("bad_type", "1")]
STATUS_VARIANTS = [st("Updated"), st(" inprogress "), st("NotStarted"), st("Weird"), MISSING, DENIED, RegRead("bad_type", 5)]
ERR_VARIANTS = [OK0, RegRead("ok", 5), MISSING, DENIED, RegRead("bad_type", "x")]
CAP_VARIANTS = [OK0, OK1, RegRead("ok", 2), MISSING, DENIED, RegRead("bad_type", None)]
FORBIDDEN = ("BIOS を開く", "F2", "F12", "既定値に戻す", "AvailableUpdates", "期限切れで起動しなくなる")


def _all_findings() -> list[Finding]:
    out: list[Finding] = []
    for fw, sb, s, e, c in itertools.product(("uefi", "bios", "unknown"), SB_VARIANTS, STATUS_VARIANTS, ERR_VARIANTS,
                                             CAP_VARIANTS):
        raw = sb_raw(fw, sb=sb, status=s, error=e, capable=c)
        f1, r1 = boot.judge_b1(raw)
        for today in (BEFORE, date(2026, 10, 19)):
            out += [f1, boot.judge_b2(raw, r1, today)[0]]
    return out


def _texts(f: Finding) -> list[str]:
    return [f.title, f.detail, f.value or "", *(a.text for a in f.actions), *(a.button or "" for a in f.actions)]


def test_ac4_never_bad_and_reasons_known() -> None:
    fs = _all_findings()
    assert all(f.status != "bad" for f in fs)
    assert {f.status for f in fs} <= {"good", "warn", "info", "unknown"}
    for fw, sb, s, e, c in itertools.product(("uefi", "bios", "unknown"), SB_VARIANTS, STATUS_VARIANTS, ERR_VARIANTS,
                                             CAP_VARIANTS):
        r1, r2 = boot.reasons(sb_raw(fw, sb=sb, status=s, error=e, capable=c), BEFORE)
        assert r1 in boot.B1_REASONS and r2 in boot.B2_REASONS
    # 11 行すべてに届いている
    seen = {boot.reasons(sb_raw(fw, sb=sb, status=s, error=e, capable=c), BEFORE)[1]
            for fw, sb, s, e, c in itertools.product(("uefi", "bios"), SB_VARIANTS, STATUS_VARIANTS, ERR_VARIANTS,
                                                     CAP_VARIANTS)}
    assert seen == set(boot.B2_REASONS)


def test_ac9_no_bios_steps_in_texts(make_module: Any) -> None:
    texts = [t for f in _all_findings() for t in _texts(f)]
    for t in texts:
        for w in FORBIDDEN:
            assert w not in t, (w, t)
    # 開くのは Windows Update と Windows セキュリティだけ(SB-FR-6)
    targets = {a.target for f in _all_findings() for a in f.actions if a.target}
    assert targets == {boot.URI_WINDOWS_UPDATE, boot.URI_WINDOWS_SECURITY}
    # コピーの文字列にも無い
    m, _ctx, _ = make_module(FakeProbes(sb=sb_raw(sb=OK0, status=st("NotStarted"))))
    m.run(["boot"])
    text = m.copy_text()
    for w in FORBIDDEN:
        assert w not in text


# ------------------------------------------------------------------ 読み取り(read_reg・read_secure_boot)
class FakeKey:
    def __enter__(self) -> FakeKey:
        return self

    def __exit__(self, *a: Any) -> None:
        return None


def fake_winreg(values: dict[tuple[str, str], Any]) -> Any:
    """values[(path, name)] は (値, 型) か、出す例外。path だけの例外は ("path", "") に置く。"""
    opened: list[tuple[int, str, int, int]] = []

    def open_key(root: int, path: str, reserved: int, access: int) -> FakeKey:
        opened.append((root, path, reserved, access))
        exc = values.get((path, ""))
        if isinstance(exc, BaseException):
            raise exc
        k = FakeKey()
        k.path = path  # type: ignore[attr-defined]
        return k

    def query(k: Any, name: str) -> tuple[Any, int]:
        v = values.get((k.path, name), FileNotFoundError(2, "no"))
        if isinstance(v, BaseException):
            raise v
        return v  # type: ignore[no-any-return]

    return SimpleNamespace(HKEY_LOCAL_MACHINE=0x80000002, KEY_READ=0x20019, OpenKey=open_key, QueryValueEx=query,
                           opened=opened)


def test_read_reg_kinds() -> None:
    api = fake_winreg({
        ("K", "dw"): (1, REG_DWORD),
        ("K", "sz"): ("Updated", REG_SZ),
        ("K", "wrong"): ("1", REG_SZ),
        ("K", "bin"): (b"\x01", 3),
        ("K", "perm"): PermissionError(13, "denied"),
        ("K", "w5"): OSError(22, "x", None, 5),
        ("D", ""): PermissionError(13, "denied"),
        ("K", "odd"): OSError(22, "x", None, 1234),
    })
    assert read_reg("K", "dw", REG_DWORD, api) == RegRead("ok", 1)
    assert read_reg("K", "sz", REG_SZ, api) == RegRead("ok", "Updated")
    assert read_reg("K", "wrong", REG_DWORD, api) == RegRead("bad_type", "1")
    assert read_reg("K", "dw", REG_SZ, api) == RegRead("bad_type", 1)
    assert read_reg("K", "bin", REG_DWORD, api) == RegRead("bad_type", None)
    assert read_reg("K", "none", REG_DWORD, api) == RegRead("missing")
    assert read_reg("Nope", "x", REG_DWORD, fake_winreg({("Nope", ""): FileNotFoundError(2, "no")})) == RegRead("missing")
    assert read_reg("K", "perm", REG_DWORD, api) == RegRead("denied")
    assert read_reg("K", "w5", REG_DWORD, api) == RegRead("denied")
    assert read_reg("D", "x", REG_DWORD, api) == RegRead("denied")
    with pytest.raises(OSError):
        read_reg("K", "odd", REG_DWORD, api)
    # KEY_READ でだけ開く(SB-INV-1)
    assert {a[3] for a in api.opened} == {0x20019}


def test_read_secure_boot_all_values() -> None:
    from deskkit.modules.pccheckup.probes import SB_SERVICING, SB_STATE

    api = fake_winreg({
        (SB_STATE, "UEFISecureBootEnabled"): (1, REG_DWORD),
        (SB_SERVICING, "UEFICA2023Status"): ("NotStarted", REG_SZ),
        (SB_SERVICING, "WindowsUEFICA2023Capable"): (1, REG_DWORD),
    })
    raw = read_secure_boot(api, lambda: "uefi")
    assert raw == SecureBootRaw("uefi", OK1, st("NotStarted"), MISSING, OK1)

    def fail() -> str:
        raise OSError("x")

    assert read_secure_boot(api, fail).firmware == "unknown"
    assert read_secure_boot(api, lambda: "weird").firmware == "unknown"
    denied = fake_winreg({(SB_SERVICING, ""): PermissionError(13, "d"), (SB_STATE, "UEFISecureBootEnabled"): (1, REG_DWORD)})
    raw2 = read_secure_boot(denied, lambda: "bios")
    assert (raw2.firmware, raw2.status.kind, raw2.error.kind, raw2.capable.kind) == ("bios", "denied", "denied", "denied")


def test_unexpected_oserror_makes_both_unknown(make_module: Any) -> None:
    m, _ctx, _ = make_module(FakeProbes(raise_on={"secure_boot"}))
    assert m.run(["boot"])
    assert {f.check_id: f.status for f in m.state.results["boot"]} == {"B1": "unknown", "B2": "unknown"}
    d = m.diagnostics()
    assert (d["sb_b1"], d["sb_b2"]) == ("error", "error")


# ------------------------------------------------------------------ モジュール(SB-FR-2・SB-AC-5・SB-AC-7)
def test_run_all_order_and_four_history_rows(make_module: Any) -> None:
    m, ctx, _ = make_module()
    assert m.all_categories() == ["perf", "net", "storage", "boot"]
    assert m.run_all()
    rows = [json.loads(x) for x in (ctx.data_dir / "history.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["category"] for r in rows] == ["perf", "net", "storage", "boot"]
    assert rows[-1]["results"] == {"B1": "good", "B2": "good"} and isinstance(rows[-1]["ms"], int)
    assert m.usage(7)[0].per_day[-1] == 4


def test_progress_text(make_module: Any) -> None:
    m, _ctx, _ = make_module()
    seen: list[str] = []
    m.notifier.changed.connect(lambda: seen.append(m.state.progress_text))
    m.run(["boot"])
    assert "起動の設定を読んでいます" in seen


def test_ac5_raw_values_only_in_copy(make_module: Any) -> None:
    raw = sb_raw(sb=RegRead("ok", 97531), status=st("NotStarted"), error=RegRead("ok", 0), capable=RegRead("ok", 1))
    m, ctx, _ = make_module(FakeProbes(sb=raw))
    m.run_all()
    text = m.copy_text()
    assert "■ 起動の安全" in text
    line = next(ln for ln in text.splitlines() if "起動の証明書がまだ古いままです" in ln)
    assert line.startswith("[注意]")
    assert line.endswith("(起動方式=UEFI / UEFISecureBootEnabled=97531 / UEFICA2023Status=NotStarted / UEFICA2023Error=0"
                         " / WindowsUEFICA2023Capable=1)")
    b1_line = next(ln for ln in text.splitlines() if "セキュア ブートは有効です" in ln)
    assert b1_line.endswith("(起動方式=UEFI / UEFISecureBootEnabled=97531)")
    diag = m.diagnostics()
    assert (diag["secureboot_check"], diag["sb_b1"], diag["sb_b2"]) == (True, "sb_on", "cert_not_started")
    blobs = [(ctx.data_dir / "history.jsonl").read_text(encoding="utf-8"), ctx.handler.text(), repr(diag)]
    for b in blobs:
        assert "NotStarted" not in b and "UEFISecureBootEnabled" not in b and "97531" not in b
    assert "b1=sb_on b2=cert_not_started" in ctx.handler.text()


def test_copy_marks_unread_and_missing() -> None:
    raw = sb_raw(sb=DENIED, status=MISSING, error=DENIED, capable=RegRead("bad_type", "2"))
    s = boot.suffixes(raw)
    assert s["B1"] == "(起動方式=UEFI / UEFISecureBootEnabled=読めませんでした)"
    assert s["B2"] == ("(起動方式=UEFI / UEFISecureBootEnabled=読めませんでした / UEFICA2023Status=なし / "
                       "UEFICA2023Error=読めませんでした / WindowsUEFICA2023Capable=2(型が違います))")
    bios = boot.suffixes(sb_raw("bios"))
    assert bios == {"B1": "(起動方式=BIOS)", "B2": "(起動方式=BIOS)"}


def test_ac7_setting_off_hides_everything(make_module: Any, qapp: Any) -> None:
    m, ctx, _ = make_module(section={"secureboot_check": False})
    assert [q.label for q in ctx.quick] == ["PC が重い原因を調べる", "ネットの不調を調べる", "容量を調べる"]
    assert m.all_categories() == list(BY_CATEGORY)
    assert m.run(["boot"]) is False
    m.run_all()
    rows = (ctx.data_dir / "history.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(r)["category"] for r in rows] == ["perf", "net", "storage"]
    page = m.create_page()
    assert not page.boot_row.isVisibleTo(page)
    assert not page.sb_toggle.isChecked()
    assert m.diagnostics()["secureboot_check"] is False
    page.close()


def test_setting_invalid_value_is_fixed(make_module: Any) -> None:
    m, ctx, _ = make_module(section={"watch_disk": False, "secureboot_check": "yes"})
    assert m.secureboot_check is True and ctx.writes[-1]["secureboot_check"] is True


def test_toggle_setting_rebuilds_quick_actions(make_module: Any) -> None:
    m, ctx, _ = make_module()
    cleared: list[int] = []

    def clear() -> None:
        cleared.append(1)
        ctx.quick.clear()

    ctx.clear_quick_actions = clear
    assert m.set_secureboot_check(False) is None
    assert cleared and "起動の証明書を調べる" not in [q.label for q in ctx.quick]
    assert ctx.settings()["secureboot_check"] is False
    assert m.set_secureboot_check(True) is None
    assert [q.label for q in ctx.quick][-1] == "起動の証明書を調べる"
    ctx.fail_write = True
    assert m.set_secureboot_check(False) is not None and m.secureboot_check is True


def test_quick_action_runs_boot(make_module: Any) -> None:
    m, ctx, _ = make_module()
    q = next(q for q in ctx.quick if q.label == "起動の証明書を調べる")
    q.callback()
    assert ctx.shown == 1 and m.state.categories == ["boot"] and m.state.done == {"boot"}


def test_open_action_allows_update_and_security(make_module: Any) -> None:
    m, _ctx, _ = make_module()
    assert m.open_action(Action("", "uri", "ms-settings:windowsupdate", "x"))
    assert m.open_action(Action("", "uri", "ms-settings:windowsdefender", "x"))
    assert not m.open_action(Action("", "uri", "ms-settings:windowsupdate-options", "x"))
    assert m.opened == ["ms-settings:windowsupdate", "ms-settings:windowsdefender"]


def test_history_rejects_raw_values(make_module: Any) -> None:
    m, ctx, _ = make_module()
    m.history.append("boot", {"B1": "good", "B2": "NotStarted", "B3": "good"}, 3)
    text = (ctx.data_dir / "history.jsonl").read_text(encoding="utf-8")
    assert '"B1": "good"' in text and "NotStarted" not in text and "B3" not in text


def test_worse_mark_for_boot(make_module: Any) -> None:
    fp = FakeProbes()
    m, _ctx, _ = make_module(fp)
    m.run(["boot"])
    fp.sb = sb_raw(status=st("NotStarted"))
    m.run(["boot"])
    assert m.state.worse == {"B2"}


# ------------------------------------------------------------------ 画面(SB-FR-1)
def test_page_boot_button_runs_and_card(qapp: Any, make_module: Any) -> None:
    m, _ctx, _ = make_module(FakeProbes(sb=sb_raw(status=st("NotStarted"))))
    page = m.create_page()
    assert page.boot_row.isVisibleTo(page) and page.boot_btn.text().endswith("起動の安全を調べる")
    page.boot_btn.click()
    QApplication.processEvents()
    assert m.state.categories == ["boot"]
    sec = page.sections["boot"]
    assert set(sec.cards) == {"B1", "B2"}
    labels = " ".join(lb.text() for lb in page.findChildren(QLabel))
    assert "起動の証明書がまだ古いままです" in labels and "Windows セキュリティの方を正としてください" in labels
    btns = [b.text() for b in page.findChildren(QPushButton)]
    assert any("Windows Update を開く" in t for t in btns) and any("Windows セキュリティを開く" in t for t in btns)
    card = sec.cards["B2"]
    page.on_action(card.finding.actions[0], page.copy_btn)
    assert m.opened == ["ms-settings:windowsupdate"]
    page.resize(1000, 800)
    page.show()
    QApplication.processEvents()
    assert page.minimumSizeHint().width() <= 900 and page.widget().minimumSizeHint().width() <= 900
    page.close()


def test_page_all_button_includes_boot(qapp: Any, make_module: Any) -> None:
    m, _ctx, _ = make_module()
    page = m.create_page()
    page.all_btn.click()
    assert m.state.categories == ["perf", "net", "storage", "boot"]
    assert set(page.sections) == {"perf", "net", "storage", "boot"}
    page.sb_toggle.click()
    assert m.secureboot_check is False and not page.boot_row.isVisibleTo(page)
    page.close()


# ------------------------------------------------------------------ SB-AC-6・SB-AC-10
def test_ac6_no_write_or_external_process() -> None:
    rx = re.compile(r"SecureBootUEFI|powershell|bcdedit|schtasks|Secure-Boot-Update|AvailableUpdates|"
                    r"SetFirmwareEnvironmentVariable|winreg\.(SetValue|SetValueEx|CreateKey|CreateKeyEx|DeleteKey|DeleteKeyEx|"
                    r"DeleteValue)")
    hits = [f"{p.name}:{i}" for p in SRC.rglob("*.py")
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if rx.search(line)]
    assert hits == []
    # 外部のプロセスも起動しない(SB-INV-2): boot と probes の読み取りに subprocess が無い
    for name in ("checks/boot.py", "probes.py"):
        assert "subprocess" not in (SRC / name).read_text(encoding="utf-8")


def test_ac10_selftest_includes_boot(capsys: pytest.CaptureFixture[str]) -> None:
    from deskkit.modules.pccheckup.selftest import run

    assert run() == 0
    out = capsys.readouterr().out
    assert "B1" in out and "B2" in out


# ------------------------------------------------------------------ SB-AC-8(実機)
@pytest.mark.win32_real
def test_ac8_real_boot_under_2s() -> None:
    from deskkit.modules.pccheckup.probes import RealProbes

    t0 = time.monotonic()
    res = run_category("boot", RealProbes(), Cancel(), LOG)
    took = time.monotonic() - t0
    assert took <= 2.0 and res.ms <= 2000
    assert [f.check_id for f in res.findings] == ["B1", "B2"]
    assert all(f.status in ("good", "warn", "info", "unknown") for f in res.findings)
    assert res.failed == []  # 読めない値があっても例外で止まらない
