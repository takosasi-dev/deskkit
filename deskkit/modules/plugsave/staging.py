# `_作業中\<回の ID>\<連番>.part` の一時ファイルの管理と、前の回の残りの片づけ(B-8・FR-20・FR-22)。
# PlugSave が消してよいのは、自分が作った名前の形の .part と、その空になった回のフォルダだけ(INV-2)。
# 消す処理はこのファイルにだけ置き、消す前に名前の形を確かめる(AC-20)。
from __future__ import annotations

import os
import re
import secrets

WORK_DIR = "_作業中"
RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
PART_RE = re.compile(r"^[0-9]+\.part$")
MARKER_TMP_RE = re.compile(r"^plugsave_id\.[0-9a-f]{32}\.part$")   # 印を書くときの一時ファイル(drives.write_new_file)
TEXT_TMP_RE = re.compile(r"^readme\.[0-9a-f]{32}\.part$")          # 説明のファイルを書くときの一時ファイル


def new_run_id() -> str:
    return secrets.token_hex(16)


def is_own_part(name: str) -> bool:
    return bool(PART_RE.match(name) or MARKER_TMP_RE.match(name) or TEXT_TMP_RE.match(name))


def discard_part(path: str) -> bool:
    """自分が作った一時ファイルを消す。名前の形が合わなければ何もしない。消せたら True。"""
    if not is_own_part(os.path.basename(path)):
        return False
    try:
        os.remove(path)  # INV-2 の例外: PlugSave が作った .part だけ(名前の形を上で確かめた)
    except OSError:
        return False
    return True


def remove_empty_run_dir(path: str) -> bool:
    """回のフォルダ(名前が 32 桁の 16 進)が空なら消す。中身があればフォルダの削除が失敗して残る。"""
    if not RUN_ID_RE.match(os.path.basename(path.rstrip("\\/"))):
        return False
    try:
        os.rmdir(path)  # INV-2 の例外: PlugSave が作った回のフォルダが空になったときだけ(空でなければ失敗して残る)
    except OSError:
        return False
    return True


def cleanup_leftovers(pc_dir: str) -> int:
    """FR-22: `<PC>\\_作業中\\` の中の、32 桁の 16 進の名前のフォルダの中の `<数字>.part` だけを消し、空になったフォルダを消す。
    消した .part の数を返す。ほかの名前のファイル・フォルダには触れない。"""
    work = os.path.join(pc_dir, WORK_DIR)
    removed = 0
    try:
        runs = list(os.scandir(work))
    except OSError:
        return 0
    for run in runs:
        if not RUN_ID_RE.match(run.name):
            continue
        try:
            if not run.is_dir(follow_symlinks=False) or run.is_junction():
                continue
            parts = list(os.scandir(run.path))
        except OSError:
            continue
        for p in parts:
            try:
                if PART_RE.match(p.name) and p.is_file(follow_symlinks=False) and discard_part(p.path):
                    removed += 1
            except OSError:
                continue
        remove_empty_run_dir(run.path)
    return removed


class RunStaging:
    """1回分の `_作業中\\<回の ID>\\`。part() で次の連番の一時ファイルのパスを返す。"""

    def __init__(self, pc_dir: str, run_id: str | None = None) -> None:
        self.run_id = run_id or new_run_id()
        self.dir = os.path.join(pc_dir, WORK_DIR, self.run_id)
        self._n = 0

    def create(self) -> None:
        """フォルダを作る。作れなければ OSError(書き込み禁止など)。"""
        os.makedirs(self.dir, exist_ok=False)

    def part(self) -> str:
        self._n += 1
        return os.path.join(self.dir, f"{self._n}.part")

    def finish(self) -> bool:
        return remove_empty_run_dir(self.dir)
