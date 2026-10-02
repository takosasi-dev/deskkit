# コピー元の走査とバックアップ先との比べ(B-6・FR-12〜FR-15)。各ファイルを「新しい」「変わった」「そのまま」「飛ばす(理由)」に分けた計画を作る。
# コピー元は開かない(os.scandir の DirEntry.stat(follow_symlinks=False) だけ)。先は1フォルダにつき1回 scandir して比べる。
# ジャンクション・シンボリックリンクのフォルダに入らない。クラウドの種類のリパースポイントのフォルダには入る(TwinSweep と同じ判定)。
from __future__ import annotations

import datetime as _dt
import heapq
import itertools
import os
import stat as _stat
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from deskkit.modules.plugsave import drives

MAX_PLAN_FILES = 300_000          # FR-15
MAX_ROWS = 5_000                  # 画面の表に持つ飛ばした・失敗したファイルの行の上限(件数は全部数える)
FAT32_LIMIT = 4_294_967_296       # FR-14: これ以上は FAT・FAT32 に置けない
TOL_FAT_NS = 2_000_000_000        # B-6: FAT・FAT32・exFAT は 2 秒
TOL_OTHER_NS = 1_000_000          # B-6: それ以外は 1 ミリ秒

# --- winnt.h のファイル属性
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
CLOUD_ONLY_ATTRS = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
# --- リパースポイントの種類
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
IO_REPARSE_TAG_SYMLINK = 0xA000000C
IO_REPARSE_TAG_CLOUD = 0x9000001A
_CLOUD_MASK = 0xFFFF0FFF

EXCLUDED_NAMES = frozenset({"thumbs.db", "desktop.ini"})
EXCLUDED_ENV_VARS = ("WINDIR", "ProgramFiles", "ProgramFiles(x86)", "ProgramData", "APPDATA", "LOCALAPPDATA")
scan_source = os.scandir   # コピー元の走査に使う(テストでクラウドの属性を持つ偽の項目に替える)

# 理由コード(§9)
R_CLOUD_ONLY = "cloud_only"
R_IN_USE = "in_use"
R_UNREADABLE = "unreadable"
R_TOO_LARGE = "too_large_fat32"
R_LINK = "link"
R_EXCLUDED_NAME = "excluded_name"
R_CHANGED_DURING_COPY = "changed_during_copy"
R_OLD_MOVE_FAILED = "old_version_move_failed"
R_WRITE_FAILED = "write_failed"
REASONS = (R_CLOUD_ONLY, R_IN_USE, R_UNREADABLE, R_TOO_LARGE, R_LINK, R_EXCLUDED_NAME, R_CHANGED_DURING_COPY,
           R_OLD_MOVE_FAILED, R_WRITE_FAILED)
FAILED_REASONS = frozenset({R_OLD_MOVE_FAILED, R_WRITE_FAILED})
QUIET_REASONS = frozenset({R_EXCLUDED_NAME, R_LINK})   # FR-23: これだけなら通知は ok のまま
REASON_TEXT = {
    R_CLOUD_ONLY: "クラウドにだけある",
    R_IN_USE: "ほかのアプリが使っている",
    R_UNREADABLE: "読めない",
    R_TOO_LARGE: "大きすぎて FAT32 のドライブに置けない",
    R_LINK: "リンク(ショートカットの一種)",
    R_EXCLUDED_NAME: "一時ファイル・Windows の管理用ファイル",
    R_CHANGED_DURING_COPY: "コピーの途中で変わった",
    R_OLD_MOVE_FAILED: "前の版を移せなかった",
    R_WRITE_FAILED: "書き込めなかった",
}


def fat_min_ns() -> int:
    """FAT の仲間が持てる最も古い日時(1980-01-01 の現地時刻)。"""
    return int(_dt.datetime(1980, 1, 1).timestamp()) * 1_000_000_000


@dataclass(frozen=True)
class Source:
    path: str
    name: str


NEW = "new"
CHANGED = "changed"
VERIFY = "verify"        # 大きさ・更新日時は同じだが、止まった回に書いた小さいファイル(中身を比べ、違えばもう一度コピー。v0.4.1)


