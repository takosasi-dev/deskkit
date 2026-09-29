# 調べる組み合わせの一覧を作る(K-4)。Windows 用の印(K-5)・試さない組(F12・Ctrl+Alt+Delete・Win+L)・おすすめ(FR-11)。
# 純粋な関数だけ。修飾キーの値は Windows のホットキーの MOD_*(MOD_NOREPEAT は含めない)。
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from deskkit.modules.keyfree import keynames

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x1, 0x2, 0x4, 0x8
MOD_MASK = 0xF
VK_F12, VK_DELETE, VK_L = 0x7B, 0x2E, 0x4C

# 既定の4組(修飾キー2つ以上で Windows キーを含まない)と、include_win で足す7組
BASE_GROUPS: tuple[int, ...] = (MOD_CONTROL | MOD_ALT, MOD_CONTROL | MOD_SHIFT, MOD_ALT | MOD_SHIFT,
                                MOD_CONTROL | MOD_ALT | MOD_SHIFT)
WIN_GROUPS: tuple[int, ...] = (MOD_WIN | MOD_CONTROL, MOD_WIN | MOD_ALT, MOD_WIN | MOD_SHIFT, MOD_WIN | MOD_CONTROL | MOD_ALT,
                               MOD_WIN | MOD_CONTROL | MOD_SHIFT, MOD_WIN | MOD_ALT | MOD_SHIFT,
                               MOD_WIN | MOD_CONTROL | MOD_ALT | MOD_SHIFT)
ALL_GROUPS = BASE_GROUPS + WIN_GROUPS
_MOD_NAMES = ((MOD_WIN, "Win"), (MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift"))
_MOD_ALIASES = {"win": MOD_WIN, "windows": MOD_WIN, "ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT,
                "shift": MOD_SHIFT}
DEFAULT_ORDER = ("Ctrl+Alt+Shift", "Ctrl+Alt", "Alt+Shift", "Ctrl+Shift")

Combo = tuple[int, int]


def group_name(mods: int) -> str:
    """修飾キーの組の名前("Ctrl+Alt+Shift")。並びは ctx.hotkeys.format と同じ(Win・Ctrl・Alt・Shift)。"""
    return "+".join(n for m, n in _MOD_NAMES if mods & m)


def parse_group(text: str) -> int | None:
    """"Ctrl+Alt" → 修飾キーの値。読めない・修飾キーが無いときは None。"""
    mods = 0
    for part in str(text).split("+"):
        m = _MOD_ALIASES.get(part.strip().lower())
        if m is None:
            return None
        mods |= m
    return mods or None


def modifier_count(mods: int) -> int:
    return bin(mods & MOD_MASK).count("1")


def is_reserved(mods: int, vk: int) -> bool:
    """一覧でも「1つだけ調べる」でも試さない組(K-4): F12 の全部・Ctrl+Alt+Delete・Win+L。"""
    m = mods & MOD_MASK
    return vk == VK_F12 or (m == MOD_CONTROL | MOD_ALT and vk == VK_DELETE) or (m == MOD_WIN and vk == VK_L)


def windows_mark(mods: int, vk: int) -> bool:
    """「Windows 用」の印(K-5): Windows キーを含む組・PrintScreen を含む組・試さない組。"""
    return bool(mods & MOD_WIN) or vk == keynames.VK_SNAPSHOT or is_reserved(mods, vk)


@dataclass(frozen=True)
class Plan:
    groups: tuple[int, ...]                          # 表のカードの順
    rows: tuple[tuple[keynames.KeyRow, tuple[int, ...]], ...]   # 5段と、それぞれに出すキー(配列に無い記号は除く)
    probe: tuple[Combo, ...]                         # host に渡す候補(DeskKit の組を除く前)
    reserved: tuple[Combo, ...]                      # 試さない組(表には「調べられない(Windows 用)」で出す)

    @property
    def keys(self) -> tuple[int, ...]:
        return tuple(vk for _r, vks in self.rows for vk in vks)

    def combos(self) -> list[Combo]:
        return [(m, vk) for m in self.groups for vk in self.keys]


def build(include_win: bool, chars: dict[int, str]) -> Plan:
    """N = M × K − E − M × S(K-4・K-10)。既定は 4 × 88 − 5 = 347、include_win で 11 × 88 − 12 = 956(S=0 のとき)。"""
    groups = ALL_GROUPS if include_win else BASE_GROUPS
    rows = tuple((r, keynames.visible_row(r, chars)) for r in keynames.ROWS)
    keys = [vk for _r, vks in rows for vk in vks]
    probe: list[Combo] = []
    reserved: list[Combo] = []
    for m in groups:
        for vk in keys:
            (reserved if is_reserved(m, vk) else probe).append((m, vk))
    return Plan(tuple(groups), rows, tuple(probe), tuple(reserved))


def normalize_order(order: object) -> list[str]:
    """recommend_order を読める組の名前だけに(重なりは除く)。1つも読めなければ既定の順。"""
    out: list[str] = []
    seen: set[int] = set()
    if isinstance(order, list):
        for item in order:
            m = parse_group(item) if isinstance(item, str) else None
            if m is None or m not in ALL_GROUPS or m in seen:
                continue
            seen.add(m)
            out.append(group_name(m))
    return out or list(DEFAULT_ORDER)


def recommend(state_of: Callable[[Combo], str | None], order: Sequence[str], count: int,
              exclude: Iterable[Combo] = ()) -> list[Combo]:
    """FR-11: 空きで Windows 用の印が無く、キーが英字か数字の組を、order の修飾キーの順に count 個まで。"""
    skip = set(exclude)
    out: list[Combo] = []
    for name in order:
        m = parse_group(name)
        if m is None:
            continue
        for vk in (*keynames.LETTERS, *keynames.DIGITS):
            c = (m, vk)
            if len(out) >= count:
                return out
            if c in skip or windows_mark(m, vk) or state_of(c) != "free":
                continue
            out.append(c)
    return out
