# キーの並び(英字・数字・F キー・記号・その他の5段。K-4・FR-3)と表示名。記号キーは今の配列の文字を出す(K-10)。
# 配列に無い記号キー(MapVirtualKeyW が 0)は表に出さず、試さない。純粋な関数だけ(Win32 は _win32.KeyboardApi 越し)。
from __future__ import annotations

from dataclasses import dataclass

from deskkit.modules.keyfree._win32 import KeyboardApi

LETTERS = [0x41 + i for i in range(26)]
DIGITS = [0x30 + i for i in range(10)]
FKEYS = [0x70 + i for i in range(24)]
# VK_OEM_1(;)・PLUS(=)・COMMA・MINUS・PERIOD・OEM_2(/)・OEM_3(`)・OEM_4([)・OEM_5(\)・OEM_6(])・OEM_7(')・OEM_102
SYMBOLS = [0xBA, 0xBB, 0xBC, 0xBD, 0xBE, 0xBF, 0xC0, 0xDB, 0xDC, 0xDD, 0xDE, 0xE2]
OTHER_LABELS: dict[int, str] = {
    0x20: "Space", 0x0D: "Enter", 0x09: "Tab", 0x08: "Back", 0x2D: "Ins", 0x2E: "Del", 0x24: "Home", 0x23: "End",
    0x21: "PgUp", 0x22: "PgDn", 0x25: "←", 0x26: "↑", 0x27: "→", 0x28: "↓", 0x13: "Pause", 0x2C: "PrtSc",
}
OTHERS = list(OTHER_LABELS)
VK_SNAPSHOT = 0x2C


@dataclass(frozen=True)
class KeyRow:
    key: str
    label: str
    vks: tuple[int, ...]


ROWS: tuple[KeyRow, ...] = (
    KeyRow("letters", "英字", tuple(LETTERS)),
    KeyRow("digits", "数字", tuple(DIGITS)),
    KeyRow("fkeys", "F キー", tuple(FKEYS)),
    KeyRow("symbols", "記号", tuple(SYMBOLS)),
    KeyRow("others", "その他", tuple(OTHERS)),
)
ALL_KEYS: tuple[int, ...] = tuple(vk for r in ROWS for vk in r.vks)   # K = 88


def layout_chars(api: KeyboardApi) -> dict[int, str]:
    """記号キーごとの今の配列の文字。0 が返ったキーは入れない(K-10)。読めなければそのキーも入れない。"""
    out: dict[int, str] = {}
    for vk in SYMBOLS:
        try:
            code = int(api.vk_to_char(vk)) & 0xFFFF   # デッドキーの印(最上位ビット)を落とす
        except (OSError, ValueError, TypeError):
            continue
        if code > 0x20:
            out[vk] = chr(code)
    return out


def visible_row(row: KeyRow, chars: dict[int, str]) -> tuple[int, ...]:
    if row.key != "symbols":
        return row.vks
    return tuple(vk for vk in row.vks if vk in chars)


def key_label(vk: int, chars: dict[int, str]) -> str:
    if vk in chars:
        return chars[vk]
    if vk in OTHER_LABELS:
        return OTHER_LABELS[vk]
    if 0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39:
        return chr(vk)
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x6F}"
    return f"VK{vk:02X}"
