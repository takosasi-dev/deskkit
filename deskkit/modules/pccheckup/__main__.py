# `python -m deskkit.modules.pccheckup --selftest` の入口。終了コード 0=合格 / 1=不合格 / 2=使い方の誤り。
from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    if argv[:1] == ["--selftest"]:
        from deskkit.modules.pccheckup.selftest import run

        return run()
    print("使い方: python -m deskkit.modules.pccheckup --selftest")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
