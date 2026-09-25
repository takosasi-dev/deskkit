# 行ごとの payload 暗号化(DPAPI の CryptProtectData / CryptUnprotectData、ユーザースコープ、UI 禁止)。
# payload は JSON {"text", "source_exe", "name"} を UTF-8 にしたもの。失敗時に平文へフォールバックしない(§10)。
from __future__ import annotations

import json
from dataclasses import dataclass
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


def encode_payload(cipher: Cipher, payload: Payload) -> bytes:
    raw = json.dumps({"text": payload.text, "source_exe": payload.source_exe, "name": payload.name},
                     ensure_ascii=False).encode("utf-8")
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
    return Payload(obj["text"], src if isinstance(src, str) else None, name if isinstance(name, str) else None)
