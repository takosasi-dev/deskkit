# PyInstaller 用の入口スクリプト。python -m deskkit と同じく app.main() を呼ぶ。
import sys

from deskkit.app import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
