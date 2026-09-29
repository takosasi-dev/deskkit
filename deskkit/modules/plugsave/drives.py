# ドライブの一覧・種類・ファイルシステム名・シリアル番号と、印(`DeskKitバックアップ\plugsave_id.json`)の読み書き(B-2・FR-4・FR-5・FR-8)。
# バックアップ先の中の並び(B-4)と、260 文字を超えるパスのための `\\?\`(FR-18)もここで作る。
# 書くのは印と「はじめにお読みください.txt」だけで、どちらも無いときに一時ファイル + 名前の変更で作る(上書きしない)。
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from deskkit.modules.plugsave import staging
from deskkit.modules.plugsave._win32 import (
    BACKUP_DRIVE_TYPES,
    DRIVE_REMOTE,
    SEM_FAILCRITICALERRORS,
    DriveApi,
    VolumeInfo,
    letters_of,
)

BACKUP_DIR = "DeskKitバックアップ"
MARKER = "plugsave_id.json"
README = "はじめにお読みください.txt"
OLD_DIR = "_以前の版"
APP_NAME = "DeskKit PlugSave"
MARKER_FORMAT = 1
ID_RE = re.compile(r"^[0-9a-f]{32}$")
FAT_FAMILY = frozenset({"FAT", "FAT32", "EXFAT"})
FAT32_ONLY = frozenset({"FAT", "FAT32"})
PROBE_RETRIES = 3
PROBE_RETRY_WAIT_S = 2.0
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

README_TEXT = """DeskKit の PlugSave(挿すだけバックアップ)が作ったフォルダです。

■ 中身
- <PC の名前>\\<コピー元の名前>\\ … コピー元のフォルダと同じ並びで、ファイルをコピーしています。
- <PC の名前>\\_以前の版\\<日時>\\ … 変わったファイルの、上書きする前の版です。
- <PC の名前>\\_作業中\\ … コピーの途中の一時ファイルです。次のバックアップのときに片づけます。
- plugsave_id.json … このドライブを見分けるための印です。消したり書き換えたりしないでください。

■ 大切なこと
- PlugSave は、このフォルダの中のファイルを消しません。上書きもしません。
- コピー元で消したファイルも、ここには残ります。
- _以前の版 は自動では消えません。要らなくなったら、ご自分で消してください。

■ 戻し方
エクスプローラーでこのフォルダを開き、戻したいファイルをコピーして、元の場所へ貼り付けてください。
古い版は _以前の版 の中の、日時の名前のフォルダにあります。
"""


# ------------------------------------------------------------------ パス
def long_path(p: str) -> str:
    """FR-18: `\\\\?\\` を付けた絶対パス。既に付いていればそのまま。"""
    if p.startswith("\\\\?\\"):
        return p
    a = os.path.abspath(p)
    if a.startswith("\\\\"):
        return "\\\\?\\UNC\\" + a[2:]
    return "\\\\?\\" + a


def display_path(p: str) -> str:
    """画面用に `\\\\?\\` を外す。"""
    if p.startswith("\\\\?\\UNC\\"):
        return "\\\\" + p[8:]
    if p.startswith("\\\\?\\"):
        return p[4:]
    return p


def safe_name(name: str, fallback: str) -> str:
    s = _BAD_CHARS.sub("_", name).strip().rstrip(".")
    return s or fallback


def unique_dir(parent: str, name: str) -> str:
    """parent\\name が既にあれば ` (2)` から番号を足す(§10 の同じ秒に2回)。"""
    cand = os.path.join(parent, name)
    n = 2
    while os.path.lexists(cand):
        cand = os.path.join(parent, f"{name} ({n})")
        n += 1
    return cand


def pc_name(env: Mapping[str, str] | None = None) -> str:
    src = os.environ if env is None else env
    return safe_name(src.get("COMPUTERNAME", "") or "", "この PC")


