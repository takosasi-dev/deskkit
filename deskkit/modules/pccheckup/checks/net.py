# 「ネット」の診断 N1〜N5(§6.3)。Windows が持っている接続の判定・アダプタの設定・レジストリを読むだけで、通信しない(INV-1)。
# N2・N3 は N1 のインターネット接続プロファイルのアダプタを見る。対応が取れなければ、既定のゲートウェイを持つ
# アダプタのうちメトリックが最小のものを見る(§10)。SSID は Finding に入れない(INV-3・P-4)。
from __future__ import annotations

from deskkit.modules.pccheckup._win32 import IF_TYPE_SOFTWARE_LOOPBACK, AdapterInfo
from deskkit.modules.pccheckup.checks.base import Action, Cancel, Check, Finding, Status, worst
from deskkit.modules.pccheckup.probes import (
    COST_FIXED,
    COST_UNRESTRICTED,
    COST_VARIABLE,
    LEVEL_CONSTRAINED,
    LEVEL_INTERNET,
    LEVEL_LOCAL,
    ConnInfo,
    Probes,
    ProxyInfo,
)

# ---- しきい値(P-2)
BARS_BAD = 1          # 電波がこれ以下で bad
BARS_WARN = 2         # これちょうどで warn
LINK_WARN_MBPS = 20.0  # リンク速度がこれ未満で warn
APIPA_PREFIX = "169.254."

NET_STATUS = Action("", "uri", "ms-settings:network-status", "ネットワークの状態を開く")


def _status_btn(text: str) -> Action:
    return Action(text, NET_STATUS.kind, NET_STATUS.target, NET_STATUS.button)


# ------------------------------------------------------------------ N1 接続の判定
def judge_connectivity(c: ConnInfo) -> Finding:
    if not c.has_profile or c.level not in (LEVEL_LOCAL, LEVEL_CONSTRAINED, LEVEL_INTERNET):
        return Finding("N1", "bad", "どこにもつながっていません",
                       "Windows は、この PC がどのネットワークにもつながっていないと判断しています。",
                       (_status_btn("Wi-Fi をつなぎ直すか、ケーブルを確かめてください。"),), "つながっていません")
    if c.level == LEVEL_LOCAL:
        return Finding("N1", "bad", "インターネットに出られません",
                       "ルーターまではつながっていますが、インターネットに出られません。",
                       (_status_btn("ルーター(と、あればモデム)の電源を入れ直してください。"),), "ルーターまで")
    if c.level == LEVEL_CONSTRAINED:
        return Finding("N1", "warn", "ログイン画面が必要なネットワークです",
                       "ホテル・カフェ・駅などの Wi-Fi で、ブラウザでのログイン(同意)が済んでいないようです。",
                       (_status_btn("ブラウザを開いて、表示されるログイン画面で手続きをしてください。"),), "ログイン待ち")
    return Finding("N1", "good", "インターネットにつながっています",
                   "Windows は、インターネットに出られると判断しています。", (), "つながっています")


# ------------------------------------------------------------------ N2 IP・ゲートウェイ・DNS
def pick_adapter(adapters: list[AdapterInfo], adapter_id: str | None) -> AdapterInfo | None:
    if adapter_id:
        want = adapter_id.strip("{}").lower()
        for a in adapters:
            if a.adapter_id == want:
                return a
    cand = [a for a in adapters if a.up and a.gateways and a.if_type != IF_TYPE_SOFTWARE_LOOPBACK]
    if not cand:
        return None
    return min(cand, key=lambda a: a.metric)


def judge_addresses(a: AdapterInfo | None) -> Finding:
    router = Action("ルーターの電源を入れ直してください。")
    cable = Action("有線なら、LAN ケーブルを抜いて差し直してください。")
    if a is None:
        return Finding("N2", "bad", "使えるネットワークの接続がありません",
                       "ルーターにつながっている接続(アダプタ)が見つかりませんでした。",
                       (_status_btn("Wi-Fi をオンにするか、ケーブルを確かめてください。"), router), "なし")
    real_v4 = [ip for ip in a.ipv4 if not ip.startswith(APIPA_PREFIX)]
    if a.ipv4 and not real_v4:
        return Finding("N2", "bad", "ルーターから住所(IP アドレス)をもらえていません",
                       "PC が自分で仮の住所(169.254.x.x)を付けています。ルーターと正しくやり取りできていません。",
                       (router, cable), a.ipv4[0])
    if not a.gateways:
        return Finding("N2", "bad", "ルーターの場所(既定のゲートウェイ)がわかりません",
                       "この接続には、外へ出るための入口(既定のゲートウェイ)がありません。", (router, cable),
                       real_v4[0] if real_v4 else None)
    if not a.dns:
        return Finding("N2", "warn", "DNS サーバーが設定されていません",
                       "サイトの名前を住所に直すサーバー(DNS)が設定されていないため、ページが開けないことがあります。",
                       (router, _status_btn("ネットワークの設定で、DNS が自動になっているか確かめてください。")),
                       real_v4[0] if real_v4 else None)
    return Finding("N2", "good", "IP アドレス・ゲートウェイ・DNS がそろっています",
                   "ルーターから住所をもらえていて、外への入口と DNS も設定されています。", (),
                   real_v4[0] if real_v4 else "IPv6")


