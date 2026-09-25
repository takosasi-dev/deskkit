# python -m deskkit.modules.dropsort --selftest(FR-24)。それ以外の引数は単独 CLI(cli.main)へ渡す。
from __future__ import annotations

import sys

from deskkit.modules.dropsort.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
