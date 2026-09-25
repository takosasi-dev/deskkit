# Mark of the Web(Zone.Identifier ストリーム)の有無・ZoneId・HostUrl のドメイン名だけを取り出す。
# URL そのものは保持も記録もしない(INV-7)。生のバイト列は移動後の一致検証にだけ使い、表示・ログに出さない。
# 読めない・形式不明のときは「MOTW あり、ZoneId は null」として扱う(安全側)。
from __future__ import annotations

import re
from dataclasses import dataclass, field

from deskkit.modules.dropsort._win32 import StreamRead, Win32Api

_DOMAIN_OK = re.compile(r"^[a-z0-9.-]+$")


@dataclass(frozen=True)
class Motw:
    present: bool
    zone_id: int | None = None
    domain: str | None = None
    raw: bytes | None = field(default=None, repr=False)


NONE = Motw(False)


def _decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


def domain_of(url: str) -> str | None:
    """'scheme://user@host:port/path' からホスト名だけを返す。取れなければ None。"""
    i = url.find("://")
    if i < 0:
        return None
    rest = url[i + 3:]
    for sep in "/?#\\":
        j = rest.find(sep)
        if j >= 0:
            rest = rest[:j]
    rest = rest.rsplit("@", 1)[-1]
    if rest.startswith("["):
        return None  # IPv6 リテラルは扱わない
    rest = rest.split(":", 1)[0].strip().lower().rstrip(".")
    if not rest or not _DOMAIN_OK.match(rest):
        return None
    return rest


def parse(data: bytes | None) -> Motw:
    if data is None:
        return Motw(True, None, None, None)
    text = _decode(data)
    zone: int | None = None
    domain: str | None = None
    in_section = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            in_section = s.lower() == "[zonetransfer]"
            continue
        if not in_section or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip().lower()
        v = v.strip()
        if k == "zoneid":
            try:
                zone = int(v)
            except ValueError:
                zone = None
        elif k == "hosturl":
            domain = domain_of(v)
    return Motw(True, zone, domain, data)


def read(api: Win32Api, path: str) -> Motw:
    r: StreamRead = api.read_zone_identifier(path)
    if not r.present:
        return NONE
    return parse(r.data)
