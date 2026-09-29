# 「起動の安全」の診断 B1(セキュア ブートが有効か)・B2(起動の証明書が 2023 年版に替わったか)。追加仕様書 §6.2・§6.3。
# SecureBootRaw を受け取り (Finding, 理由コード) を返す純粋な関数。いちばん悪くて warn(B-4)。BIOS の設定・値の書き換えの手順は
# 画面にもコピーにも出さない(B-6・SB-INV-4)。案内は Windows Update・Windows セキュリティ・メーカーの案内の3つだけ。
from __future__ import annotations

from datetime import date

from deskkit.modules.pccheckup.checks.base import Action, Cancel, Check, Finding, Status
from deskkit.modules.pccheckup.probes import Probes, RegRead, SecureBootRaw

# B-5: 画面に出す期限は Microsoft Windows Production PCA 2011 の日付だけ(文書 A)
PCA2011_EXPIRY = date(2026, 10, 19)

URI_WINDOWS_UPDATE = "ms-settings:windowsupdate"
URI_WINDOWS_SECURITY = "ms-settings:windowsdefender"

# 理由コード(§6.2 の表の英字。diagnostics にはこれだけを入れる。SB-FR-8)
B1_REASONS = ("legacy_bios", "sb_on", "sb_off", "sb_denied", "sb_unknown")
B2_REASONS = ("cert_skipped_bios", "cert_denied", "cert_missing", "cert_conflict", "cert_updated", "cert_sb_off",
              "cert_error", "cert_in_progress", "cert_not_started", "cert_boot_2023", "cert_unexpected")

ACT_UPDATE = Action("Windows Update で更新を確かめてください。再起動を求められたら再起動してください。", "uri",
                    URI_WINDOWS_UPDATE, "Windows Update を開く")
ACT_SECURITY = Action("Windows セキュリティの『デバイス セキュリティ』でも確かめられます。"
                      "表示が違うときは、Windows セキュリティの方を正としてください。", "uri", URI_WINDOWS_SECURITY,
                      "Windows セキュリティを開く")
ACT_MAKER = Action("何度更新しても変わらないときは、PC のメーカーのサポートの案内を見てください。")
ACT_SECURITY_ONLY = Action("Windows セキュリティの『デバイス セキュリティ』で確かめられます。", "uri", URI_WINDOWS_SECURITY,
                           "Windows セキュリティを開く")

DENIED_DETAIL = "管理者の権限が要る場所でした。PcCheckup は管理者の権限を求めません。"

NOT_STARTED_BEFORE = ("PC は今までどおり使えます。Windows の起動に使う古い証明書は、2026 年 10 月 19 日に期限が来ます。"
                      "期限が来ても PC は起動し、ふだんの更新も届きます。ただ、起動のしくみを守る新しい対策が届かなくなります。")
NOT_STARTED_AFTER = ("PC は今までどおり使えます。Windows の起動に使う古い証明書は、2026 年 10 月 19 日に期限が過ぎました。"
                     "PC は起動し、ふだんの更新も届きますが、起動のしくみを守る新しい対策は届きません。")
CERT_ERROR_DETAIL = "PC は今までどおり使えます。Windows が証明書を新しくしようとしましたが、うまく進んでいません。"


def _int(r: RegRead) -> int | None:
    return r.value if r.kind == "ok" and isinstance(r.value, int) else None


def _status_word(r: RegRead) -> str | None:
    """Status を比べる形に(前後の空白を除き、小文字)。ok の文字列でなければ None。"""
    return r.value.strip().lower() if r.kind == "ok" and isinstance(r.value, str) else None


# ------------------------------------------------------------------ B1
def judge_b1(raw: SecureBootRaw) -> tuple[Finding, str]:
    """上から順に、最初に当てはまった行で決める(§6.2 の B1 の表)。"""
    if raw.firmware == "bios":
        return Finding("B1", "info", "この PC は古い起動方式(BIOS)で動いています",
                       "この起動方式では、セキュア ブートは使われていません。", (), "BIOS"), "legacy_bios"
    v = _int(raw.sb_enabled)
    if v is not None and v != 0:
        return Finding("B1", "good", "セキュア ブートは有効です",
                       "Windows の起動を守るしくみ(セキュア ブート)が働いています。", (), "オン"), "sb_on"
    if v is not None:
        return Finding("B1", "info", "セキュア ブートはオフです",
                       "PC はこのまま使えます。オンにするかどうかは、PC のメーカーの案内を見てください。"
                       "BIOS の設定は誤ると起動に困ることがあるため、ここでは手順を案内しません。", (), "オフ"), "sb_off"
    if raw.sb_enabled.kind == "denied":
        return Finding("B1", "unknown", "セキュア ブートの状態を読めませんでした", DENIED_DETAIL,
                       (ACT_SECURITY_ONLY,)), "sb_denied"
    return Finding("B1", "unknown", "セキュア ブートの状態を読めませんでした",
                   "Windows がセキュア ブートの状態を記録していませんでした。", (ACT_SECURITY_ONLY,)), "sb_unknown"


# ------------------------------------------------------------------ B2
def _actions(reason: str, status: str) -> tuple[Action, ...]:
    """§6.3: warn・unknown・info の action。1 は cert_skipped_bios と cert_sb_off には出さない。good は無し。"""
    if status == "good":
        return ()
    first = () if reason in ("cert_skipped_bios", "cert_sb_off") else (ACT_UPDATE,)
    return (*first, ACT_SECURITY, ACT_MAKER)


