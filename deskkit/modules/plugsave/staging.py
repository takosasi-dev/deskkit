# `_作業中\<回の ID>\<連番>.part` の一時ファイルの管理と、前の回の残りの片づけ(B-8・FR-20・FR-22)。
# PlugSave が消してよいのは、自分が作った名前の形の .part と、その空になった回のフォルダ、回のフォルダの中の印の部品
# (空の `alive` のファイル・空の `end`・`from` のフォルダ)だけ(INV-2)。
# 止まった回のフォルダは印として次の回まで残す(v0.4.1。unfinished.py)。
# 消す処理はこのファイルにだけ置き、消す前に名前の形を確かめる(AC-20)。
from __future__ import annotations

import os
import re
import secrets

WORK_DIR = "_作業中"
ALIVE_NAME = "alive"              # v0.4.1 の回の目印(空のファイル)。更新日時を回の途中で進める(unfinished.py)
END_NAME = "end"                  # 止まった回の印を次の回が閉じたしるし(空のフォルダ。作成日時が窓の終わり)
FROM_NAME = "from"                # 確かめ終えたところ(空のフォルダ。作成日時より前は確かめ済み)
MARK_DIRS = (END_NAME, FROM_NAME)
RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
PART_RE = re.compile(r"^[0-9]+\.part$")
MARKER_TMP_RE = re.compile(r"^plugsave_id\.[0-9a-f]{32}\.part$")   # 印を書くときの一時ファイル(drives.write_new_file)
TEXT_TMP_RE = re.compile(r"^readme\.[0-9a-f]{32}\.part$")          # 説明のファイルを書くときの一時ファイル


def new_run_id() -> str:
    return secrets.token_hex(16)


def is_own_part(name: str) -> bool:
    return bool(PART_RE.match(name) or MARKER_TMP_RE.match(name) or TEXT_TMP_RE.match(name))


def _is_run_dir(path: str) -> bool:
    return bool(RUN_ID_RE.match(os.path.basename(path.rstrip("\\/"))))


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
    if not _is_run_dir(path):
        return False
    try:
        os.rmdir(path)  # INV-2 の例外: PlugSave が作った回のフォルダが空になったときだけ(空でなければ失敗して残る)
    except OSError:
        return False
    return True


def remove_marks(run_dir: str) -> None:
    """回のフォルダ(名前が 32 桁の 16 進)の中の印の部品を消す: 空の `alive` のファイルと、空の `end`・`from` のフォルダ。
    大きさが 0 でない `alive`・空でないフォルダは消さない(PlugSave が作った形と違う)。"""
    if not _is_run_dir(run_dir):
        return
    alive = os.path.join(run_dir, ALIVE_NAME)
    try:
        st = os.lstat(alive)
        if st.st_size == 0 and os.path.isfile(alive) and not os.path.islink(alive):
            os.remove(alive)  # INV-2 の例外: PlugSave が作った空の目印のファイルだけ(名前・大きさ 0 を確かめた)
    except OSError:
        pass
    for name in MARK_DIRS:
        try:
            os.rmdir(os.path.join(run_dir, name))  # INV-2 の例外: PlugSave が作った空の印のフォルダだけ(空でなければ失敗して残る)
        except OSError:
            pass


def retire_markers(run_dirs: list[str]) -> int:
    """止まった回の印を片づける(手当てが済んだとき)。中の .part・印の部品を消してから空の回のフォルダを消す。消せた印の数。"""
    n = 0
    for d in run_dirs:
        if not _is_run_dir(d):
            continue
        try:
            inner = list(os.scandir(d))
        except OSError:
            continue
        for p in inner:
            try:
                if PART_RE.match(p.name) and p.is_file(follow_symlinks=False):
                    discard_part(p.path)
            except OSError:
                continue
        remove_marks(d)
        if remove_empty_run_dir(d):
            n += 1
    return n


def cleanup_leftovers(pc_dir: str, *, keep_dirs: bool = False) -> int:
    """FR-22: `<PC>\\_作業中\\` の中の、32 桁の 16 進の名前のフォルダの中の `<数字>.part` だけを消し、空になったフォルダを消す。
    keep_dirs なら回のフォルダは残す(止まった回の印。unfinished.py。手当てが済んでから retire_markers で消す)。
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
        if not keep_dirs:
            remove_empty_run_dir(run.path)
    return removed


class RunStaging:
    """1回分の `_作業中\\<回の ID>\\`。part() で次の連番の一時ファイルのパスを返す。
    v0.4.1: 中に空の `alive` のファイルを置き(版の目印)、beat() でその更新日時を今にする(前の回の終わりに近い時刻)。"""

    def __init__(self, pc_dir: str, run_id: str | None = None) -> None:
        self.run_id = run_id or new_run_id()
        self.dir = os.path.join(pc_dir, WORK_DIR, self.run_id)
        self.alive = os.path.join(self.dir, ALIVE_NAME)
        self._n = 0

    def create(self) -> None:
        """フォルダを作る。作れなければ OSError(書き込み禁止など)。目印を作れなかったときは目印なし(次の回は安全な側で扱う)。"""
        os.makedirs(self.dir, exist_ok=False)
        try:
            with open(self.alive, "xb"):
                pass
        except OSError:
            pass

    def beat(self) -> None:
        """目印の更新日時を今にする(失敗は無視)。"""
        try:
            os.utime(self.alive)
        except OSError:
            pass

    def part(self) -> str:
        self._n += 1
        return os.path.join(self.dir, f"{self._n}.part")

    def finish(self) -> bool:
        remove_marks(self.dir)
        return remove_empty_run_dir(self.dir)
