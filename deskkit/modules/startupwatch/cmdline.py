# コマンドの行から、起動するプログラムのパスとファイル名を取り出す純粋な関数(W-10・W-13)。
# 引用符で囲まれていればその中身、無ければ実行できる拡張子の所まで(無ければ最初の空白まで)をプログラムとみなす。
from __future__ import annotations

import ntpath
import re
from collections.abc import Callable

_EXEC_EXT = re.compile(r"\.(exe|com|bat|cmd|scr|pif|lnk|vbs|vbe|js|jse|wsf|ps1|msc|cpl)(?=\s|$)", re.IGNORECASE)


def program_path(command: str, expand: Callable[[str], str] | None = None) -> str:
    """コマンドの行のプログラムの部分(引用符は外す)。expand を渡すと環境変数を展開してから取り出す。"""
    s = (expand(command) if expand is not None else command).strip()
    if not s:
        return ""
    if s.startswith('"'):
        end = s.find('"', 1)
        return s[1:end] if end > 0 else s[1:]
    m = _EXEC_EXT.search(s)
    if m is not None:
        return s[:m.end()]
    return s.split(None, 1)[0]


def exe_name(command: str, expand: Callable[[str], str] | None = None) -> str:
    """プログラムのファイル名(例: x.exe)。取れなければ空。"""
    p = program_path(command, expand)
    return ntpath.basename(p) if p else ""


def same_program(command: str, programs: set[str], expand: Callable[[str], str] | None = None) -> bool:
    """コマンドのプログラムが programs(小文字のフルパス)のどれかと同じか。大文字小文字と引用符は無視する。"""
    p = program_path(command, expand)
    return bool(p) and ntpath.normcase(ntpath.normpath(p)) in programs


def norm_program(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path))
