# layouts/<sig>.json(+ .prev.json)・undo.json・oplog.jsonl の読み書き(§9.4)。書き込みは一時ファイル+os.replace。
# 壊れたレイアウトは読まずに .broken-<日時> へ改名して残し、削除は .deleted-<日時> への改名で行う(C-8)。
# oplog にはタイトルを書かない(D-14)。exe はファイル名だけにする(パスにユーザー名が入るため。INV-8)。
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .model import Layout

_SIG_CHARS = set("0123456789abcdef")


class BrokenLayoutError(Exception):
    """レイアウトファイルが壊れている(JSON として読めない・形が違う)。"""


class StoreWriteError(Exception):
    """ファイルを書けなかった(読み取り専用・ディスク不足など)。"""


@dataclass
class LayoutSummary:
    signature: str
    path: Path
    saved_at: str
    monitor_count: int
    window_count: int
    broken: bool = False
    has_prev: bool = False


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def is_signature(s: str) -> bool:
    return len(s) == 12 and set(s) <= _SIG_CHARS


def atomic_write_json(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="." + path.stem + "-", suffix=".tmp", dir=str(path.parent))
    except OSError as e:
        raise StoreWriteError(f"{path.name} を書けません: {e.strerror or e}") from e
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise StoreWriteError(f"{path.name} を書けません: {e.strerror or e}") from e


# oplog に書いてよいキー(タイトルやパスを誤って混ぜないための許可リスト)
_OPLOG_KEYS = {"ts", "action", "source", "sig", "moved", "planned", "skipped", "errors", "reason", "request_id",
               "result", "count", "focus_kept", "items", "name", "retry"}


def sanitize_record(rec: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in rec.items():
        if k not in _OPLOG_KEYS or "title" in k:
            continue
        out[k] = v
    return out


class Store:
    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir)
        self.layouts_dir = self.root / "layouts"
        self.undo_path = self.root / "undo.json"
        self.oplog_path = self.root / "oplog.jsonl"

    # ------------------------------------------------------------ レイアウト
    def layout_path(self, sig: str) -> Path:
        if not is_signature(sig):
            raise ValueError("シグネチャの形が違います")
        return self.layouts_dir / f"{sig}.json"

    def prev_path(self, sig: str) -> Path:
        return self.layouts_dir / f"{sig}.prev.json"

    def has_layout(self, sig: str | None) -> bool:
        return bool(sig) and is_signature(str(sig)) and self.layout_path(str(sig)).exists()

    def load_layout(self, sig: str) -> Layout | None:
        """無ければ None。壊れていれば BrokenLayoutError(ファイルには触らない)。"""
        p = self.layout_path(sig)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
            lay = Layout.from_json(data)
        except (OSError, ValueError, TypeError) as e:
            raise BrokenLayoutError(f"{p.name} を読めません: {e}") from e
        if lay.signature != sig:
            raise BrokenLayoutError(f"{p.name} の signature がファイル名と一致しません")
        return lay

    def save_layout(self, layout: Layout) -> Path | None:
        """保存する。既存が読めれば .prev.json へ1世代退避、壊れていれば .broken-<日時> に改名。
        戻り値は壊れたファイルを改名した先(無ければ None)。"""
        p = self.layout_path(layout.signature)
        broken_to: Path | None = None
        if p.exists():
            try:
                self.load_layout(layout.signature)
                ok = True
            except BrokenLayoutError:
                ok = False
            try:
                if ok:
                    os.replace(p, self.prev_path(layout.signature))
                else:
                    broken_to = p.with_name(f"{p.name}.broken-{_stamp()}")
                    os.replace(p, broken_to)
            except OSError as e:
                raise StoreWriteError(f"既存の {p.name} を退避できません: {e.strerror or e}") from e
        atomic_write_json(p, layout.to_json())
        return broken_to

    def write_layout_inplace(self, layout: Layout) -> None:
        """title_regex の手直しなど、退避を伴わない更新。"""
        atomic_write_json(self.layout_path(layout.signature), layout.to_json())

    def delete_layout(self, sig: str) -> Path:
        """恒久削除はしない。<sig>.json.deleted-<日時> に改名する。"""
        p = self.layout_path(sig)
        dst = p.with_name(f"{p.name}.deleted-{_stamp()}")
        try:
            os.replace(p, dst)
        except OSError as e:
            raise StoreWriteError(f"{p.name} を片付けられません: {e.strerror or e}") from e
        return dst

    def list_layouts(self) -> list[LayoutSummary]:
        out: list[LayoutSummary] = []
        if not self.layouts_dir.exists():
            return out
        for p in sorted(self.layouts_dir.glob("*.json")):
            sig = p.stem
            if not is_signature(sig):
                continue
            has_prev = self.prev_path(sig).exists()
            try:
                lay = self.load_layout(sig)
            except BrokenLayoutError:
                out.append(LayoutSummary(sig, p, "", 0, 0, broken=True, has_prev=has_prev))
                continue
            if lay is not None:
                out.append(LayoutSummary(sig, p, lay.saved_at, len(lay.monitors), len(lay.windows), has_prev=has_prev))
        return out

    # ------------------------------------------------------------ undo
    def write_undo(self, data: dict[str, Any]) -> None:
        atomic_write_json(self.undo_path, data)

    def read_undo(self) -> dict[str, Any] | None:
        if not self.undo_path.exists():
            return None
        try:
            d = json.loads(self.undo_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None
        if not isinstance(d, dict) or not d.get("windows"):
            return None
        return d

    def clear_undo(self) -> None:
        atomic_write_json(self.undo_path, {})

    # ------------------------------------------------------------ oplog
    def append_oplog(self, rec: dict[str, Any]) -> dict[str, Any]:
        r = {"ts": now_iso(), **rec}
        clean = sanitize_record(r)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.oplog_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(clean, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError:
            pass  # 操作ログが書けなくても本体の動作は止めない(呼び出し側がロガーに出す)
        return clean

    def read_oplog(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self.oplog_path.exists():
            return []
        try:
            lines = self.oplog_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for ln in lines[-limit:]:
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            if isinstance(d, dict):
                out.append(d)
        out.reverse()
        return out