# ------------------------------------------------------------------ N3 Wi-Fi の電波とリンク速度
def judge_signal(c: ConnInfo, a: AdapterInfo | None) -> Finding:
    if not c.has_profile:
        return Finding("N3", "info", "つながっていないため、電波は調べていません",
                       "インターネットへの接続が無いため、電波の強さは判定していません。", (), None)
    if not c.is_wireless:
        return Finding("N3", "info", "有線でつながっています", "有線の接続なので、電波の強さは判定していません。", (), "有線")
    mbps = c.link_mbps
    if mbps is None and a is not None and a.link_bps > 0:
        mbps = a.link_bps / 1_000_000
    statuses: list[Status] = []
    reasons: list[str] = []
    if c.signal_bars is not None:
        b = c.signal_bars
        if b <= BARS_BAD:
            statuses.append("bad")
            reasons.append(f"Wi-Fi の電波がとても弱いです(5 本中 {b} 本)。")
        elif b == BARS_WARN:
            statuses.append("warn")
            reasons.append(f"Wi-Fi の電波が弱めです(5 本中 {b} 本)。")
        else:
            statuses.append("good")
    if mbps is not None:
        if mbps < LINK_WARN_MBPS:
            statuses.append("warn")
            reasons.append(f"ルーターとのつながる速さ(リンク速度)が {mbps:.0f}Mbps と遅めです。")
        else:
            statuses.append("good")
    if not statuses:
        return Finding("N3", "unknown", "Wi-Fi の電波を読めませんでした", "電波の強さとリンク速度を読めませんでした。")
    status = worst(*statuses)
    parts = []
    if c.signal_bars is not None:
        parts.append(f"電波 {c.signal_bars}/5")
    if mbps is not None:
        parts.append(f"{mbps:.0f}Mbps")
    value = "・".join(parts)
    if status == "good":
        return Finding("N3", "good", "Wi-Fi の電波は十分です", "電波の強さも、ルーターとのつながる速さも十分です。", (), value)
    title = "Wi-Fi の電波がとても弱いです" if status == "bad" else "Wi-Fi のつながり方が弱めです"
    acts = (Action("ルーターに近づいてください。"), Action("ルーターとの間の壁や、電子レンジなどの家電を避けてください。"))
    return Finding("N3", status, title, "".join(reasons), acts, value)


# ------------------------------------------------------------------ N4 従量制課金
def judge_cost(c: ConnInfo) -> Finding:
    btn = Action("", "uri", "ms-settings:network-wifi", "Wi-Fi の設定を開く")
    if not c.has_profile:
        return Finding("N4", "info", "つながっていないため、従量制の設定は調べていません",
                       "インターネットへの接続が無いため、判定していません。", (), None)
    if c.cost_type in (COST_FIXED, COST_VARIABLE):
        return Finding("N4", "info", "従量制接続に設定されています",
                       "従量制接続に設定されています。更新や同期が止まることがあります。",
                       (Action("心当たりが無ければ、接続のプロパティで『従量制課金接続』をオフにしてください。",
                               btn.kind, btn.target, btn.button),), "従量制")
    if c.cost_type == COST_UNRESTRICTED:
        return Finding("N4", "good", "従量制接続ではありません", "通信量で更新や同期が止まる設定にはなっていません。", (), "定額")
    return Finding("N4", "unknown", "従量制の設定を読めませんでした", "Windows が接続の料金の種類を返しませんでした。")


# ------------------------------------------------------------------ N5 プロキシ
def judge_proxy(p: ProxyInfo) -> Finding:
    if p.enabled or p.auto_config:
        kind = "手動のプロキシ" if p.enabled else "自動構成スクリプト"
        if p.enabled and p.auto_config:
            kind = "手動のプロキシと自動構成スクリプト"
        return Finding("N5", "info", "プロキシが設定されています",
                       f"{kind}が設定されています。心当たりが無ければ確認してください。",
                       (Action("会社・学校の PC でなければ、設定を確かめてください。", "uri", "ms-settings:network-proxy",
                               "プロキシの設定を開く"),), "設定あり")
    return Finding("N5", "good", "プロキシは使っていません", "プロキシは設定されていません。", (), "なし")


# ------------------------------------------------------------------ 実行
def _adapter_for(p: Probes) -> AdapterInfo | None:
    try:
        aid = p.connectivity().adapter_id
    except Exception:  # noqa: BLE001 - winrt が読めなくても N2 は続ける(§10)
        aid = None
    return pick_adapter(p.adapters(), aid)


def _run_n3(p: Probes, _c: Cancel) -> Finding:
    c = p.connectivity()
    try:
        a = pick_adapter(p.adapters(), c.adapter_id)
    except Exception:  # noqa: BLE001 - アダプタが読めなくても電波は判定できる
        a = None
    return judge_signal(c, a)


CHECKS: tuple[Check, ...] = (
    Check("N1", "net", "Windows の接続の判定を読んでいます", lambda p, _c: judge_connectivity(p.connectivity())),
    Check("N2", "net", "IP アドレスとルーターの設定を調べています", lambda p, _c: judge_addresses(_adapter_for(p))),
    Check("N3", "net", "Wi-Fi の電波を調べています", _run_n3),
    Check("N4", "net", "従量制の設定を調べています", lambda p, _c: judge_cost(p.connectivity())),
    Check("N5", "net", "プロキシの設定を調べています", lambda p, _c: judge_proxy(p.proxy())),
)
TITLES: dict[str, str] = {"N1": "インターネットへの接続", "N2": "IP アドレス・ゲートウェイ・DNS", "N3": "Wi-Fi の電波",
                          "N4": "従量制の設定", "N5": "プロキシ"}
