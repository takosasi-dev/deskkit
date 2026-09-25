# `python -m deskkit` の入口。実処理は app.main() に委ねる。
import sys

from deskkit.app import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
