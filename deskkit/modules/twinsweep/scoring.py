# 「残す」候補の選び方(G-4、2026-09-25 に利用者の判断で順番を変更)。(1)画素数が多い (2)撮影日時(無ければ更新日時)が古い
# (3)鮮明さが高い (4)ファイルが大きい の順に比べて1枚選び、決め手になった項目を画面に出す理由にする。
# 日時を鮮明さより先にするのは、明るさ・コントラストを上げて編集した版ほど鮮明さが高く出て、元の写真より選ばれてしまうため。
from __future__ import annotations

from collections.abc import Sequence

from deskkit.modules.twinsweep.grouping import Photo

REASON_PIXELS = "いちばん解像度が高い"
REASON_OLDEST = "いちばん先に撮った写真"
REASON_SHARP = "いちばんくっきりしている"
REASON_SIZE = "いちばんファイルが大きい"
REASON_FIRST = "どれも同じなので最初の1枚"
SHARP_EPSILON = 1e-6


def _second(p: Photo) -> int:
    """撮影日時(無ければ更新日時)を秒に丸める。EXIF の撮影日時は秒単位なので、1 秒未満の差は同じ時刻とみなす。"""
    return round(p.when)


def _key(p: Photo) -> tuple[int, int, float, int, str]:
    return (-p.pixels, _second(p), -round(p.sharpness, 6), -p.size, p.path.casefold())


def pick_keep(group: Sequence[Photo]) -> tuple[Photo, str]:
    """(残す候補, 理由)。グループは2枚以上。"""
    ranked = sorted(group, key=_key)
    best, second = ranked[0], ranked[1]
    if best.pixels != second.pixels:
        return best, REASON_PIXELS
    if _second(best) != _second(second):
        return best, REASON_OLDEST
    if abs(best.sharpness - second.sharpness) > SHARP_EPSILON:
        return best, REASON_SHARP
    if best.size != second.size:
        return best, REASON_SIZE
    return best, REASON_FIRST
