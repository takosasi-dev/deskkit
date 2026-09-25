# 「結果をコピー」の文字列を作る(FR-6・P-4)。プロセス名と数値は入れ、ユーザーフォルダは %USERPROFILE% に置き換える。
# SSID とコンピューター名は入れない(念のため、与えられた値が紛れ込んでいれば伏せる)。
# この文字列はクリップボードにだけ置き、ログ・履歴には書かない。
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from deskkit.modules.pccheckup.checks.base import CATEGORY_TITLES, STATUS_ORDER, STATUS_TEXT, Finding


@dataclass(frozen=True)
class Secrets:
    """伏せる値。user_profile は %USERPROFILE%、user_name は %USERNAME%、ほかは「(伏せ字)」にする。"""

    user_profile: str | None = None
    user_name: str | None = None
    computer_name: str | None = None
    ssids: tuple[str, ...] = ()


MIN_WORD = 2  # これより短い名前は、ほかの語を壊すので単独では置き換えない(パスの中の分は置き換える)


def redact(text: str, s: Secrets) -> str:
    """大文字小文字を区別せずに置き換える。置き換え後の文字(%USERPROFILE% など)を後の置き換えで壊さないよう、印を経由する。"""
    marks: list[tuple[str, str]] = []

    def sub(src: str, needle: str | None, repl: str, *, short_ok: bool = False) -> str:
        if not needle:
            return src
        if len(needle) < MIN_WORD and not short_ok:
            return src
        mark = f"\x00{len(marks)}\x00"
        marks.append((mark, repl))
        return re.sub(re.escape(needle), mark, src, flags=re.IGNORECASE)

    out = text
    if s.user_profile:
        prof = s.user_profile.rstrip("\\/")
        out = sub(out, prof, "%USERPROFILE%", short_ok=True)
        out = sub(out, prof.replace("\\", "/"), "%USERPROFILE%", short_ok=True)
    for ssid in sorted((x for x in s.ssids if x), key=len, reverse=True):
        out = sub(out, ssid, "(伏せ字)")
    out = sub(out, s.computer_name, "(PC 名)")
    out = sub(out, s.user_name, "%USERNAME%")
    for mark, repl in marks:
        out = out.replace(mark, repl)
    return out


def _finding_lines(f: Finding, worse: bool) -> list[str]:
    head = f"[{STATUS_TEXT[f.status]}] {f.title}"
    if f.value:
        head += f": {f.value}"
    if worse:
        head += "(前回より悪化)"
    lines = [head]
    if f.detail:
        lines.append(f"  {f.detail}")
    for r in f.rows:
        note = f"({r.note})" if r.note else ""
        lines.append(f"  ・{r.label}  {r.value}{note}")
    for a in f.actions:
        text = a.text or (a.button or "")
        if a.button and a.text:
            text += f"(「{a.button}」)"
        if text:
            lines.append(f"  → {text}")
    return lines


def build(sections: Sequence[tuple[str, Sequence[Finding], int | None]], when: datetime, secrets: Secrets,
          worse: Iterable[str] = ()) -> str:
    """sections: (カテゴリ, 結果, 所要ミリ秒 or None)。status の悪い順に並べる。"""
    worse_set = set(worse)
    out = [f"PcCheckup の診断結果({when.strftime('%Y-%m-%d %H:%M')})"]
    for cat, findings, ms in sections:
        out.append("")
        took = f"(所要 {ms / 1000:.1f} 秒)" if ms is not None else ""
        out.append(f"■ {CATEGORY_TITLES.get(cat, cat)}{took}")
        for f in sorted(findings, key=lambda x: (STATUS_ORDER[x.status], x.check_id)):
            out.extend(_finding_lines(f, f.check_id in worse_set))
    return redact("\n".join(out) + "\n", secrets)
