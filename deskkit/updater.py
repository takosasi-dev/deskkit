# GitHub Releases からの更新(利用者の指示で C-6「通信しない」の唯一の例外。設定でオフにできる)。
# 接続先は api.github.com と GitHub のダウンロード用ホストだけ。SHA-256 が一致した exe だけを入れ替え、
# 入れ替え前に新しい exe が起動できることを確かめ、旧版は DeskKit.previous.exe として残す(巻き戻し用)。
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deskkit import __version__

DEFAULT_REPO = "takosasi-dev/deskkit"
ASSET_EXE = "DeskKit.exe"
ASSET_SHA = "DeskKit.exe.sha256"
PREVIOUS_NAME = "DeskKit.previous.exe"
ALLOWED_HOSTS = {"api.github.com", "github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
MAX_EXE_BYTES = 400 * 1024 * 1024
_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag: str
    notes: str
    html_url: str
    published_at: str
    exe_url: str | None
    exe_size: int
    sha_url: str | None


def parse_version(text: str) -> tuple[int, ...] | None:
    """'v1.2.3' → (1, 2, 3)。数字以外を含む(プレリリース等)なら None。"""
    m = re.fullmatch(r"v?(\d+(?:\.\d+){0,3})", text.strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def is_newer(remote: str, local: str = __version__) -> bool:
    r, lo = parse_version(remote), parse_version(local)
    if r is None or lo is None:
        return False
    n = max(len(r), len(lo))
    return r + (0,) * (n - len(r)) > lo + (0,) * (n - len(lo))


def valid_repo(repo: str) -> bool:
    return bool(_REPO_RE.match(repo.strip()))


def _check_url(url: str) -> None:
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or (u.hostname or "") not in ALLOWED_HOSTS:
        raise UpdateError(f"許可していない接続先です: {u.hostname}")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """リダイレクト先を「たどる前に」検査する(http や許可リスト外のホストへは一度も接続しない)。"""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SafeRedirect)


def _open(url: str, timeout: float, accept: str) -> Any:
    _check_url(url)
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": f"DeskKit/{__version__}"})
    resp = _OPENER.open(req, timeout=timeout)  # https と接続先を上で検査済み。リダイレクト先も _SafeRedirect で検査
    _check_url(resp.geturl())  # 念のため最終的な接続先も検査する
    return resp


def fetch_latest(repo: str, timeout: float = 15) -> ReleaseInfo:
    if not valid_repo(repo):
        raise UpdateError("リポジトリ名は「ユーザー名/リポジトリ名」の形で指定してください")
    try:
        with _open(f"https://api.github.com/repos/{repo}/releases/latest", timeout, "application/vnd.github+json") as r:
            data = json.loads(r.read(2_000_000).decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("公開されているリリースが見つかりません") from e
        raise UpdateError(f"GitHub が応答しません(HTTP {e.code})") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise UpdateError("GitHub に接続できません(オフラインの可能性)") from e
    except ValueError as e:
        raise UpdateError("GitHub の応答を読めません") from e
    tag = str(data.get("tag_name", ""))
    exe_url = sha_url = None
    exe_size = 0
    for a in data.get("assets", []) or []:
        name = str(a.get("name", ""))
        if name == ASSET_EXE:
            exe_url = str(a.get("browser_download_url", ""))
            exe_size = int(a.get("size", 0) or 0)
        elif name == ASSET_SHA:
            sha_url = str(a.get("browser_download_url", ""))
    return ReleaseInfo(
        version=tag.lstrip("vV"), tag=tag, notes=str(data.get("body") or ""), html_url=str(data.get("html_url", "")),
        published_at=str(data.get("published_at", "")), exe_url=exe_url, exe_size=exe_size, sha_url=sha_url,
    )


def _expected_sha(info: ReleaseInfo, timeout: float) -> str:
    if not info.sha_url:
        raise UpdateError("このリリースには検証用の DeskKit.exe.sha256 が無いため、自動更新できません")
    with _open(info.sha_url, timeout, "application/octet-stream") as r:
        text = r.read(4096).decode("utf-8", "replace")
    m = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    if not m:
        raise UpdateError("DeskKit.exe.sha256 の書式を読めません")
    return m.group(1).lower()


def download(info: ReleaseInfo, dest_dir: Path, progress: Callable[[int, int], None] | None = None,
             cancelled: Callable[[], bool] | None = None, timeout: float = 30) -> Path:
    """exe をダウンロードして SHA-256 と PE ヘッダを検証し、検証済みファイルのパスを返す。"""
    if not info.exe_url:
        raise UpdateError("このリリースには DeskKit.exe が添付されていません")
    if info.exe_size > MAX_EXE_BYTES:
        raise UpdateError("ファイルが大きすぎます")
    expected = _expected_sha(info, timeout)
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / f"DeskKit-{info.version}.exe"
    part = final.with_suffix(".part")
    h = hashlib.sha256()
    got = 0
    try:
        with _open(info.exe_url, timeout, "application/octet-stream") as r, part.open("wb") as f:
            total = int(r.headers.get("Content-Length") or info.exe_size or 0)
            while True:
                if cancelled is not None and cancelled():
                    raise UpdateError("キャンセルしました")
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                got += len(chunk)
                if got > MAX_EXE_BYTES:
                    raise UpdateError("ファイルが大きすぎます")
                h.update(chunk)
                f.write(chunk)
                if progress is not None:
                    progress(got, total)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        part.unlink(missing_ok=True)
        raise UpdateError("ダウンロードに失敗しました") from e
    except UpdateError:
        part.unlink(missing_ok=True)
        raise
    if h.hexdigest() != expected:
        part.unlink(missing_ok=True)
        raise UpdateError("ダウンロードしたファイルの SHA-256 が一致しません(破損または改ざんの可能性)")
    with part.open("rb") as f:
        if f.read(2) != b"MZ":
            part.unlink(missing_ok=True)
            raise UpdateError("実行ファイルではありません")
    os.replace(part, final)
    return final


def verify_runs(exe: Path, expected_version: str, timeout: float = 120) -> None:
    """新しい exe を --version-file 付きで起動し、正しい版数を書いて終了するか確かめる。"""
    fd, out = tempfile.mkstemp(prefix="deskkit-ver-", suffix=".txt")
    os.close(fd)
    try:
        flags = 0x08000000  # CREATE_NO_WINDOW
        subprocess.run([str(exe), "--version-file", out], timeout=timeout, check=True, creationflags=flags)
        got = Path(out).read_text(encoding="utf-8").strip()
    except (subprocess.SubprocessError, OSError) as e:
        raise UpdateError("新しい版を試しに起動できませんでした") from e
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass
    if got != expected_version:
        raise UpdateError(f"新しい版の版数が一致しません(期待 {expected_version} / 実際 {got})")


def current_exe() -> Path | None:
    return Path(sys.executable) if getattr(sys, "frozen", False) else None


def previous_exe() -> Path | None:
    cur = current_exe()
    if cur is None:
        return None
    p = cur.with_name(PREVIOUS_NAME)
    return p if p.exists() else None


def _file_sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


_RETRY_WINERRORS = {5, 32, 33}  # ERROR_ACCESS_DENIED / ERROR_SHARING_VIOLATION / ERROR_LOCK_VIOLATION(ウイルス対策の一時的なロック)


def _replace(src: Path, dst: Path, tries: int = 8, delay: float = 0.25) -> None:
    """os.replace を、ウイルス対策ソフトなどの一時的なロックの間だけ少し待って再試行する。"""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except OSError as e:
            if i == tries - 1 or getattr(e, "winerror", None) not in _RETRY_WINERRORS:
                raise
            time.sleep(delay)


def swap_in(new_exe: Path) -> Path:
    """実行中の exe を DeskKit.previous.exe に改名し、新しい exe をその場所に置く。失敗したら元に戻す。
    どの失敗でも UpdateError にし、DeskKit.exe が無くなる状態を残さない(戻せないときは手順を示す)。"""
    cur = current_exe()
    if cur is None:
        raise UpdateError("開発版(python 実行)では入れ替えできません")
    prev = cur.with_name(PREVIOUS_NAME)
    staged = cur.with_name(cur.name + ".new")
    try:
        shutil.copyfile(new_exe, staged)
        if _file_sha(staged) != _file_sha(new_exe):
            raise UpdateError("コピーした exe の検証に失敗しました")
    except OSError as e:
        staged.unlink(missing_ok=True)
        raise UpdateError(f"{cur.parent} に書き込めません。DeskKit.exe を書き込みできるフォルダ(例: ドキュメント)に置いてください") from e
    try:
        if prev.exists():
            prev.unlink()  # 1世代前の自分自身の退避ファイルだけを消す
        _replace(cur, prev)  # 実行中の exe でも改名はできる
    except OSError as e:
        staged.unlink(missing_ok=True)
        raise UpdateError("今の DeskKit.exe を退避できませんでした(ウイルス対策ソフトが検査中の可能性。少し待ってからやり直してください)") from e
    try:
        _replace(staged, cur)
    except OSError as e:
        try:
            _replace(prev, cur)
        except OSError as e2:
            # 元にも戻せない: 新しい exe(.new)と旧版(previous)は残っているので、利用者に名前の付け直しを頼む
            raise UpdateError(f"新しい exe を置けず、元の版にも戻せませんでした。{cur.parent} の {PREVIOUS_NAME} を "
                              f"{cur.name} に名前を変えてください") from e2
        staged.unlink(missing_ok=True)
        raise UpdateError("新しい exe を置けませんでした(元の版に戻しました)") from e
    return cur


def swap_back() -> Path:
    """DeskKit.previous.exe と今の exe を入れ替える(前の版に戻す)。
    3段階(今→.swap、前→今、.swap→前)のうち2段階目までできれば「戻せた」とみなす(3段階目の失敗は .swap が残るだけ)。"""
    cur = current_exe()
    prev = previous_exe()
    if cur is None or prev is None:
        raise UpdateError("前の版が残っていません")
    tmp = cur.with_name(cur.name + ".swap")
    try:
        _replace(cur, tmp)
    except OSError as e:
        raise UpdateError("前の版に戻せませんでした(今の exe を退避できません)") from e
    try:
        _replace(prev, cur)
    except OSError as e:
        try:
            _replace(tmp, cur)
        except OSError as e2:
            raise UpdateError(f"前の版に戻せず、今の版も元の名前に戻せませんでした。{cur.parent} の {tmp.name} を "
                              f"{cur.name} に名前を変えてください") from e2
        raise UpdateError("前の版に戻せませんでした(元のままです)") from e
    try:
        _replace(tmp, prev)
    except OSError:
        pass  # DeskKit.exe は前の版になっている。.swap が残るだけ(次の swap_in / cleanup で扱う)
    return cur


def relaunch(exe: Path, extra: list[str]) -> None:
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen([str(exe), "--post-update", str(os.getpid()), *extra], creationflags=flags, close_fds=True)
    except OSError as e:
        raise UpdateError(f"新しい DeskKit を起動できませんでした。{exe.name} を手で起動してください") from e


def wait_for_pid_exit(pid: int, timeout_ms: int = 30000) -> None:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if h:
        try:
            k32.WaitForSingleObject(h, timeout_ms)
        finally:
            k32.CloseHandle(h)


def cleanup_downloads(dest_dir: Path) -> None:
    if not dest_dir.exists():
        return
    for p in [*dest_dir.glob("DeskKit-*.exe*"), *dest_dir.glob("DeskKit-*.part")]:
        try:
            p.unlink()  # 自分でダウンロードした更新ファイルだけ
        except OSError:
            pass