def backup_dir(root: str) -> str:
    return os.path.join(long_path(root), BACKUP_DIR)


def pc_dir(root: str, pc: str) -> str:
    return os.path.join(backup_dir(root), pc)


def fs_kind(fs: str) -> str:
    """ops.jsonl の fs(NTFS / exFAT / FAT32 / other)。"""
    u = fs.upper()
    if u == "NTFS":
        return "NTFS"
    if u == "EXFAT":
        return "exFAT"
    if u == "FAT32":
        return "FAT32"
    return "other"


def is_fat_family(fs: str) -> bool:
    return fs.upper() in FAT_FAMILY


def is_fat32(fs: str) -> bool:
    return fs.upper() in FAT32_ONLY


# ------------------------------------------------------------------ 印
@dataclass(frozen=True)
class MarkerRead:
    status: str            # "none"(無い)/ "ok" / "broken"(壊れている・形が違う)
    id: str | None = None


def read_marker(root: str) -> MarkerRead:
    """印を読む。無ければ none、形が違えば broken。読めない(OSError)は呼ぶ側でやり直す。"""
    path = os.path.join(backup_dir(root), MARKER)
    try:
        with open(path, "rb") as f:
            raw = f.read(4096)
    except FileNotFoundError:
        return MarkerRead("none")
    except NotADirectoryError:
        return MarkerRead("none")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return MarkerRead("broken")
    if not isinstance(obj, dict) or obj.get("app") != APP_NAME or obj.get("format") != MARKER_FORMAT:
        return MarkerRead("broken")
    mid = obj.get("id")
    if not isinstance(mid, str) or not ID_RE.match(mid):
        return MarkerRead("broken")
    return MarkerRead("ok", mid)


