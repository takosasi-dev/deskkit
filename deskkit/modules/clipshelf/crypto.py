# 行ごとの payload 暗号化(DPAPI の CryptProtectData / CryptUnprotectData、ユーザースコープ、UI 禁止)。
# payload は JSON {"text", "source_exe", "name"(, "expires_at")} を UTF-8 にしたもの。失敗時に平文へフォールバックしない(§10)。
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from deskkit.modules.clipshelf._win32 import CryptoError, Win32Api

__all__ = ["Cipher", "CryptoError", "DpapiCipher", "Payload", "decode_payload", "encode_payload"]


class Cipher(Protocol):
    def protect(self, data: bytes) -> bytes: ...
    def unprotect(self, data: bytes) -> bytes: ...


class DpapiCipher:
    """Win32Api の DPAPI 実装に委ねる暗号器。"""

    def __init__(self, api: Win32Api) -> None:
        self._api = api

    def protect(self, data: bytes) -> bytes:
        return self._api.protect(data)

    def unprotect(self, data: bytes) -> bytes:
        return self._api.unprotect(data)


@dataclass(frozen=True)
class Payload:
    text: str
    source_exe: str | None
    name: str | None
    expires_at: datetime | None = None  # 短命記録の期限(v0.2)。無ければキーごと書かない(v0.1 と同じ形)


def encode_payload(cipher: Cipher, payload: Payload) -> bytes:
    obj: dict[str, str | None] = {"text": payload.text, "source_exe": payload.source_exe, "name": payload.name}
    if payload.expires_at is not None:
        obj["expires_at"] = payload.expires_at.astimezone().isoformat(timespec="seconds")
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return cipher.protect(raw)


def decode_payload(cipher: Cipher, blob: bytes) -> Payload:
    """復号して Payload にする。壊れていれば CryptoError(本文はメッセージに入れない)。"""
    raw = cipher.unprotect(blob)
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CryptoError("payload decode", 0) from None
    if not isinstance(obj, dict) or not isinstance(obj.get("text"), str):
        raise CryptoError("payload shape", 0)
    src = obj.get("source_exe")
    name = obj.get("name")
    exp: datetime | None = None
    raw_exp = obj.get("expires_at")
    if raw_exp is not None:
        try:
            exp = datetime.fromisoformat(str(raw_exp))
            if exp.tzinfo is None:
                exp = exp.astimezone()
        except ValueError:
            exp = datetime(2000, 1, 1).astimezone()  # 読めない期限は期限切れ扱い(短命のはずのものを残さない側に倒す)
    return Payload(obj["text"], src if isinstance(src, str) else None, name if isinstance(name, str) else None, exp)
