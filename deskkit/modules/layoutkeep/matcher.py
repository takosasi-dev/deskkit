# 保存エントリと現在ウィンドウの対応付け(§5 D-5 の決定表 M-1〜M-6)。純関数。
# exe+クラスが同じ「グループ」単位で扱い、曖昧なら動かさない(INV-2)。タイトルは照合にだけ使う。
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from .model import SavedEntry, WindowInfo


@dataclass
class Match:
    index: int  # layout.windows の添字
    entry: SavedEntry
    rule: str  # "M-1" 〜 "M-6"
    status: str  # "matched" | "not_running" | "ambiguous" | "excluded:<種類>"
    window: WindowInfo | None = None


def _compile(rx: str | None) -> re.Pattern[str] | None:
    if not rx:
        return None
    try:
        return re.compile(rx)
    except re.error:
        return None


def _strong(e: SavedEntry, w: WindowInfo) -> bool:
    return (e.hwnd != 0 and e.hwnd == w.hwnd and e.pid == w.pid and e.proc_start is not None
            and e.proc_start == w.raw.proc_start and (w.exe or "").lower() == e.exe.lower() and w.cls == e.cls)


def match(entries: Sequence[SavedEntry], windows: Sequence[WindowInfo]) -> list[Match]:
    """entries の各要素に Match を1つずつ返す(順序は entries と同じ)。windows は targets に当たるもの。"""
    result: dict[int, Match] = {}
    # M-6: 除外に当たる現在ウィンドウは対応付けの前に候補から外す
    candidates = [w for w in windows if w.excluded is None]
    excluded = [w for w in windows if w.excluded is not None]

    # M-1: 強い一致(hwnd + pid + プロセス開始時刻 + exe + クラス)
    claims: dict[int, list[int]] = defaultdict(list)
    for i, e in enumerate(entries):
        for w in candidates:
            if _strong(e, w):
                claims[w.hwnd].append(i)
                break
    used: set[int] = set()
    for hwnd, idxs in claims.items():
        w = next(x for x in candidates if x.hwnd == hwnd)
        if len(idxs) == 1:
            result[idxs[0]] = Match(idxs[0], entries[idxs[0]], "M-1", "matched", w)
        else:  # 同じウィンドウを複数エントリが主張する壊れたレイアウト → 動かさない
            for i in idxs:
                result[i] = Match(i, entries[i], "M-5", "ambiguous")
        used.add(hwnd)
    # 強い一致の相手が除外中(最小化など)なら、そのエントリは excluded:<種類>(M-6)
    excluded_claimed: set[int] = set()
    for i, e in enumerate(entries):
        if i in result:
            continue
        for w in excluded:
            if _strong(e, w):
                result[i] = Match(i, e, "M-6", f"excluded:{w.excluded}", w)
                excluded_claimed.add(w.hwnd)
                break

    # 残りをグループごとに
    rest_entries: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, e in enumerate(entries):
        if i not in result:
            rest_entries[e.group].append(i)
    rest_windows: dict[tuple[str, str], list[WindowInfo]] = defaultdict(list)
    for w in candidates:
        if w.hwnd not in used:
            rest_windows[w.group].append(w)
    rest_excluded: dict[tuple[str, str], list[WindowInfo]] = defaultdict(list)
    for w in excluded:
        if w.hwnd not in excluded_claimed:
            rest_excluded[w.group].append(w)

    for g, idxs in rest_entries.items():
        cur = rest_windows.get(g, [])
        if not cur:
            ex = rest_excluded.get(g, [])
            if ex:  # M-6: 同じグループのウィンドウはあるが除外中
                for i in idxs:
                    result[i] = Match(i, entries[i], "M-6", f"excluded:{ex[0].excluded}")
            else:  # M-2: 起動していない
                for i in idxs:
                    result[i] = Match(i, entries[i], "M-2", "not_running")
            continue
        if len(idxs) == 1 and len(cur) == 1:
            e = entries[idxs[0]]
            pat = _compile(e.title_regex)
            if not e.title_regex or (pat is not None and pat.search(cur[0].title)):
                result[idxs[0]] = Match(idxs[0], e, "M-3", "matched", cur[0])
                continue
        # M-4: title_regex でちょうど1対1になる組だけ
        hits: dict[int, list[int]] = {}
        for i in idxs:
            pat = _compile(entries[i].title_regex)
            if pat is None:
                continue
            hits[i] = [k for k, w in enumerate(cur) if pat.search(w.title)]
        by_window: dict[int, list[int]] = defaultdict(list)
        for i, ks in hits.items():
            for k in ks:
                by_window[k].append(i)
        paired: set[int] = set()
        for i, ks in hits.items():
            if len(ks) == 1 and len(by_window[ks[0]]) == 1:
                result[i] = Match(i, entries[i], "M-4", "matched", cur[ks[0]])
                paired.add(i)
        # M-5: それ以外はグループの残り全部を曖昧でスキップ
        for i in idxs:
            if i not in paired:
                result[i] = Match(i, entries[i], "M-5", "ambiguous")
    return [result[i] for i in range(len(entries))]