@dataclass(frozen=True)
class PlanItem:
    kind: str            # "new" / "changed" / "verify"
    src: str             # コピー元(\\?\ 付き)
    rel: str             # コピー元の中の相対パス
    source_name: str     # 先のフォルダ名(FR-3)
    size: int
    mtime_ns: int
    atime_ns: int
    birth_ns: int = 0    # 確かめる物だけ: 先のファイルの作成日時(確かめる順・確かめ終えたところに使う)


@dataclass
class Plan:
    items: list[PlanItem] = field(default_factory=list)      # コピーする物(新しい・変わった)。上限 max_files
    verify: list[PlanItem] = field(default_factory=list)     # 確かめる物(作成日時の古い順)。上限 max_verify(別に数える)
    unchanged: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    rows: list[tuple[str, str]] = field(default_factory=list)   # (画面用のパス, 理由コード)
    truncated: bool = False
    missing_sources: int = 0
    total_sources: int = 0
    seen: int = 0
    cancelled: bool = False
    verify_truncated: bool = False   # 確かめる物が上限を超えた(作成日時の古い方から上限まで取った)
    verify_from: int = 0             # verify_truncated のとき、取らなかった物の中で最も古い作成日時
    unreadable_dirs: int = 0         # 途中で読めなかったフォルダ(コピー元・先)
    suspect_skipped: int = 0         # 確かめるべきだが確かめられない物(コピー元がクラウドにだけある)

    @property
    def new_count(self) -> int:
        return sum(1 for i in self.items if i.kind == NEW)

    @property
    def changed_count(self) -> int:
        return sum(1 for i in self.items if i.kind == CHANGED)

    @property
    def verify_count(self) -> int:
        return len(self.verify)

    @property
    def copy_bytes(self) -> int:
        """コピーする大きさ(確かめるだけの物は数えない。中身が違ってコピーし直す分はコピー中の空きの確認で見る)。"""
        return sum(i.size for i in self.items)

    @property
    def scan_complete(self) -> bool:
        """コピー元を全部たどれた(止めていない・切り詰めていない・見つからない/読めない所が無い・確かめられない物が無い)。"""
        return not (self.cancelled or self.truncated or self.missing_sources or self.unreadable_dirs or self.suspect_skipped)

    def skip(self, path: str, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1
        if len(self.rows) < MAX_ROWS:
            self.rows.append((drives.display_path(path), reason))


def norm(p: str) -> str:
    """比べる用(\\?\\ を外し、大文字小文字を無視し、末尾の区切りなし)。"""
    s = os.path.normcase(drives.display_path(os.path.abspath(p)))
    return s.rstrip("\\/") if len(s) > 3 else s


def inside(path_n: str, base_n: str) -> bool:
    if path_n == base_n:
        return True
    prefix = base_n if base_n.endswith(("\\", "/")) else base_n + os.sep
    return path_n.startswith(prefix)


def excluded_dirs(env: Mapping[str, str] | None = None, extra: Iterable[str] = ()) -> list[str]:
    """FR-2 の足せないフォルダ(正規化済み)。走査でもこの中には入らない(§10)。"""
    src = os.environ if env is None else env
    out = [norm(v) for k in EXCLUDED_ENV_VARS if (v := src.get(k))]
    out.extend(norm(p) for p in extra)
    return sorted(set(out))


def _is_cloud_tag(tag: int) -> bool:
    return (tag & _CLOUD_MASK) == IO_REPARSE_TAG_CLOUD


def _enter_dir(attrs: int, tag: int) -> bool:
    """FR-13: 隠しとシステムの両方を持つフォルダ・クラウド以外のリパースポイント(ジャンクション・シンボリックリンク)に入らない。"""
    if (attrs & FILE_ATTRIBUTE_HIDDEN) and (attrs & FILE_ATTRIBUTE_SYSTEM):
        return False
    if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
        return _is_cloud_tag(tag)
    return True


def _is_link_file(e: os.DirEntry[str], attrs: int, tag: int) -> bool:
    if e.is_symlink():
        return True
    return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT) and tag in (IO_REPARSE_TAG_SYMLINK, IO_REPARSE_TAG_MOUNT_POINT)


