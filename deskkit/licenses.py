# 同梱したサードパーティのライセンス(THIRD_PARTY_LICENSES.txt)の場所と読み込み(H-5)。
# exe では PyInstaller の展開先(sys._MEIPASS)の直下、ソース実行ではリポジトリ直下にある。
# 表示は deskkit/ui/license_dialog.py、中身は tools/make_third_party_licenses.py が作る。
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from deskkit import paths

FILE_NAME = "THIRD_PARTY_LICENSES.txt"
_RULE = "=" * 72


@dataclass(frozen=True)
class Entry:
    name: str
    version: str
    license: str
    anchor: int  # 本文の中の、その節の見出しの位置(文字数)。見つからなければ -1


def licenses_path() -> Path | None:
    if paths.is_frozen():
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        cands = [base / FILE_NAME, Path(sys.executable).parent / FILE_NAME]
    else:
        cands = [paths.source_root() / FILE_NAME]
    return next((p for p in cands if p.is_file()), None)


def read_text() -> str:
    """全文(BOM を除く)。見つからないときは、その旨の短い文。"""
    p = licenses_path()
    if p is None:
        return f"{FILE_NAME} が見つかりません。DeskKit を入れ直してください。"
    return p.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n")


_ITEM = re.compile(r"^\s*(\d+)\. (.+)\n\s+版: (.+)\n\s+ライセンス: (.+)$", re.MULTILINE)


def summary(text: str) -> list[Entry]:
    """冒頭の一覧(番号・名前・版・ライセンス)と、各節の位置。"""
    out: list[Entry] = []
    for m in _ITEM.finditer(text):
        num, name = m.group(1), m.group(2).strip()
        head = f"{_RULE}\n{num}. {name}\n{_RULE}"
        pos = text.find(head, m.end())
        out.append(Entry(name, m.group(3).strip(), m.group(4).strip(), pos + len(_RULE) + 1 if pos >= 0 else -1))
    return out


def export_copy() -> Path | None:
    """メモ帳などで開く用に %LOCALAPPDATA%\\DeskKit\\ へ写しを置く(exe の展開先は終了時に消えるため)。"""
    p = licenses_path()
    if p is None:
        return None
    dst = paths.local_dir() / FILE_NAME
    dst.parent.mkdir(parents=True, exist_ok=True)
    data = p.read_bytes()
    if not dst.is_file() or dst.read_bytes() != data:
        dst.write_bytes(data)
    return dst
