# 設定の自動世代保存(H3)。settings.json が書かれるたびに(2 秒の間の連続書き込みは1つにまとめて)中身を
# %LOCALAPPDATA%\DeskKit\settings-history\settings-<日時>.json に残し、新しいものから N 世代だけ保つ。
# 消すのはこのフォルダの、この命名の DeskKit 自身のファイルだけ。画面の表示・入れ替えだけに関わる値の違いでは世代を増やさない。
from __future__ import annotations

import copy
import datetime as _dt
import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deskkit.settings import SettingsError, SettingsStore

DIR_NAME = "settings-history"
_NAME_RE = re.compile(r"^settings-(\d{8})-(\d{6})-(\d{6})\.json$")
# 世代を分ける理由にならない値(画面の表示状態・自動で書かれる時刻など)
_VOLATILE_HOST_KEYS = ("usage_period", "usage_view", "onboarded")
_VOLATILE_UPDATE_KEYS = ("last_check",)


@dataclass(frozen=True)
class Snapshot:
    path: Path
    ts: _dt.datetime

    @property
    def name(self) -> str:
        return self.path.name


def _normalized(data: dict[str, Any]) -> dict[str, Any]:
    d = copy.deepcopy(data)
    host = d.get("host")
    if isinstance(host, dict):
        for k in _VOLATILE_HOST_KEYS:
            host.pop(k, None)
        upd = host.get("update")
        if isinstance(upd, dict):
            for k in _VOLATILE_UPDATE_KEYS:
                upd.pop(k, None)
    return d


class SnapshotStore:
    def __init__(self, directory: Path, keep: Callable[[], int]) -> None:
        self.dir = directory
        self._keep = keep

    def list(self) -> list[Snapshot]:
        """新しい順。"""
        if not self.dir.exists():
            return []
        out: list[Snapshot] = []
        for p in self.dir.iterdir():
            m = _NAME_RE.match(p.name)
            if not m or not p.is_file():
                continue
            try:
                ts = _dt.datetime.strptime("".join(m.groups()), "%Y%m%d%H%M%S%f")
            except ValueError:
                continue
            out.append(Snapshot(p, ts))
        out.sort(key=lambda s: s.ts, reverse=True)
        return out

    def read(self, snap: Snapshot) -> dict[str, Any]:
        """世代の中身(検証済み)。読めない・形が違えば SettingsError。"""
        try:
            data = json.loads(snap.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            raise SettingsError("この世代を読めません") from e
        SettingsStore._validate(data)  # noqa: SLF001 - 形の検査だけ
        if not isinstance(data, dict):
            raise SettingsError("この世代を読めません")
        return data

    def save(self, data: dict[str, Any], *, now: _dt.datetime | None = None) -> Snapshot | None:
        """直近の世代と(表示用の値を除いて)同じなら何もしない。残したら新しい世代を返す。"""
        latest = self.list()
        if latest:
            try:
                if _normalized(self.read(latest[0])) == _normalized(data):
                    return None
            except SettingsError:
                pass
        self.dir.mkdir(parents=True, exist_ok=True)
        ts = now or _dt.datetime.now()
        path = self.dir / f"settings-{ts:%Y%m%d-%H%M%S-%f}.json"
        while path.exists():  # 同じ時刻(テストなど)は 1 µs ずらす
            ts += _dt.timedelta(microseconds=1)
            path = self.dir / f"settings-{ts:%Y%m%d-%H%M%S-%f}.json"
        fd, tmp = tempfile.mkstemp(prefix=".snap-", suffix=".tmp", dir=str(self.dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self.prune()
        return Snapshot(path, ts)

    def save_file(self, settings_path: Path) -> Snapshot | None:
        """settings.json の今の中身を世代にする。構文エラーなどで読めなければ残さない。"""
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return self.save(data)

    def prune(self) -> int:
        """新しい方から keep 世代を残し、それより古い世代を消す。消した数を返す。"""
        keep = max(1, int(self._keep()))
        n = 0
        for s in self.list()[keep:]:
            try:
                s.path.unlink()  # DeskKit 自身が作った世代ファイルだけ
                n += 1
            except OSError:
                pass
        return n
