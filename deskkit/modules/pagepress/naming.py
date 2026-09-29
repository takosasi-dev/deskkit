# 出力名・出力先・一時ファイル(P-12・FR-21・§10)。一時ファイルは同じフォルダの `~pagepress-<英数字16字>.tmp` に書き、
# 検査を通ってから正式な名前に変える。消してよいのは自分の一時ファイルと自分の出力だけで、消す処理は remove_own の1か所(INV-8)。
# pending.json には一時ファイルのパスだけを書く(ログではない。§9)。
from __future__ import annotations

import json
import os
import secrets
import string
import threading
from pathlib import Path

TMP_PREFIX = "~pagepress-"
TMP_SUFFIX = ".tmp"
MAX_NAME_TRIES = 1000
MAX_PATH = 259
FALLBACK_SUBDIR = "PagePress"
_ALNUM = string.ascii_letters + string.digits
_WORST_SUFFIX = f" ({MAX_NAME_TRIES})"


class NamesExhaustedError(Exception):
    """同じ名前が 1000 個あって空きが無い(names_exhausted)。"""


def tmp_name() -> str:
    return TMP_PREFIX + "".join(secrets.choice(_ALNUM) for _ in range(16)) + TMP_SUFFIX


def is_own_tmp(p: Path) -> bool:
    return p.name.startswith(TMP_PREFIX) and p.name.endswith(TMP_SUFFIX)


def remove_own(p: Path | None) -> None:
    """自分の一時ファイル・自分が作った出力(と空の分割フォルダ)・自分が作った「送る」のショートカットを消す唯一の場所
    (INV-8・AC-18)。利用者のファイルは渡さない。"""
    if p is None:
        return
    try:
        if p.is_dir():
            os.rmdir(p)  # 空のときだけ消える(中身があれば OSError で残る)
        else:
            os.remove(p)
    except OSError:
        pass


def fit_stem(folder: Path, stem: str, tail: str) -> str | None:
    """folder\\<stem><tail> が番号 ` (1000)` を足しても 259 文字に収まるよう stem を後ろから削る。フォルダだけで超えるなら None。"""
    room = MAX_PATH - len(str(folder)) - 1 - len(tail) - len(_WORST_SUFFIX)
    if room < 1:
        return None
    return stem[:room] if len(stem) > room else stem


def candidates(folder: Path, stem: str, tail: str) -> list[Path]:
    return [folder / (f"{stem}{tail}" if n == 1 else f"{stem} ({n}){tail}") for n in range(1, MAX_NAME_TRIES + 1)]


def rename_unique(tmp: Path, folder: Path, stem: str, tail: str) -> Path:
    """tmp を folder\\<stem><tail>(同じ名前があれば ` (2)` … ` (1000)`)に変える。Windows の rename は上書きしない。"""
    for c in candidates(folder, stem, tail):
        try:
            os.rename(tmp, c)
            return c
        except FileExistsError:
            continue
    raise NamesExhaustedError


def make_unique_dir(parent: Path, name: str) -> Path:
    for c in candidates(parent, name, ""):
        try:
            c.mkdir()
            return c
        except FileExistsError:
            continue
    raise NamesExhaustedError


def same_file(a: Path, b: Path) -> bool:
    """同じ実体か(INV-1・FR-17)。どちらかが無ければ正規化したパスで比べる。"""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


# ------------------------------------------------------------------ 書けないときの保存先(§10)
def default_fallback() -> Path:
    """ドキュメント\\PagePress\\。"""
    from deskkit.modules.pagepress._win32 import documents_dir

    return documents_dir() / FALLBACK_SUBDIR


# ------------------------------------------------------------------ pending.json(FR-21)
class Pending:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._mu = threading.Lock()

    def _read(self) -> list[str]:
        try:
            obj = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        tmp = obj.get("tmp") if isinstance(obj, dict) else None
        return [str(x) for x in tmp] if isinstance(tmp, list) else []

    def _write(self, items: list[str]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            part = self.path.with_name(self.path.name + ".part")
            part.write_text(json.dumps({"tmp": items}, ensure_ascii=False), encoding="utf-8")
            os.replace(part, self.path)
        except OSError:
            pass  # 書けなくても本処理は止めない(次の起動で消せないだけ)

    def add(self, p: Path) -> None:
        with self._mu:
            items = self._read()
            items.append(str(p))
            self._write(items)

    def discard(self, p: Path) -> None:
        with self._mu:
            items = [x for x in self._read() if x != str(p)]
            self._write(items)

    def count(self) -> int:
        with self._mu:
            return len(self._read())

    def sweep(self) -> int:
        """起動時: 名前が `~pagepress-*.tmp` の物だけを消し、pending.json を空にする。消した数。"""
        with self._mu:
            n = 0
            for s in self._read():
                p = Path(s)
                if is_own_tmp(p) and p.is_file():
                    remove_own(p)
                    n += 1
            self._write([])
            return n
