# `python -m deskkit.modules.modeshift --selftest` の入口(FR-24)。偽の実装だけで検査する。
import sys

from deskkit.modules.modeshift.selftest import run

if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        sys.exit(run())
    print("使い方: python -m deskkit.modules.modeshift --selftest")
    sys.exit(1)