def _b2(reason: str, status: Status, title: str, detail: str, value: str | None) -> tuple[Finding, str]:
    return Finding("B2", status, title, detail, _actions(reason, status), value if status != "unknown" else None), reason


def judge_b2(raw: SecureBootRaw, b1_reason: str, today: date) -> tuple[Finding, str]:
    """上から順に、最初に当てはまった行で決める(§6.2 の B2 の表)。today は SB-FR-9 の文の切り替えだけに使う(B-5)。"""
    if b1_reason == "legacy_bios":
        return _b2("cert_skipped_bios", "info", "この起動方式では調べません",
                   "古い起動方式(BIOS)の PC では、起動の証明書の状態を調べていません。", "対象外")
    reads = (raw.status, raw.error, raw.capable)
    if any(r.kind == "denied" for r in reads):
        return _b2("cert_denied", "unknown", "起動の証明書の状態を読めませんでした", DENIED_DETAIL, None)
    if all(r.kind == "missing" for r in reads):
        return _b2("cert_missing", "unknown", "起動の証明書の状態が分かりませんでした",
                   "Windows が起動の証明書の更新の状態を記録していませんでした。", None)
    word = _status_word(raw.status)
    err = _int(raw.error)
    err_nonzero = err is not None and err != 0
    if word == "updated" and err_nonzero:
        return _b2("cert_conflict", "unknown", "起動の証明書の状態が食い違っています",
                   "Windows の記録に「更新済み」と「失敗」の両方が残っています。", None)
    if word == "updated":
        return _b2("cert_updated", "good", "起動の証明書は新しいもの(2023 年版)に更新済みです",
                   "何もしなくて大丈夫です。", "更新済み")
    if b1_reason == "sb_off":
        return _b2("cert_sb_off", "info", "セキュア ブートがオフのため、参考として示します",
                   "起動の証明書は、まだ新しいものに替わった記録がありません。", "参考")
    if err_nonzero:
        return _b2("cert_error", "warn", "起動の証明書の更新がうまく進んでいません", CERT_ERROR_DETAIL, "うまく進んでいない")
    if word == "inprogress":
        return _b2("cert_in_progress", "info", "起動の証明書を更新している途中です",
                   "Windows が自動で進めています。再起動を求められたら再起動してください。", "更新中")
    if word == "notstarted":
        detail = NOT_STARTED_BEFORE if today < PCA2011_EXPIRY else NOT_STARTED_AFTER
        return _b2("cert_not_started", "warn", "起動の証明書がまだ古いままです", detail, "まだ古い")
    if raw.status.kind == "missing" and _int(raw.capable) == 2:
        return _b2("cert_boot_2023", "info", "新しい証明書で起動しています", "更新の記録は見つかりませんでした。", "参考")
    return _b2("cert_unexpected", "unknown", "起動の証明書の状態が分かりませんでした",
               "Windows の記録が、PcCheckup の知らない形でした。", None)


def reasons(raw: SecureBootRaw, today: date) -> tuple[str, str]:
    b1 = judge_b1(raw)[1]
    return b1, judge_b2(raw, b1, today)[1]


# ------------------------------------------------------------------ コピーに付ける生の値(SB-FR-5)
FIRMWARE_TEXT = {"uefi": "UEFI", "bios": "BIOS", "unknown": "不明"}


def _raw_value(r: RegRead) -> str:
    if r.kind == "ok":
        return str(r.value)
    if r.kind == "missing":
        return "なし"
    if r.kind == "denied":
        return "読めませんでした"
    return f"{r.value}(型が違います)" if r.value is not None else "型が違います"


def raw_suffix(raw: SecureBootRaw, check_id: str, b1_reason: str) -> str:
    """「結果をコピー」の B1・B2 の行の末尾に付ける文字列。例: (起動方式=UEFI / UEFISecureBootEnabled=1 / ...)"""
    parts = [f"起動方式={FIRMWARE_TEXT.get(raw.firmware, '不明')}"]
    if b1_reason != "legacy_bios":  # BIOS の PC はほかの値を使わない(cert_skipped_bios)
        parts.append(f"UEFISecureBootEnabled={_raw_value(raw.sb_enabled)}")
        if check_id == "B2":
            parts += [f"UEFICA2023Status={_raw_value(raw.status)}", f"UEFICA2023Error={_raw_value(raw.error)}",
                      f"WindowsUEFICA2023Capable={_raw_value(raw.capable)}"]
    return "(" + " / ".join(parts) + ")"


def suffixes(raw: SecureBootRaw) -> dict[str, str]:
    b1 = judge_b1(raw)[1]
    return {"B1": raw_suffix(raw, "B1", b1), "B2": raw_suffix(raw, "B2", b1)}


# ------------------------------------------------------------------ 実行
def today() -> date:
    """PC の時計の今日(テストでは差し替える)。"""
    return date.today()


def _run_b1(p: Probes, _c: Cancel) -> Finding:
    return judge_b1(p.secure_boot())[0]


def _run_b2(p: Probes, _c: Cancel) -> Finding:
    raw = p.secure_boot()
    return judge_b2(raw, judge_b1(raw)[1], today())[0]


PROGRESS = "起動の設定を読んでいます"
CHECKS: tuple[Check, ...] = (
    Check("B1", "boot", PROGRESS, _run_b1),
    Check("B2", "boot", PROGRESS, _run_b2),
)
TITLES: dict[str, str] = {"B1": "セキュア ブート", "B2": "起動の証明書"}
IDS: tuple[str, ...] = tuple(c.check_id for c in CHECKS)
