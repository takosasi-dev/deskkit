# python -m deskkit.modules.layoutkeep --selftest の入口(FR-17)。偽の Win32Api で M-1〜M-6 を検査する。
from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        from .selftest import run

        return run()
    print("使い方: python -m deskkit.modules.layoutkeep --selftest")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
