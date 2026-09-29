# 測定用(配布物に入れない。KeyFree 仕様書 §8 の tools/keyfree_measure.py の代わりに、担当の範囲の tests に置く)。
# 別のプロセスとしてホットキーを持つ・試す。KeyFree 本体とは関係の無い、テストのためだけのプログラム。
#   hold <mods> <vk> <seconds> [--norepeat] : 登録して "READY" を出し、標準入力が閉じるか seconds 秒で返して終わる。失敗は "FAIL <番号>"
#   try <mods> <vk>                         : 登録してすぐ返す。終了コード 0=登録できた / 20=1409 / 22=それ以外
from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes as w

MOD_NOREPEAT = 0x4000


def _user32() -> ctypes.WinDLL:
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.RegisterHotKey.argtypes = [w.HWND, ctypes.c_int, w.UINT, w.UINT]
    u.RegisterHotKey.restype = w.BOOL
    u.UnregisterHotKey.argtypes = [w.HWND, ctypes.c_int]
    u.UnregisterHotKey.restype = w.BOOL
    return u


def hold(mods: int, vk: int, seconds: float, norepeat: bool) -> int:
    u = _user32()
    if not u.RegisterHotKey(None, 1, mods | (MOD_NOREPEAT if norepeat else 0), vk):
        print(f"FAIL {ctypes.get_last_error()}", flush=True)
        return 1
    try:
        print("READY", flush=True)
        done = threading.Event()

        def wait_stdin() -> None:
            sys.stdin.read()
            done.set()

        threading.Thread(target=wait_stdin, daemon=True).start()
        done.wait(seconds)
    finally:
        u.UnregisterHotKey(None, 1)
    return 0


def try_once(mods: int, vk: int) -> int:
    u = _user32()
    if u.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk):
        u.UnregisterHotKey(None, 1)
        return 0
    return 20 if ctypes.get_last_error() == 1409 else 22


def main(argv: list[str]) -> int:
    if len(argv) >= 4 and argv[0] == "hold":
        return hold(int(argv[1]), int(argv[2]), float(argv[3]), "--norepeat" in argv)
    if len(argv) >= 3 and argv[0] == "try":
        return try_once(int(argv[1]), int(argv[2]))
    print(__doc__ or "usage: hold <mods> <vk> <seconds> [--norepeat] | try <mods> <vk>")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