def write_new_file(folder: str, name: str, data: bytes, tmp_prefix: str) -> bool:
    """folder\\name が無いときだけ、一時ファイル + 名前の変更で作る。既にあれば何もしない(False)。
    名前の変更(os.rename)は同じ名前があると失敗するので上書きしない。失敗は OSError。"""
    final = os.path.join(folder, name)
    if os.path.lexists(final):
        return False
    tmp = os.path.join(folder, f"{tmp_prefix}.{secrets.token_hex(16)}.part")
    try:
        with open(tmp, "xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, final)
    except FileExistsError:
        staging.discard_part(tmp)
        return False
    except OSError:
        staging.discard_part(tmp)
        raise
    return True


def marker_bytes(mid: str) -> bytes:
    return json.dumps({"app": APP_NAME, "format": MARKER_FORMAT, "id": mid}, ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class RegisterOutcome:
    id: str
    reused: bool            # 既にあった印の ID を使った
    readme_written: bool


def prepare_drive(root: str, *, replace_broken: bool = False, now: _dt.datetime | None = None) -> RegisterOutcome:
    """FR-5・FR-24: `DeskKitバックアップ\\` を作り、印が無ければ書く。あればその ID を使う(書き換えない)。
    印が壊れているときは replace_broken=True のときだけ、壊れた印を別の名前に移してから新しい印を書く(消さない)。
    失敗は OSError(書き込み禁止など)。印が壊れていて replace_broken=False なら ValueError。"""
    bdir = backup_dir(root)
    os.makedirs(bdir, exist_ok=True)
    m = read_marker(root)
    if m.status == "broken":
        if not replace_broken:
            raise ValueError("broken marker")
        # 壊れた印は消さず、`DeskKitバックアップ\_以前の版\<日時>\` へ名前の変更で移す(INV-2 の「_以前の版 への名前の変更」)
        old_dir = unique_dir(os.path.join(bdir, OLD_DIR), (now or _dt.datetime.now()).strftime("%Y-%m-%d_%H%M%S"))
        os.makedirs(old_dir, exist_ok=False)
        os.rename(os.path.join(bdir, MARKER), os.path.join(old_dir, MARKER))
        m = MarkerRead("none")
    reused = m.status == "ok"
    if m.status == "ok" and m.id is not None:
        mid = m.id
    else:
        mid = secrets.token_hex(16)
        if not write_new_file(bdir, MARKER, marker_bytes(mid), "plugsave_id"):
            # 同時に誰かが作った: その印を使う
            again = read_marker(root)
            if again.status != "ok" or again.id is None:
                raise OSError("marker race")
            mid, reused = again.id, True
    readme = write_new_file(bdir, README, README_TEXT.replace("\n", "\r\n").encode("utf-8-sig"), "readme")
    return RegisterOutcome(mid, reused, readme)


# ------------------------------------------------------------------ ドライブを調べる
@dataclass(frozen=True)
class DriveProbe:
    letter: str
    root: str
    drive_type: int
    marker: MarkerRead
    volume: VolumeInfo | None
    readable: bool = True          # False = やり直しても印かボリューム情報を読めなかった


def probe_letter(api: DriveApi, letter: str, *, retries: int = PROBE_RETRIES, wait_s: float = PROBE_RETRY_WAIT_S,
                 sleep: Callable[[float], None] = time.sleep, need_volume_without_marker: bool = False) -> DriveProbe | None:
    """FR-8: SetThreadErrorMode → GetDriveTypeW(2・3 以外は None)→ 印 → GetVolumeInformationW。
    印が無ければ(need_volume_without_marker が偽なら)ボリューム情報を読まずに返す。読めなければ wait_s あけて最大 retries 回やり直す。
    別スレッドで呼ぶ(待ちがある)。"""
    root = api.root(letter)
    old = api.set_thread_error_mode(SEM_FAILCRITICALERRORS)
    try:
        t = api.drive_type(root)
        if t not in BACKUP_DRIVE_TYPES:
            return None
        marker = MarkerRead("none")
        vol: VolumeInfo | None = None
        for attempt in range(retries + 1):
            if attempt:
                sleep(wait_s)
            try:
                marker = read_marker(root)
            except OSError:
                continue
            if marker.status != "ok" and not need_volume_without_marker:
                return DriveProbe(letter, root, t, marker, None)
            vol = api.volume_info(root)
            if vol is not None:
                return DriveProbe(letter, root, t, marker, vol)
        return DriveProbe(letter, root, t, marker, vol, readable=False)
    finally:
        if old >= 0:
            api.set_thread_error_mode(old)


def system_letter(env: Mapping[str, str] | None = None) -> str:
    src = os.environ if env is None else env
    sd = (src.get("SystemDrive") or "C:").strip()
    return sd[:1].upper() if sd else "C"


def letter_of_path(path: str) -> str | None:
    d = os.path.splitdrive(display_path(os.path.abspath(path)))[0]
    if len(d) == 2 and d[1] == ":":
        return d[0].upper()
    return None


def is_remote_path(api: DriveApi, path: str) -> bool:
    p = display_path(os.path.abspath(path))
    if p.startswith("\\\\"):
        return True
    letter = letter_of_path(p)
    if letter is None:
        return True
    return api.drive_type(api.root(letter)) == DRIVE_REMOTE


def scan_letters(api: DriveApi, *, skip: set[str] | None = None, retries: int = 0,
                 need_volume_without_marker: bool = False) -> list[DriveProbe]:
    """つながっている 2・3 の種類のドライブを調べる(別スレッドで)。skip の文字は調べない。"""
    old = api.set_thread_error_mode(SEM_FAILCRITICALERRORS)
    try:
        out: list[DriveProbe] = []
        for letter in letters_of(api.logical_drives()):
            if skip and letter in skip:
                continue
            p = probe_letter(api, letter, retries=retries, need_volume_without_marker=need_volume_without_marker)
            if p is not None:
                out.append(p)
        return out
    finally:
        if old >= 0:
            api.set_thread_error_mode(old)