def is_excluded_name(name: str) -> bool:
    return name.startswith("~$") or name.lower() in EXCLUDED_NAMES


def _birth(st: os.stat_result) -> int:
    v = getattr(st, "st_birthtime_ns", None)
    return int(v) if isinstance(v, int) and v > 0 else 0


def _dest_listing(path: str, plan: Plan | None = None) -> dict[str, tuple[bool, int, int, int]]:
    """先のフォルダの中身: 名前(normcase)→(フォルダか, 大きさ, 更新日時 ns, 作成日時 ns(読めなければ 0))。無い・読めないときは空。
    無い(まだコピーしていない)以外の理由で読めなければ plan.unreadable_dirs を数える。"""
    out: dict[str, tuple[bool, int, int, int]] = {}
    try:
        it = os.scandir(path)
    except (FileNotFoundError, NotADirectoryError):
        return out
    except OSError:
        if plan is not None:
            plan.unreadable_dirs += 1
        return out
    with it:
        for e in it:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            attrs = int(getattr(st, "st_file_attributes", 0))
            is_dir = _stat.S_ISDIR(st.st_mode) or bool(attrs & FILE_ATTRIBUTE_DIRECTORY)
            out[os.path.normcase(e.name)] = (is_dir, int(st.st_size), int(st.st_mtime_ns), _birth(st))
    return out


def same_file(src_size: int, src_mtime: int, dst_size: int, dst_mtime: int, fs: str) -> bool:
    """B-6: 大きさが同じで、更新日時の差が幅の中なら「そのまま」。先が FAT の仲間で 1980 年より前の日時なら大きさだけで比べる。"""
    if src_size != dst_size:
        return False
    fat = drives.is_fat_family(fs)
    if fat and (src_mtime < fat_min_ns() or dst_mtime < fat_min_ns()):
        return True
    tol = TOL_FAT_NS if fat else TOL_OTHER_NS
    return abs(src_mtime - dst_mtime) <= tol


def make_plan(sources: Sequence[Source], pc_dir: str, fs: str, *, excluded: Sequence[str] = (),
              cancel: threading.Event | None = None, on_count: Callable[[int], None] | None = None,
              max_files: int = MAX_PLAN_FILES, suspect: Callable[[int, int], bool] | None = None,
              max_verify: int = MAX_PLAN_FILES) -> Plan:
    """計画を作る。pc_dir は `<根>\\DeskKitバックアップ\\<PC の名前>`(\\?\\ 付き)。fs は先のファイルシステム名。
    max_files はコピーが要るファイル(新しい・変わった)の数の上限で、超えたらそこで切る(FR-15。docs/v0.4/plugsave.md S-3)。
    suspect(大きさ, 先の作成日時 ns)が真の「そのまま」は「確かめる」にする(前の回が途中で止まったとき。unfinished.py)。
    確かめる物は max_files に数えず、別に max_verify まで、先の作成日時の古い順に取る(超えた分は次の回。verify_from)。"""
    plan = Plan(total_sources=len(sources))
    heap: list[tuple[int, int, PlanItem]] = []    # (-作成日時, 通し番号, 項目): 作成日時の新しい物を捨てる
    try:
        _scan(plan, heap, sources, pc_dir, fs, excluded, cancel, on_count, max_files, suspect, max_verify)
    finally:
        plan.verify = sorted((x[2] for x in heap), key=lambda i: i.birth_ns)
    return plan


