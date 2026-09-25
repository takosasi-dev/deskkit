# ルールの試し当て(D1): 画面で入力したファイル名・サイズ・入手元(ドメインとゾーン)に対し、どのルールが当たり、
# どこへ動く予定かを、実際の評価と同じ関数(rules.explain / first_match 相当)で求める。ファイルには一切触らない。
# 入力されたドメインや URL はここで使うだけで、ログ・操作ログ・設定には書かない。
from __future__ import annotations

import ntpath
from collections.abc import Mapping
from dataclasses import dataclass, field

from deskkit.modules.dropsort import guard
from deskkit.modules.dropsort import template as tpl
from deskkit.modules.dropsort.completion import CompletionTracker
from deskkit.modules.dropsort.config import Config, RuleDef
from deskkit.modules.dropsort.motw import Motw, domain_of
from deskkit.modules.dropsort.paths import DestCheck
from deskkit.modules.dropsort.rules import explain

GUARD_TEXT = {
    "double_extension": "二重拡張子(例: report.pdf.exe)のため、どのルールにも当てず「要確認」として残します",
    "bidi_control_char": "双方向制御文字を含む名前のため、「要確認」として残します",
    "spaced_extension": "拡張子の直前に連続した空白があるため、「要確認」として残します",
}


@dataclass(frozen=True)
class Verdict:
    index: int
    name: str
    status: str    # match / miss / disabled / shadowed(上のルールが先に一致)
    text: str


@dataclass
class SimResult:
    verdicts: list[Verdict] = field(default_factory=list)
    matched: RuleDef | None = None
    dest: str | None = None          # 移動予定のフォルダ(テンプレートは展開済み)
    blocked: str | None = None       # temp / 危険名の理由コード(このときは動かさない)
    notes: list[str] = field(default_factory=list)


def make_motw(zone: str, domain_text: str) -> Motw:
    """zone: 'none'(MOTW なし) / 'unknown'(あり・ゾーン不明) / '0'〜'4'。domain_text は URL でもドメインでもよい。"""
    if zone == "none":
        return Motw(False)
    d = domain_text.strip()
    dom = domain_of(d) if "://" in d else (d.lower().strip(".") or None)
    if dom is not None and not all(c.isalnum() or c in ".-" for c in dom):
        dom = None
    zid = int(zone) if zone.isdigit() else None
    return Motw(True, zid, dom)


def simulate(cfg: Config, checks: Mapping[int, DestCheck], name: str, size: int, motw: Motw, now: float) -> SimResult:
    res = SimResult()
    name = ntpath.basename(name.strip())
    if CompletionTracker.is_temp(name, cfg.temp_extensions):
        res.blocked = "temp"
        res.notes.append("ダウンロード中の一時ファイル名なので、完了して名前が変わるまで待ちます")
    g = guard.check_name(name, cfg.exec_extensions)
    if g is not None:
        res.blocked = g
        res.notes.append(GUARD_TEXT.get(g, "名前が要確認に当たります"))
    for r in cfg.rules:
        c = checks.get(r.index)
        if r.error is not None:
            res.verdicts.append(Verdict(r.index, r.name, "disabled", f"無効: {r.error}"))
            continue
        if c is not None and not c.ok and not c.unavailable:
            res.verdicts.append(Verdict(r.index, r.name, "disabled", f"無効: {c.message or c.code}"))
            continue
        why = explain(r, name, size, motw)
        if res.matched is not None and why is None:
            res.verdicts.append(Verdict(r.index, r.name, "shadowed", "一致するが、上のルールが先に当たるので使われない"))
            continue
        if why is not None:
            res.verdicts.append(Verdict(r.index, r.name, "miss", why))
            continue
        res.matched = r
        base_final = c.final if c is not None and c.final else None
        if r.is_template:
            res.dest = tpl.expand(r.dest, tpl.TemplateVars(now, name, motw.domain), base_final or r.base_dest)
        else:
            res.dest = base_final or r.dest
        res.verdicts.append(Verdict(r.index, r.name, "match", "一致"))
        if c is not None and c.unavailable:
            res.notes.append("移動先のドライブが接続されていないため、今は見送ります(つながれば動きます)")
        if motw.present and c is not None and c.volume is not None and not c.volume.named_streams:
            res.notes.append(f"移動先が {c.volume.fs_name} で MOTW を保てないため、移動を拒否します")
        if not r.apply:
            res.notes.append("このルールは試運転中です。動かさずに「移動予定」として記録します")
    if res.matched is None and res.blocked is None:
        res.notes.append("どのルールにも当たりません。ダウンロードフォルダに残します")
    return res


__all__ = ["Verdict", "SimResult", "make_motw", "simulate"]
