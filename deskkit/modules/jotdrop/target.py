# 書き込み先の解決(フォルダ + 日付つきのファイル名。J-16)と、書く前の検証(拡張子・ネットワーク・フォルダの有無。FR-9・J-8)。
# フォルダは作らない(既定の「ドキュメント\JotDrop」だけは module が作る)。パスはログに書かない(J-15)。
from __future__ import annotations

import ntpath
from datetime import datetime

from deskkit.modules.jotdrop import compose
from deskkit.modules.jotdrop._win32 import DRIVE_REMOTE, Win32Api

DEFAULT_SUBDIR = "JotDrop"

# 預かりの理由コード(§9)。AUTO_RETRY は 60 秒おき・次のホットキー・起動時に自動で書き直す(FR-12)
REASON_BUSY = "busy"
REASON_OPEN_FAILED = "open_failed"
REASON_FOLDER_MISSING = "folder_missing"
REASON_NOT_UTF8 = "not_utf8"
REASON_NETWORK = "network"
REASON_DENIED = "denied"
REASON_MISSING = "missing"
REASON_NO_FILE = "no_file"          # create_file が false で、ファイルが無い(仕様書の一覧に無いので足した)
REASON_BAD_TARGET = "bad_target"    # 拡張子が .md / .txt でない(設定の検証をすり抜けたとき)
AUTO_RETRY = frozenset({REASON_BUSY, REASON_OPEN_FAILED})
REASONS = (REASON_BUSY, REASON_OPEN_FAILED, REASON_FOLDER_MISSING, REASON_NOT_UTF8, REASON_NETWORK, REASON_DENIED,
           REASON_MISSING, REASON_NO_FILE, REASON_BAD_TARGET)

# 通知と画面に出す文(パス・本文を入れない)
REASON_TEXT: dict[str, str] = {
    REASON_BUSY: "ほかのアプリがファイルを使っています",
    REASON_OPEN_FAILED: "ファイルを開けませんでした",
    REASON_FOLDER_MISSING: "書き込み先のフォルダが見つかりません",
    REASON_NOT_UTF8: "このファイルの文字の形式には書けません",
    REASON_NETWORK: "ネットワーク上の場所には書きません",
    REASON_DENIED: "このファイルには書き込めません",
    REASON_MISSING: "書いた行がファイルに見つかりません",
    REASON_NO_FILE: "書き込み先のファイルがありません(新しく作らない設定です)",
    REASON_BAD_TARGET: "書き込み先は .md か .txt のファイルにしてください",
}


def default_folder(documents: str) -> str:
    return ntpath.join(documents, DEFAULT_SUBDIR)


def folder_of(setting: str, documents: str) -> str:
    """設定の folder が空なら既定(ドキュメント\\JotDrop)。"""
    return setting.strip() or default_folder(documents)


def resolve(folder: str, pattern: str, when: datetime) -> str:
    """Enter を押した瞬間の暦の日付でファイル名を決める(0 時で切り替わる。J-16)。"""
    name = compose.expand(pattern, when, allowed=("date",))
    return ntpath.join(folder, name)


def is_unc(path: str) -> bool:
    p = path.replace("/", "\\")
    return p.startswith("\\\\")


def check(path: str, api: Win32Api) -> str | None:
    """書く前の検証(FR-9)。書けないときは理由コード。"""
    if not path.lower().endswith(compose.EXTENSIONS):
        return REASON_BAD_TARGET
    if is_unc(path):
        return REASON_NETWORK
    drive, _rest = ntpath.splitdrive(path)
    if not drive:
        return REASON_OPEN_FAILED  # 相対パスには書かない
    if api.drive_type(drive + "\\") == DRIVE_REMOTE:
        return REASON_NETWORK
    if not api.is_dir(ntpath.dirname(path)):
        return REASON_FOLDER_MISSING
    if api.is_dir(path):
        return REASON_OPEN_FAILED  # 書き込み先がフォルダ(§10)
    return None


def validate_folder(folder: str) -> str | None:
    """設定の folder の検証(空は既定)。ネットワーク・相対パスは保存しない。"""
    f = folder.strip()
    if not f:
        return None
    if is_unc(f):
        return "ネットワーク上の場所(\\\\ で始まる)には書きません"
    drive, rest = ntpath.splitdrive(f)
    if not drive or not rest.startswith(("\\", "/")):
        return "フォルダは C:\\… のような完全な場所で指定してください"
    return None
