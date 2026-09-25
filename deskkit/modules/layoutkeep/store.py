# layouts/<sig>.json(プリセット集。+ .prev.json)・<sig>.auto.json(自動スナップショット)・undo.json・oplog.jsonl の読み書き。
# 書き込みは一時ファイル+os.replace。v0.1 形式(schema 1)は読めるまま、初めて書くときに .v1-backup-<日時> を残して移行する。
# 壊れたファイルは .broken-<日時>、削除は .deleted-<日時> への改名で残す(C-8)。oplog にタイトル・パスを書かない(INV-8)。
from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .model import AUTO_SLOT_KEYS, MIGRATED_PRESET_NAME, AutoSlots, Layout, Preset, PresetSet

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
    window_count: int  # 既定のプリセットの枚数(プリセットが無ければ自動スナップショット「最新」の枚数)
    broken: bool = False
    has_prev: bool = False
    preset_count: int = 0
    default_name: str = ""
    auto_slots: tuple[str, ...] = ()  # 自動スナップショットの有無("latest" / "before_change")
    v1: bool = False  # まだ v0.1 形式(次に書くとき移行する)


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
               "result", "count", "focus_kept", "items", "name", "retry", "preset", "slot"}


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

    # ------------------------------------------------------------ レイアウト(プリセット集)
    def layout_path(self, sig: str) -> Path:
        if not is_signature(sig):
            raise ValueError("シグネチャの形が違います")
        return self.layouts_dir / f"{sig}.json"

    def prev_path(self, sig: str) -> Path:
        return self.layouts_dir / f"{sig}.prev.json"

    def auto_path(self, sig: str) -> Path:
        if not is_signature(sig):
            raise ValueError("シグネチャの形が違います")
        return self.layouts_dir / f"{sig}.auto.json"

    def has_layout(self, sig: str | None) -> bool:
        """その構成に利用者のプリセットがあるか(自動スナップショットは含めない)。"""
        return bool(sig) and is_signature(str(sig)) and self.layout_path(str(sig)).exists()

    def load_set(self, sig: str) -> PresetSet | None:
        """無ければ None。壊れていれば BrokenLayoutError(ファイルには触らない)。v0.1 形式も読む。"""
        p = self.layout_path(sig)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
            ps = PresetSet.from_json(data)
        except (OSError, ValueError, TypeError) as e:
            raise BrokenLayoutError(f"{p.name} を読めません: {e}") from e
        if ps.signature != sig:
            raise BrokenLayoutError(f"{p.name} の signature がファイル名と一致しません")
        return ps

    def load_layout(self, sig: str, preset: str | None = None) -> Layout | None:
        """preset(名前)の配置。None なら既定のプリセット。「自動」「抜く前」は自動スナップショット。無ければ None。"""
        if preset is not None and preset.strip() in AUTO_SLOT_KEYS:
            return self.load_auto_slot(sig, AUTO_SLOT_KEYS[preset.strip()])
        ps = self.load_set(sig)
        if ps is None:
            return None
        p = ps.default() if preset is None else ps.by_name(preset)
        return ps.layout_of(p) if p is not None else None

    def _move_aside_broken(self, p: Path) -> Path:
        dst = p.with_name(f"{p.name}.broken-{_stamp()}")
        try:
            os.replace(p, dst)
        except OSError as e:
            raise StoreWriteError(f"既存の {p.name} を退避できません: {e.strerror or e}") from e
        return dst

    def write_set(self, ps: PresetSet, *, keep_prev: bool) -> None:
        """プリセット集を書く。v0.1 形式のファイルが残っていれば、先に .v1-backup-<日時> を複製して残す(移行)。
        keep_prev なら書く前の中身を .prev.json に1世代残す(利用者のプリセットを上書き・削除するとき)。"""
        p = self.layout_path(ps.signature)
        if p.exists():
            try:
                on_disk = self.load_set(ps.signature)
            except BrokenLayoutError:
                on_disk = None
            try:
                if on_disk is not None and on_disk.from_v1:
                    shutil.copy2(p, p.with_name(f"{p.name}.v1-backup-{_stamp()}"))
                if keep_prev and on_disk is not None:
                    shutil.copy2(p, self.prev_path(ps.signature))
            except OSError as e:
                raise StoreWriteError(f"既存の {p.name} の控えを残せません: {e.strerror or e}") from e
        atomic_write_json(p, ps.to_json())
        ps.from_v1 = False

    def save_layout(self, layout: Layout, preset: str | None = None) -> Path | None:
        """配置をプリセットとして保存する(preset=None なら既定のプリセット。無ければ「基本」を作って既定にする)。
        既存が読めれば .prev.json へ1世代残し、壊れていれば .broken-<日時> に改名する。
        戻り値は壊れたファイルを改名した先(無ければ None)。"""
        sig = layout.signature
        p = self.layout_path(sig)
        broken_to: Path | None = None
        ps: PresetSet | None = None
        if p.exists():
            try:
                ps = self.load_set(sig)
            except BrokenLayoutError:
                broken_to = self._move_aside_broken(p)
        if ps is None:
            ps = PresetSet(signature=sig, monitors=layout.monitors, presets=[], default_id=None)
        ps.monitors = layout.monitors or ps.monitors
        target = ps.default() if preset is None else ps.by_name(preset)
        if target is None:
            target = Preset(id=ps.new_id(), name=(preset or MIGRATED_PRESET_NAME).strip(), saved_at=layout.saved_at,
                            windows=layout.windows)
            ps.presets.append(target)
            if ps.default_id is None or ps.by_id(ps.default_id) is None:
                ps.default_id = target.id
        else:
            target.saved_at, target.windows = layout.saved_at, layout.windows
        self.write_set(ps, keep_prev=True)
        return broken_to

    def write_layout_inplace(self, layout: Layout, preset: str | None = None) -> None:
        """title_regex の手直しなど、退避を伴わない更新(preset=None なら既定、「自動」「抜く前」は自動スナップショット)。"""
        if preset is not None and preset.strip() in AUTO_SLOT_KEYS:
            auto = self.load_auto(layout.signature) or AutoSlots(layout.signature, layout.monitors)
            auto.slots[AUTO_SLOT_KEYS[preset.strip()]] = layout
            atomic_write_json(self.auto_path(layout.signature), auto.to_json())
            return
        ps = self.load_set(layout.signature)
        if ps is None:
            raise StoreWriteError("レイアウトが見つかりません")
        target = ps.default() if preset is None else ps.by_name(preset)
        if target is None:
            raise StoreWriteError("プリセットが見つかりません")
        target.windows = layout.windows
        self.write_set(ps, keep_prev=False)

    def delete_layout(self, sig: str) -> Path:
        """構成ごと片付ける。恒久削除はしない。<sig>.json(と自動スナップショット)を .deleted-<日時> に改名する。"""
        p = self.layout_path(sig)
        stamp = _stamp()
        dst = p.with_name(f"{p.name}.deleted-{stamp}")
        try:
            if p.exists():
                os.replace(p, dst)
            a = self.auto_path(sig)
            if a.exists():
                os.replace(a, a.with_name(f"{a.name}.deleted-{stamp}"))
        except OSError as e:
            raise StoreWriteError(f"{p.name} を片付けられません: {e.strerror or e}") from e
        return dst

    def delete_preset(self, sig: str, preset_id: str) -> Path:
        """プリセットを1つ外す。外したプリセットは <sig>.<id>.json.deleted-<日時> に単体のレイアウトとして残す。
        既定を外したら残りの先頭を既定にする。最後の1つなら構成ごと改名する(delete_layout。自動スナップショットは残す)。"""
        ps = self.load_set(sig)
        if ps is None:
            raise StoreWriteError("レイアウトが見つかりません")
        target = ps.by_id(preset_id)
        if target is None:
            raise StoreWriteError("プリセットが見つかりません")
        if len(ps.presets) == 1:
            p = self.layout_path(sig)
            dst = p.with_name(f"{p.name}.deleted-{_stamp()}")
            try:
                os.replace(p, dst)
            except OSError as e:
                raise StoreWriteError(f"{p.name} を片付けられません: {e.strerror or e}") from e
            return dst
        keep = self.layouts_dir / f"{sig}.{preset_id}.json.deleted-{_stamp()}"
        atomic_write_json(keep, ps.layout_of(target).to_json())
        ps.presets = [x for x in ps.presets if x.id != preset_id]
        if ps.default_id == preset_id:
            ps.default_id = ps.presets[0].id
        self.write_set(ps, keep_prev=True)
        return keep

    def list_layouts(self) -> list[LayoutSummary]:
        out: list[LayoutSummary] = []
        if not self.layouts_dir.exists():
            return out
        sigs = {p.stem for p in self.layouts_dir.glob("*.json") if is_signature(p.stem)}
        sigs |= {p.name[:12] for p in self.layouts_dir.glob("*.auto.json") if is_signature(p.name[:12])}
        for sig in sorted(sigs):
            p = self.layout_path(sig)
            has_prev = self.prev_path(sig).exists()
            auto = self.load_auto_quiet(sig)
            keys = tuple(k for k in ("latest", "before_change") if auto is not None and k in auto.slots)
            if not p.exists():
                latest = auto.slots.get("latest") if auto is not None else None
                out.append(LayoutSummary(sig, p, latest.saved_at if latest else "", len(auto.monitors) if auto else 0,
                                         len(latest.windows) if latest else 0, auto_slots=keys))
                continue
            try:
                ps = self.load_set(sig)
            except BrokenLayoutError:
                out.append(LayoutSummary(sig, p, "", 0, 0, broken=True, has_prev=has_prev, auto_slots=keys))
                continue
            if ps is None:
                continue
            d = ps.default()
            out.append(LayoutSummary(sig, p, d.saved_at if d else "", len(ps.monitors), len(d.windows) if d else 0,
                                     has_prev=has_prev, preset_count=len(ps.presets), default_name=d.name if d else "",
                                     auto_slots=keys, v1=ps.from_v1))
        return out

    # ------------------------------------------------------------ 自動スナップショット(利用者のプリセットとは別ファイル)
    def load_auto(self, sig: str) -> AutoSlots | None:
        """無ければ None。壊れていれば BrokenLayoutError。"""
        p = self.auto_path(sig)
        if not p.exists():
            return None
        try:
            a = AutoSlots.from_json(json.loads(p.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError, TypeError) as e:
            raise BrokenLayoutError(f"{p.name} を読めません: {e}") from e
        if a.signature != sig:
            raise BrokenLayoutError(f"{p.name} の signature がファイル名と一致しません")
        return a

    def load_auto_quiet(self, sig: str) -> AutoSlots | None:
        try:
            return self.load_auto(sig)
        except BrokenLayoutError:
            return None

    def load_auto_slot(self, sig: str, slot: str) -> Layout | None:
        a = self.load_auto_quiet(sig)
        return a.slots.get(slot) if a is not None else None

    def write_auto_latest(self, layout: Layout) -> None:
        """自動スナップショットの「最新」を書く(壊れた自動ファイルは .broken-<日時> に改名して作り直す)。"""
        p = self.auto_path(layout.signature)
        try:
            a = self.load_auto(layout.signature)
        except BrokenLayoutError:
            self._move_aside_broken(p)
            a = None
        if a is None:
            a = AutoSlots(layout.signature, layout.monitors)
        a.monitors = layout.monitors or a.monitors
        a.slots["latest"] = layout
        atomic_write_json(p, a.to_json())

    def promote_auto(self, sig: str) -> bool:
        """構成が変わり始めたとき、「最新」を「抜く前」に写す(同じなら書かない)。写したら True。"""
        a = self.load_auto_quiet(sig)
        if a is None or "latest" not in a.slots:
            return False
        cur = a.slots["latest"]
        prev = a.slots.get("before_change")
        if prev is not None and prev.saved_at == cur.saved_at:
            return False
        a.slots["before_change"] = Layout(sig, a.monitors, cur.saved_at, list(cur.windows))
        atomic_write_json(self.auto_path(sig), a.to_json())
        return True

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
