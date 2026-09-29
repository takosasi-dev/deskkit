# 覚えている記録と今の一覧を比べる純粋な関数(W-4)。比べる単位は「場所+名前」(名前は小文字にそろえる)のハッシュ。
# 新しい物・中身が変わった物・消えた物を返し、記録の次の中身(ハッシュと日時と印だけ。W-6)を作る。
from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from deskkit.modules.startupwatch.sources import StartupItem

FLAG_KNOWN = "known"
FLAG_NEW = "new"
FLAG_CHANGED = "changed"
FLAGS = (FLAG_KNOWN, FLAG_NEW, FLAG_CHANGED)


def item_key(loc: str, name: str) -> str:
    """「場所の記号 + NUL + 小文字にそろえた名前」の UTF-8 の SHA-256(§9)。"""
    return hashlib.sha256(f"{loc}\0{name.lower()}".encode()).hexdigest()


@dataclass
class Diff:
    added: list[StartupItem] = field(default_factory=list)
    changed: list[StartupItem] = field(default_factory=list)
    removed: list[tuple[str, str]] = field(default_factory=list)  # (鍵のハッシュ, 場所の記号)

    @property
    def empty(self) -> bool:
        return not (self.added or self.changed or self.removed)


def compare(record: dict[str, dict[str, Any]], current: Iterable[StartupItem]) -> Diff:
    d = Diff()
    seen: set[str] = set()
    for it in current:
        k = item_key(it.loc, it.name)
        if k in seen:
            continue
        seen.add(k)
        old = record.get(k)
        if old is None:
            d.added.append(it)
        elif old.get("cmd") != it.cmd_hash():
            d.changed.append(it)
    for k, v in record.items():
        if k not in seen:
            d.removed.append((k, str(v.get("loc", ""))))
    return d


def next_record(record: dict[str, dict[str, Any]], current: Iterable[StartupItem], now_iso: str,
                flag_for_added: Callable[[StartupItem], str]) -> dict[str, dict[str, Any]]:
    """次の記録の items。消えた物は外す。変わった物は「新しい」のままか「変わった」にする。"""
    out: dict[str, dict[str, Any]] = {}
    for it in current:
        k = item_key(it.loc, it.name)
        if k in out:
            continue
        old = record.get(k)
        cmd = it.cmd_hash()
        if old is None:
            out[k] = {"loc": it.loc, "cmd": cmd, "first_seen": now_iso, "flag": flag_for_added(it)}
            continue
        entry = {"loc": it.loc, "cmd": cmd, "first_seen": str(old.get("first_seen") or now_iso),
                 "flag": old.get("flag") if old.get("flag") in FLAGS else FLAG_KNOWN}
        if old.get("cmd") != cmd and entry["flag"] != FLAG_NEW and not it.is_deskkit:
            entry["flag"] = FLAG_CHANGED
        out[k] = entry
    return out


def baseline(current: Iterable[StartupItem], now_iso: str) -> dict[str, dict[str, Any]]:
    """記録が無いとき: 今ある物を全部「知っている」として覚える(W-5)。"""
    return next_record({}, current, now_iso, lambda _it: FLAG_KNOWN)
