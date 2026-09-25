# updater の単体テスト(版数比較・接続先の制限・SHA-256 検証・exe の入れ替えと巻き戻し)。通信はしない。
from __future__ import annotations

import hashlib
import io
import os
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


# ---- 入れ替え・巻き戻しの失敗経路(DeskKit.exe が無くなる状態を残さない)
def _frozen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    cur = tmp_path / "DeskKit.exe"
    cur.write_bytes(b"MZold")
    new = tmp_path / "dl" / "DeskKit-0.2.0.exe"
    new.parent.mkdir()
    new.write_bytes(b"MZnew")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(cur))
    monkeypatch.setattr(updater.time, "sleep", lambda _s: None)
    return cur, new


def _fail_when(monkeypatch: pytest.MonkeyPatch, pred: Any, winerror: int = 5) -> list[tuple[str, str]]:
    real = os.replace
    calls: list[tuple[str, str]] = []

    def fake(src: Any, dst: Any) -> None:
        calls.append((Path(src).name, Path(dst).name))
        if pred(Path(src), Path(dst)):
            e = PermissionError(13, "denied")
            e.winerror = winerror  # type: ignore[attr-defined]
            raise e
        real(src, dst)

    monkeypatch.setattr(updater.os, "replace", fake)
    return calls


def test_replace_retries_transient_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _frozen(tmp_path, monkeypatch)
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"x")
    n = {"i": 0}

    def flaky(src: Path, dst: Path) -> bool:
        n["i"] += 1
        return n["i"] <= 2

    _fail_when(monkeypatch, flaky, winerror=32)
    updater._replace(a, b)
    assert b.read_bytes() == b"x" and n["i"] == 3


def test_swap_in_restores_when_new_cannot_be_placed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cur, new = _frozen(tmp_path, monkeypatch)
    _fail_when(monkeypatch, lambda s, d: s.name.endswith(".new"))
    with pytest.raises(UpdateError, match="元の版に戻しました"):
        updater.swap_in(new)
    assert cur.read_bytes() == b"MZold"
    assert not (tmp_path / "DeskKit.exe.new").exists()


def test_swap_in_double_failure_is_update_error_with_instructions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cur, new = _frozen(tmp_path, monkeypatch)
    _fail_when(monkeypatch, lambda s, d: d.name == "DeskKit.exe")
    with pytest.raises(UpdateError, match=updater.PREVIOUS_NAME):
        updater.swap_in(new)
    assert (tmp_path / updater.PREVIOUS_NAME).read_bytes() == b"MZold"  # 旧版は残っている


def test_swap_back_third_step_failure_still_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cur, new = _frozen(tmp_path, monkeypatch)
    updater.swap_in(new)
    _fail_when(monkeypatch, lambda s, d: s.name.endswith(".swap"))
    assert updater.swap_back() == cur
    assert cur.read_bytes() == b"MZold"
    assert (tmp_path / "DeskKit.exe.swap").read_bytes() == b"MZnew"


def test_swap_back_second_step_failure_restores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cur, new = _frozen(tmp_path, monkeypatch)
    updater.swap_in(new)
    _fail_when(monkeypatch, lambda s, d: s.name == updater.PREVIOUS_NAME)
    with pytest.raises(UpdateError, match="元のまま"):
        updater.swap_back()
    assert cur.read_bytes() == b"MZnew"


def test_relaunch_oserror_becomes_update_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise FileNotFoundError(2, "nope")

    monkeypatch.setattr(updater.subprocess, "Popen", boom)
    with pytest.raises(UpdateError):
        updater.relaunch(tmp_path / "DeskKit.exe", [])


# ---- リダイレクトは「たどる前に」検査する
@pytest.mark.parametrize("target", ["http://github.com/x", "https://evil.example.com/DeskKit.exe"])
def test_redirect_checked_before_follow(target: str) -> None:
    import urllib.request

    h = updater._SafeRedirect()
    req = urllib.request.Request("https://api.github.com/repos/a/b/releases/latest")
    with pytest.raises(UpdateError):
        h.redirect_request(req, None, 302, "Found", {}, target)


def test_redirect_to_allowed_host_is_followed() -> None:
    import urllib.request

    h = updater._SafeRedirect()
    req = urllib.request.Request("https://github.com/x/DeskKit.exe")
    new = h.redirect_request(req, None, 302, "Found", {}, "https://objects.githubusercontent.com/y")
    assert new is not None and new.full_url == "https://objects.githubusercontent.com/y"
    assert any(isinstance(x, updater._SafeRedirect) for x in updater._OPENER.handlers)


@pytest.mark.parametrize("exc", [OSError(5, "denied"), RuntimeError("x"), UpdateError("置けません")])
def test_manager_never_sticks_in_installing(qapp: Any, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    import types

    from deskkit.update_manager import UpdateManager

    quits: list[int] = []
    m = UpdateManager(types.SimpleNamespace(quit=lambda: quits.append(1)))  # type: ignore[arg-type]

    def boom(*_a: Any) -> Path:
        raise exc

    monkeypatch.setattr(updater, "swap_in", boom)
    monkeypatch.setattr(updater, "swap_back", boom)
    m._on_downloaded(Path("x.exe"), None)
    assert m.state == "error" and m.error
    m.state = "idle"
    m.rollback()
    assert m.state == "error" and m.error
    assert not quits