def _scan(plan: Plan, heap: list[tuple[int, int, PlanItem]], sources: Sequence[Source], pc_dir: str, fs: str,
          excluded: Sequence[str], cancel: threading.Event | None, on_count: Callable[[int], None] | None, max_files: int,
          suspect: Callable[[int, int], bool] | None, max_verify: int) -> None:
    fat32 = drives.is_fat32(fs)
    seq = itertools.count()

    def add_verify(item: PlanItem) -> None:
        heapq.heappush(heap, (-item.birth_ns, next(seq), item))
        if len(heap) > max(0, max_verify):
            dropped = heapq.heappop(heap)[2]
            plan.verify_truncated = True
            plan.verify_from = dropped.birth_ns if not plan.verify_from else min(plan.verify_from, dropped.birth_ns)

    for src in sources:
        if cancel is not None and cancel.is_set():
            plan.cancelled = True
            return
        root = drives.long_path(src.path)
        if not os.path.isdir(root) or any(inside(norm(root), x) for x in excluded):
            plan.missing_sources += 1
            continue
        dest_base = os.path.join(pc_dir, src.name)
        seen_rel: set[str] = set()
        stack: list[tuple[str, str]] = [(root, "")]
        first = True
        while stack:
            if cancel is not None and cancel.is_set():
                plan.cancelled = True
                return
            d, rel_d = stack.pop()
            try:
                it = scan_source(d)
            except OSError:
                if first:
                    plan.missing_sources += 1
                else:
                    plan.unreadable_dirs += 1
                first = False
                continue
            first = False
            dest_now = _dest_listing(os.path.join(dest_base, rel_d) if rel_d else dest_base, plan)
            subdirs: list[tuple[str, str]] = []
            with it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    attrs = int(getattr(st, "st_file_attributes", 0))
                    tag = int(getattr(st, "st_reparse_tag", 0))
                    rel = os.path.join(rel_d, e.name) if rel_d else e.name
                    if _stat.S_ISDIR(st.st_mode) or (attrs & FILE_ATTRIBUTE_DIRECTORY):
                        if not _enter_dir(attrs, tag):
                            continue
                        if any(inside(norm(e.path), x) for x in excluded):
                            continue
                        subdirs.append((e.path, rel))
                        continue
                    plan.seen += 1
                    if on_count is not None and plan.seen % 500 == 0:
                        on_count(plan.seen)
                    if _is_link_file(e, attrs, tag):
                        plan.skip(e.path, R_LINK)
                        continue
                    if is_excluded_name(e.name):
                        plan.skip(e.path, R_EXCLUDED_NAME)
                        continue
                    if attrs & CLOUD_ONLY_ATTRS:
                        plan.skip(e.path, R_CLOUD_ONLY)   # 開かない(AC-6)
                        was = dest_now.get(os.path.normcase(e.name))
                        if suspect is not None and was is not None and not was[0] and suspect(was[1], was[3]):
                            plan.suspect_skipped += 1     # 先にある物を確かめられない(印を残す)
                        continue
                    size = int(st.st_size)
                    if fat32 and size >= FAT32_LIMIT:
                        plan.skip(e.path, R_TOO_LARGE)
                        continue
                    key = os.path.normcase(rel)
                    if key in seen_rel:
                        plan.skip(e.path, R_WRITE_FAILED)   # §10: 大文字・小文字だけ違う2つ目
                        continue
                    seen_rel.add(key)
                    mtime = int(st.st_mtime_ns)
                    got = dest_now.get(os.path.normcase(e.name))
                    if got is not None and not got[0] and same_file(size, mtime, got[1], got[2], fs):
                        if suspect is None or not suspect(got[1], got[3]):
                            plan.unchanged += 1
                        else:
                            add_verify(PlanItem(VERIFY, e.path, rel, src.name, size, mtime, int(st.st_atime_ns), got[3]))
                        continue
                    if len(plan.items) >= max_files:
                        plan.truncated = True
                        return
                    kind = NEW if got is None else CHANGED
                    plan.items.append(PlanItem(kind, e.path, rel, src.name, size, mtime, int(st.st_atime_ns)))
            stack.extend(reversed(sorted(subdirs)))
    if on_count is not None:
        on_count(plan.seen)


def fit_to_space(items: Sequence[PlanItem], budget: int) -> tuple[list[PlanItem], int]:
    """B-9「入る分だけコピー」: 更新日時の新しい順に、合計が budget を超えないところまで取る。(取った物, 残した数)。"""
    ordered = sorted(items, key=lambda i: i.mtime_ns, reverse=True)
    out: list[PlanItem] = []
    used = 0
    for it in ordered:
        if used + it.size > budget:
            break
        out.append(it)
        used += it.size
    return out, len(items) - len(out)
