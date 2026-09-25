# ルール評価(FR-5〜FR-7): 上から評価して最初に一致したルールを返す(先勝ち)。条件は AND、未指定は無条件。
# 無効化されたルール(形の不正・移動先の不正)は評価から外す。
from __future__ import annotations

import ntpath
from collections.abc import Iterable

from deskkit.modules.dropsort.config import RuleDef
from deskkit.modules.dropsort.motw import Motw


def matches(rule: RuleDef, name: str, size: int, motw: Motw) -> bool:
    m = rule.match
    if m.ext is not None and ntpath.splitext(name)[1].lower() not in m.ext:
        return False
    if rule.pattern is not None and rule.pattern.search(name) is None:
        return False
    if m.size_min is not None and size < m.size_min:
        return False
    if m.size_max is not None and size > m.size_max:
        return False
    if m.motw is not None and motw.present != m.motw:
        return False
    if m.zone_ids is not None and (motw.zone_id is None or motw.zone_id not in m.zone_ids):
        return False
    if m.host_domain is not None:
        d = motw.domain
        if d is None or not any(d == x or d.endswith("." + x) for x in m.host_domain):
            return False
    return True


def first_match(rules: Iterable[RuleDef], name: str, size: int, motw: Motw, disabled: set[int]) -> RuleDef | None:
    for r in rules:
        if r.error is not None or r.index in disabled:
            continue
        if matches(r, name, size, motw):
            return r
    return None


def describe(rule: RuleDef) -> list[str]:
    """GUI 用の条件の要約(短い文字列の並び)。"""
    from deskkit.modules.dropsort.config import ZONE_NAMES

    m = rule.match
    out: list[str] = []
    if m.ext:
        exts = sorted(m.ext)
        out.append(" ".join(exts[:6]) + (f" 他{len(exts) - 6}" if len(exts) > 6 else ""))
    if m.name_regex:
        out.append(f"名前 /{m.name_regex}/")
    if m.size_min is not None or m.size_max is not None:
        out.append(f"サイズ {fmt_size(m.size_min) if m.size_min is not None else ''}〜"
                   f"{fmt_size(m.size_max) if m.size_max is not None else ''}")
    if m.motw is not None:
        out.append("MOTW あり" if m.motw else "MOTW なし")
    if m.zone_ids:
        out.append("ゾーン " + "・".join(ZONE_NAMES.get(z, str(z)) for z in sorted(m.zone_ids)))
    if m.host_domain:
        out.append("入手元 " + ", ".join(m.host_domain))
    return out or ["すべてのファイル"]


def fmt_size(n: int | None) -> str:
    if n is None:
        return "—"
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return f"{int(v)} {unit}" if unit == "B" else f"{v:.1f} {unit}".replace(".0 ", " ")
        v /= 1024
    return f"{n} B"


def parse_size(text: str) -> int | None:
    """'10MB' / '1.5 GB' / '2048' → バイト数。空は None。解釈できなければ ValueError。"""
    s = text.strip().upper().replace(" ", "")
    if not s:
        return None
    units = {"TB": 1024 ** 4, "GB": 1024 ** 3, "MB": 1024 ** 2, "KB": 1024, "B": 1}
    num, mul = s, 1
    for u, m in units.items():
        if s.endswith(u):
            num, mul = s[: -len(u)], m
            break
    v = float(num)
    if v < 0:
        raise ValueError("負のサイズ")
    return int(v * mul)


def fmt_size_exact(n: int | None) -> str:
    """編集欄用: 単位で割り切れるときだけ単位付き、それ以外はバイト数(往復で値が変わらない)。"""
    if n is None:
        return ""
    for unit, mul in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= mul and n % mul == 0:
            return f"{n // mul}{unit}"
    return str(n)
