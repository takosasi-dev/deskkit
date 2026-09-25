# トップレベルウィンドウを列挙し、除外判定(D-7)と対象判定(targets の許可リスト。D-6)を付ける。
# タイトルは対応付けとダイアログ表示のためにメモリ上だけで使い、ログには出さない(D-14)。
from __future__ import annotations

from collections.abc import Iterable, Sequence

from ._win32 import Win32Api
from .config import Target
from .model import RawMonitor, RawWindow, Rect, WindowInfo, exe_basename
from .monitors import placement_to_screen_candidates, screen_to_workspace

# 除外の種類(excluded:<種類>)
EXCLUDE_LABELS = {
    "self": "DeskKit 自身",
    "invisible": "非表示",
    "cloaked": "別の仮想デスクトップ等(cloaked)",
    "toolwindow": "ツールウィンドウ",
    "owned": "オーナー付き(ダイアログ)",
    "minimized": "最小化中",
    "no_exe": "exe を取得できない",
    "game": "ゲーム",
    "class": "除外クラス",
    "no_placement": "配置を取得できない",
    "offscreen": "どのモニタとも重ならない",
}


def effective_normal_rect(raw: RawWindow, mons: Sequence[RawMonitor]) -> tuple[Rect | None, bool]:
    """(戻す基準にする normal_rect(ワークスペース座標), スナップ中か)。
    通常表示で GetWindowRect が rcNormalPosition(スクリーン座標に換算)と違えば Aero スナップ等とみなし、
    今の見た目の矩形をワークスペース座標に戻して使う(rcNormalPosition はスナップ前の位置のため)。
    換算のずれはウィンドウが載っているモニタの作業領域で決まる(主モニタのずれを全モニタに使わない)。"""
    p = raw.placement
    if p is None:
        return None, False
    if p.show != "normal" or raw.screen_rect is None or not mons:
        return p.normal_rect, False
    if raw.screen_rect in placement_to_screen_candidates(p.normal_rect, mons):
        return p.normal_rect, False
    return screen_to_workspace(raw.screen_rect, mons), True


def exclusion_of(raw: RawWindow, own_pid: int, game_processes: frozenset[str], exclude_classes: frozenset[str]) -> str | None:
    """D-7 の除外理由。候補なら None。判定順は固定。"""
    if raw.pid == own_pid:
        return "self"
    if not raw.visible:
        return "invisible"
    if raw.cloaked:
        return "cloaked"
    if raw.toolwindow:
        return "toolwindow"
    if raw.owned:
        return "owned"
    if raw.iconic or (raw.placement is not None and raw.placement.show == "minimized"):
        return "minimized"
    if not raw.exe:
        return "no_exe"
    if exe_basename(raw.exe).lower() in game_processes:
        return "game"
    if raw.cls in exclude_classes:
        return "class"
    if raw.placement is None:
        return "no_placement"
    return None


def target_index(raw: RawWindow, targets: Sequence[Target]) -> int | None:
    for i, t in enumerate(targets):
        if t.matches(raw.exe, raw.cls, raw.title):
            return i
    return None


def classify(raws: Iterable[RawWindow], targets: Sequence[Target], own_pid: int, game_processes: frozenset[str],
             exclude_classes: frozenset[str]) -> list[WindowInfo]:
    out: list[WindowInfo] = []
    for r in raws:
        out.append(WindowInfo(raw=r, excluded=exclusion_of(r, own_pid, game_processes, exclude_classes),
                              target_index=target_index(r, targets)))
    return out


def enumerate_raw(api: Win32Api) -> list[RawWindow]:
    out: list[RawWindow] = []
    for h in api.enum_windows():
        r = api.describe_window(h)
        if r is not None:
            out.append(r)
    return out


def snapshot(api: Win32Api, targets: Sequence[Target], game_processes: frozenset[str],
             exclude_classes: frozenset[str]) -> list[WindowInfo]:
    """全トップレベルウィンドウ(除外付き)。対象外(targets に当たらない)も含む。"""
    return classify(enumerate_raw(api), targets, api.current_pid(), game_processes, exclude_classes)


def target_windows(wins: Iterable[WindowInfo]) -> list[WindowInfo]:
    """targets に当たるウィンドウ(除外されたものも含む。M-6 の判定に使う)。"""
    return [w for w in wins if w.target_index is not None]


def pickable(wins: Iterable[WindowInfo]) -> list[WindowInfo]:
    """「今開いているウィンドウから選ぶ」に出す候補(見えていて普通のトップレベル)。"""
    keep = {None, "minimized", "class", "game"}
    return [w for w in wins if w.excluded in keep and w.raw.exe]
