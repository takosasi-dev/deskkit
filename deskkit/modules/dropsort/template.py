# 移動先テンプレート(D2): ルールの移動先に {yyyy} {mm} {dd} {ext} {domain} を書けるようにする。
# 最初のプレースホルダより前のフォルダ(基準フォルダ)は利用者が決めたもので、自動では作らない(作るのはその下だけ)。
# 展開した値は1階層ずつ無害化する(Windows で使えない文字・予約名・末尾の点と空白・".."。URL は入らない)。
from __future__ import annotations

import ntpath
import re
from dataclasses import dataclass
from datetime import datetime

PLACEHOLDERS = ("yyyy", "mm", "dd", "ext", "domain")
UNKNOWN_DOMAIN = "unknown"   # 入手元のドメインが分からないとき
NO_EXT = "noext"             # 拡張子が無いとき
MAX_SEGMENT = 120            # 展開後の1階層の長さの上限(長いパスで失敗しにくくする)

_TOKEN = re.compile(r"\{([^{}\\/]*)\}")
_SEP = re.compile(r"[\\/]")
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                       *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))})


@dataclass(frozen=True)
class TemplateVars:
    when: float            # 日付の元(ファイルがダウンロードフォルダに来た時刻)
    name: str              # ファイル名({ext} の元)
    domain: str | None     # 入手元のドメイン名(Zone.Identifier の HostUrl から。URL そのものは持たない)


def has_placeholder(dest: str) -> bool:
    return "{" in dest or "}" in dest


def split(dest: str) -> tuple[str, str]:
    """'D:\\整理\\{yyyy}\\{mm}' → ('D:\\整理', '{yyyy}\\{mm}')。基準フォルダはプレースホルダを含む階層の親。"""
    d = dest.strip()
    i = d.find("{")
    if i < 0:
        return ntpath.normpath(d) if d else d, ""
    head = d[:i]
    j = max(head.rfind("\\"), head.rfind("/"))
    if j < 0:
        return "", d
    return ntpath.normpath(d[: j + 1]), d[j + 1:]


def base(dest: str) -> str:
    return split(dest)[0]


def validate(dest: str) -> str | None:
    """テンプレートの形の検査。問題があれば理由文、無ければ None(基準フォルダの存在などは check_dest が見る)。"""
    d = dest.strip()
    for m in _TOKEN.finditer(d):
        if m.group(1).lower() not in PLACEHOLDERS:
            return (f"移動先の {{{m.group(1)}}} は使えません(使えるのは "
                    + " ".join("{" + p + "}" for p in PLACEHOLDERS) + ")")
    rest_all = _TOKEN.sub("", d)
    if "{" in rest_all or "}" in rest_all:
        return "移動先の { } の対応が取れていません"
    b, rest = split(d)
    if not b or b.startswith(("\\\\", "//")) or len(b) < 3 or b[1] != ":" or b[2] not in "\\/":
        return "テンプレートの前に、ドライブ文字から始まるフォルダを書いてください(例: D:\\整理\\{yyyy}\\{mm})"
    for seg in _SEP.split(rest):
        literal = _TOKEN.sub("", seg)
        if not seg.strip() or seg.strip() in (".", ".."):
            return "移動先に空の階層や「.」「..」は使えません"
        if _INVALID.search(literal):
            return "移動先に Windows で使えない文字(< > : \" | ? *)があります"
        if not _TOKEN.search(seg) and seg.split(".")[0].strip().upper() in _RESERVED:
            return f"移動先の「{seg}」は Windows の予約名なので使えません"
    return None


def _value(key: str, v: TemplateVars) -> str:
    if key in ("yyyy", "mm", "dd"):
        try:
            t = datetime.fromtimestamp(v.when)
        except (OverflowError, OSError, ValueError):
            t = datetime.fromtimestamp(0)
        return {"yyyy": f"{t.year:04d}", "mm": f"{t.month:02d}", "dd": f"{t.day:02d}"}[key]
    if key == "ext":
        ext = ntpath.splitext(v.name)[1].lstrip(".").lower()
        return ext or NO_EXT
    return (v.domain or "").strip().lower() or UNKNOWN_DOMAIN


def sanitize_segment(seg: str) -> str:
    """1階層ぶんのフォルダ名を Windows で安全な形にする(区切り文字・使えない文字・予約名・末尾の点と空白・'..')。"""
    s = _INVALID.sub("_", seg).strip()
    s = s[:MAX_SEGMENT].rstrip(" .")
    if not s or s in (".", ".."):
        return "_"
    if s.split(".")[0].rstrip(" ").upper() in _RESERVED:
        s = "_" + s
    return s


def expand(dest: str, v: TemplateVars, base_override: str | None = None) -> str:
    """テンプレートを展開した移動先フォルダ。base_override を渡すと基準フォルダをその実パスに置き換える。"""
    b, rest = split(dest)
    root = base_override or b
    if not rest:
        return root
    segs = [sanitize_segment(_TOKEN.sub(lambda m: _INVALID.sub("_", _value(m.group(1).lower(), v))[:MAX_SEGMENT], seg))
            for seg in _SEP.split(rest)]
    return ntpath.join(root, *segs)


def example(dest: str, *, when: float, ext: str = ".pdf") -> str:
    """編集画面のプレビュー用: 今日の日付・例のファイル名・例のドメインで展開する。"""
    return expand(dest, TemplateVars(when, "example" + (ext if ext.startswith(".") else "." + ext), "example.com"))


__all__ = ["PLACEHOLDERS", "UNKNOWN_DOMAIN", "NO_EXT", "TemplateVars", "has_placeholder", "split", "base", "validate",
           "expand", "sanitize_segment", "example"]
