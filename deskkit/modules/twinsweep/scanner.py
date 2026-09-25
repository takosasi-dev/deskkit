# 調べる写真ファイルの列挙(FR-3〜FR-5・§10)。調べてはいけないフォルダの判定、リパースポイントをたどらない走査、
# 隠し・システム・10KB 未満・クラウドにだけあるファイルの除外、入れ子のフォルダの重複除去、10 万枚の上限を持つ。
# ファイルは開かない(DirEntry の情報だけを使う)。ログには件数だけを書く(INV-4)。
from __future__ import annotations

import os
import stat as _stat
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

IMAGE_EXTENSIONS: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".webp", ".heic", ".heif", ".bmp", ".dib", ".gif", ".tif", ".tiff",
})
MIN_BYTES = 10 * 1024          # FR-4: 10KB 未満は数えない
MAX_FILES = 100_000            # FR-5
MAX_ROOTS = 10                 # FR-1

# --- winnt.h のファイル属性
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
CLOUD_ONLY_ATTRS = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | FILE_ATTRIBUTE_RECALL_ON_OPEN
# --- リパースポイントの種類
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003  # ジャンクション
IO_REPARSE_TAG_SYMLINK = 0xA000000C
IO_REPARSE_TAG_CLOUD = 0x9000001A        # OneDrive などのクラウドファイル(0x9000?01A の系列)
_CLOUD_MASK = 0xFFFF0FFF

EXCLUDED_ENV_VARS = ("WINDIR", "ProgramFiles", "ProgramFiles(x86)", "ProgramData", "APPDATA", "LOCALAPPDATA")

RootCheck = str  # "ok" / "forbidden" / "drive_root" / "missing"


def norm(p: str | Path) -> str:
    """比較用の正規化(実パス・大文字小文字無視・末尾の区切りなし)。"""
    s = os.path.normcase(os.path.realpath(str(p)))
    return s.rstrip("\\/") if len(s) > 3 else s


def excluded_dirs(env: Mapping[str, str] | None = None, extra: Iterable[Path] = ()) -> list[str]:
    """FR-3 の調べないフォルダ(正規化済み)。環境変数が無いものは飛ばす。"""
    src = os.environ if env is None else env
    out: list[str] = []
    for k in EXCLUDED_ENV_VARS:
        v = src.get(k)
        if v:
            out.append(norm(v))
    out.extend(norm(p) for p in extra)
    return sorted(set(out))


def _inside(path_n: str, base_n: str) -> bool:
    if path_n == base_n:
        return True
    prefix = base_n if base_n.endswith(("\\", "/")) else base_n + os.sep
    return path_n.startswith(prefix)


def is_drive_root(p: str | Path) -> bool:
    pp = Path(os.path.realpath(str(p)))
    return pp.parent == pp


def check_root(p: str | Path, excluded: Sequence[str]) -> RootCheck:
    """選ばれたフォルダを調べてよいか。forbidden は FR-3 のフォルダかその中。drive_root は確認が要る。"""
    if not os.path.isdir(str(p)):
        return "missing"
    n = norm(p)
    if any(_inside(n, e) for e in excluded):
        return "forbidden"
    if is_drive_root(p):
        return "drive_root"
    return "ok"


@dataclass(frozen=True)
class FileEntry:
    path: str
    root: str
    size: int
    mtime_ns: int


@dataclass
class Listing:
    files: list[FileEntry] = field(default_factory=list)
    cloud_only: int = 0
    too_many: bool = False
    cancelled: bool = False
    forbidden_roots: int = 0
    missing_roots: int = 0


def _is_cloud_tag(tag: int) -> bool:
    return (tag & _CLOUD_MASK) == IO_REPARSE_TAG_CLOUD


def _follow_dir(attrs: int, tag: int) -> bool:
    """ディレクトリに入ってよいか。ジャンクション・シンボリックリンクなどのリパースポイントはたどらない(FR-4)。
    OneDrive のフォルダはクラウドの種類のリパースポイントなので入る(中のオンラインのみのファイルは後で飛ばす)。"""
    if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
        return _is_cloud_tag(tag)
    return True


def _file_is_link(attrs: int, tag: int) -> bool:
    return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT) and tag in (IO_REPARSE_TAG_SYMLINK, IO_REPARSE_TAG_MOUNT_POINT)


def enumerate_images(roots: Sequence[str | Path], *, recursive: bool, excluded: Sequence[str],
                     cancel: threading.Event | None = None, max_files: int = MAX_FILES,
                     on_count: Callable[[int], None] | None = None) -> Listing:
    """roots の下の画像ファイルを列挙する。roots は呼ぶ側で check_root 済みのものを渡す(ここでも forbidden は飛ばす)。"""
    out = Listing()
    seen: set[str] = set()
    # 外側のフォルダを先に調べる(入れ子のときに、相対パスが外側のフォルダ基準になる)
    uniq: dict[str, str] = {}
    for r in roots:
        n = norm(r)
        uniq.setdefault(n, os.path.realpath(str(r)))
    ordered = sorted(uniq.items(), key=lambda kv: len(kv[0]))
    for root_n, root in ordered:
        if cancel is not None and cancel.is_set():
            out.cancelled = True
            return out
        if not os.path.isdir(root):
            out.missing_roots += 1
            continue
        if any(_inside(root_n, e) for e in excluded):
            out.forbidden_roots += 1
            continue
        stack = [root]
        while stack:
            if cancel is not None and cancel.is_set():
                out.cancelled = True
                return out
            d = stack.pop()
            try:
                it = os.scandir(d)
            except OSError:
                continue
            subdirs: list[str] = []
            with it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    attrs = int(getattr(st, "st_file_attributes", 0))
                    tag = int(getattr(st, "st_reparse_tag", 0))
                    if _stat.S_ISDIR(st.st_mode) or (attrs & 0x10):
                        if not recursive:
                            continue
                        if attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM):
                            continue  # $RECYCLE.BIN・System Volume Information などを含む(docs/v0.3/twinsweep.md T-2)
                        if not _follow_dir(attrs, tag):
                            continue
                        sub_n = os.path.normcase(e.path)
                        if any(_inside(sub_n, x) for x in excluded):
                            continue
                        subdirs.append(e.path)
                        continue
                    if e.is_symlink() or _file_is_link(attrs, tag):
                        continue
                    if os.path.splitext(e.name)[1].lower() not in IMAGE_EXTENSIONS:
                        continue
                    if attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM):
                        continue
                    if st.st_size < MIN_BYTES:
                        continue
                    key = os.path.normcase(e.path)
                    if key in seen:
                        continue
                    if attrs & CLOUD_ONLY_ATTRS:
                        seen.add(key)
                        out.cloud_only += 1
                        continue
                    seen.add(key)
                    if len(out.files) >= max_files:
                        out.too_many = True
                        return out
                    out.files.append(FileEntry(e.path, root, int(st.st_size), int(st.st_mtime_ns)))
                    if on_count is not None and len(out.files) % 500 == 0:
                        on_count(len(out.files))
            stack.extend(reversed(sorted(subdirs)))
    return out
