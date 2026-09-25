# updater の単体テスト(版数比較・接続先の制限・SHA-256 検証・exe の入れ替えと巻き戻し)。通信はしない。
from __future__ import annotations

import hashlib
import io
import sys
from pathlib import Path
from typing import Any

import pytest

from deskkit import updater
from deskkit.updater import ReleaseInfo, UpdateError


@pytest.mark.parametrize(("remote", "local", "newer"), [
    ("v0.2.0", "0.1.0", True), ("0.1.1", "0.1.0", True), ("v1.0", "0.9.9", True),
    ("v0.1.0", "0.1.0", False), ("v0.0.9", "0.1.0", False), ("v0.2.0-beta", "0.1.0", False), ("garbage", "0.1.0", False),
])
def test_is_newer(remote: str, local: str, newer: bool) -> None:
    assert updater.is_newer(remote, local) is newer


def test_url_allowlist() -> None:
    updater._check_url("https://api.github.com/repos/a/b/releases/latest")
    for bad in ("http://github.com/x", "https://evil.example.com/DeskKit.exe", "https://github.com.evil.io/x"):
        with pytest.raises(UpdateError):
            updater._check_url(bad)


def test_repo_validation() -> None:
    assert updater.valid_repo("takosasi-dev/deskkit")
    for bad in ("", "noslash", "a/b/c", "../x", "a b/c"):
        assert not updater.valid_repo(bad)


class _Resp(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: Any) -> None:
        self.close()


def _info() -> ReleaseInfo:
    return ReleaseInfo("0.2.0", "v0.2.0", "", "", "", "https://github.com/x/DeskKit.exe", 0,
                       "https://github.com/x/DeskKit.exe.sha256")


def test_download_verifies_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"MZ" + b"\0" * 5000
    digest = hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(updater, "_open", lambda url, t, a: _Resp((digest + "  DeskKit.exe\n").encode() if url.endswith(".sha256") else body))
    p = updater.download(_info(), tmp_path)
    assert p.read_bytes() == body


def test_download_rejects_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updater, "_open", lambda url, t, a: _Resp(("0" * 64).encode() if url.endswith(".sha256") else b"MZabc"))
    with pytest.raises(UpdateError):
        updater.download(_info(), tmp_path)
    assert not list(tmp_path.glob("DeskKit-*"))


def test_download_requires_sha_asset(tmp_path: Path) -> None:
    info = ReleaseInfo("0.2.0", "v0.2.0", "", "", "", "https://github.com/x/DeskKit.exe", 0, None)
    with pytest.raises(UpdateError):
        updater.download(info, tmp_path)


def test_swap_in_and_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cur = tmp_path / "DeskKit.exe"
    cur.write_bytes(b"MZold")
    new = tmp_path / "dl" / "DeskKit-0.2.0.exe"
    new.parent.mkdir()
    new.write_bytes(b"MZnew")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(cur))
    updater.swap_in(new)
    assert cur.read_bytes() == b"MZnew"
    assert (tmp_path / updater.PREVIOUS_NAME).read_bytes() == b"MZold"
    updater.swap_back()
    assert cur.read_bytes() == b"MZold"
    assert (tmp_path / updater.PREVIOUS_NAME).read_bytes() == b"MZnew"
