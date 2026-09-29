# 操作記録 ops.jsonl(仕様書 §9。1行1操作)。書くのは種類・結果・理由コード・codec の名前・件数・バイト数・秒数だけ。
# ファイル名・パス・zip の中の名前・テキストの中身を受け取る口が無い(INV-5)。ワーカーと GUI の両方から書くので排他する。
# 大きくなったら古い半分を捨てて、この記録のファイルだけを書き直す(置き換えの API は使わない。AC-18)。
from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

OPS = ("text", "zip", "rename", "undo")
RESULTS = ("ok", "cancelled", "error", "unchanged", "refused")
REASONS = ("empty", "binary", "unreadable", "undecodable", "unencodable", "changed", "verify_failed", "disk_full",
           "names_exhausted", "broken_zip", "bad_names", "encrypted", "unsupported_method", "zip_empty", "too_many",
           "bomb", "forbidden_folder", "too_many_items")
CODECS = ("utf-8-sig", "utf-8", "cp932", "euc_jp", "utf-16-le", "utf-16-be", "iso2022_jp_ext", "cp437")
_MAX_BYTES = 2 * 1024 * 1024


def _int(v: Any, name: str, *, nullable: bool) -> int | None:
    if v is None and nullable:
        return None
    if not isinstance(v, int) or isinstance(v, bool):
        raise TypeError(f"ops の {name} は int のみ")
    return v


class OpsLog:
    def __init__(self, path: Path, now: Callable[[], datetime] | None = None) -> None:
        self.path = path
        self._now = now or (lambda: datetime.now().astimezone())
        self._mu = threading.Lock()

    def write(self, *, op: str, result: str, reason: str | None = None, codec_in: str | None = None,
              codec_out: str | None = None, in_bytes: int | None = None, out_bytes: int | None = None,
              lines: int | None = None, replaced: int = 0, composed: int = 0, files: int = 0, skipped: int = 0,
              ms: int = 0) -> None:
        if op not in OPS or result not in RESULTS:
            raise ValueError("ops の op / result が範囲外")
        if reason is not None and reason not in REASONS:
            raise ValueError("ops の reason が範囲外")
        for c in (codec_in, codec_out):
            if c is not None and c not in CODECS:
                raise ValueError("ops の codec が範囲外")
        rec: dict[str, Any] = {
            "ts": self._now().astimezone().isoformat(timespec="seconds"),
            "op": op, "result": result, "reason": reason, "codec_in": codec_in, "codec_out": codec_out,
            "in_bytes": _int(in_bytes, "in_bytes", nullable=True), "out_bytes": _int(out_bytes, "out_bytes", nullable=True),
            "lines": _int(lines, "lines", nullable=True), "replaced": _int(replaced, "replaced", nullable=False),
            "composed": _int(composed, "composed", nullable=False), "files": _int(files, "files", nullable=False),
            "skipped": _int(skipped, "skipped", nullable=False), "ms": _int(ms, "ms", nullable=False),
        }
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with self._mu:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > _MAX_BYTES:
                    self._shrink()
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass  # 操作記録が書けなくても本処理は止めない

    def _shrink(self) -> None:
        text = self.path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        keep = text[len(text) // 2:]
        with open(self.path, "w", encoding="utf-8") as f:  # DeskKit 自身の記録だけを書き直す
            f.writelines(keep)

    def read(self) -> list[dict[str, Any]]:
        """テスト・画面用。古い順。壊れた行は飛ばす。"""
        out: list[dict[str, Any]] = []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return out
        for line in text.splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out


def is_fixed(row: dict[str, Any]) -> bool:
    """「直した回数」に数える行(FR-31): 書き出し・展開・名前の変更が result ok で終わったもの。"""
    return row.get("result") == "ok" and row.get("op") in ("text", "zip", "rename")


def is_op(op: str) -> Callable[[dict[str, Any]], bool]:
    return lambda row: row.get("result") == "ok" and row.get("op") == op
