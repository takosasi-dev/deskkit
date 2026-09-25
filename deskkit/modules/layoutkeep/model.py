# LayoutKeep が扱うデータ型(モニタ・ウィンドウ・配置・保存エントリ・レイアウト・プリセット・自動スナップショット)と定数。
# ウィンドウタイトルは repr に出さない(ログへの混入を防ぐ。D-14 / INV-8)。
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PureWindowsPath
from typing import Any

Rect = tuple[int, int, int, int]  # left, top, right, bottom

# winuser.h の showCmd
SW_SHOWNORMAL = 1
SW_SHOWMINIMIZED = 2
SW_SHOWMAXIMIZED = 3
SW_SHOWNOACTIVATE = 4
SW_MINIMIZE = 6
SW_SHOWMINNOACTIVE = 7

ID_SOURCES: tuple[str, ...] = ("device_interface", "displayconfig_path", "edid")
DEFAULT_FIELDS: tuple[str, ...] = ("id", "w", "h", "x", "y", "dpi", "primary")
LAYOUT_SCHEMA = 1
PRESET_SCHEMA = 2

# プリセット(v0.2): 1つの構成シグネチャに名前付きの配置を複数持つ。既定の1つを自動復元に使う。
MIGRATED_PRESET_NAME = "基本"  # v0.1 の1構成1レイアウトを読み込んだときのプリセット名
AUTO_LATEST = "自動"  # 自動スナップショット(最新)。利用者のプリセット名には使えない
AUTO_BEFORE = "抜く前"  # 構成が変わる直前(抜き差し・スリープ前)の自動スナップショット
RESERVED_PRESET_NAMES = frozenset({AUTO_LATEST, AUTO_BEFORE})
AUTO_SLOT_KEYS = {AUTO_LATEST: "latest", AUTO_BEFORE: "before_change"}
MAX_PRESET_NAME = 40


def rect_of(v: Any) -> Rect | None:
    """JSON の配列などから Rect を作る。形が違えば None。"""
    if isinstance(v, (list, tuple)) and len(v) == 4 and all(isinstance(x, int) and not isinstance(x, bool) for x in v):
        return (int(v[0]), int(v[1]), int(v[2]), int(v[3]))
    return None


def rect_text(r: Rect | None) -> str:
    if r is None:
        return "—"
    return f"({r[0]}, {r[1]}) {r[2] - r[0]}×{r[3] - r[1]}"


def rects_intersect(a: Rect, b: Rect) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def as_list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else []


def exe_basename(path: str | None) -> str:
    if not path:
        return ""
    return PureWindowsPath(path).name


@dataclass(frozen=True)
class RawMonitor:
    """Win32 から取ったモニタ1台分の原値。"""

    device: str  # \\.\DISPLAY1(表示用。ID には使わない)
    rect: Rect
    work: Rect
    primary: bool
    dpi: int | None
    ids: dict[str, str | None] = field(default_factory=dict)

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]


@dataclass(frozen=True)
class Placement:
    """WINDOWPLACEMENT の必要部分。normal_rect はワークスペース座標の原値(§4)。"""

    show_cmd: int
    normal_rect: Rect
    min_pos: tuple[int, int] = (-1, -1)
    max_pos: tuple[int, int] = (-1, -1)
    flags: int = 0

    @property
    def show(self) -> str:
        if self.show_cmd == SW_SHOWMAXIMIZED:
            return "maximized"
        if self.show_cmd in (SW_SHOWMINIMIZED, SW_MINIMIZE, SW_SHOWMINNOACTIVE):
            return "minimized"
        return "normal"


@dataclass
class RawWindow:
    """EnumWindows で見えたトップレベルウィンドウ1枚の原値。title はメモリ上だけで使う。"""

    hwnd: int
    pid: int
    cls: str
    title: str = field(repr=False)
    visible: bool
    iconic: bool
    toolwindow: bool
    owned: bool
    cloaked: bool
    exe: str | None
    exe_error: int
    proc_start: int | None
    placement: Placement | None
    screen_rect: Rect | None


@dataclass
class WindowInfo:
    """除外判定と対象判定を済ませたウィンドウ。excluded は除外の種類(None なら候補)。"""

    raw: RawWindow
    excluded: str | None = None
    target_index: int | None = None  # 当たった targets の添字(None なら対象外)

    @property
    def hwnd(self) -> int:
        return self.raw.hwnd

    @property
    def pid(self) -> int:
        return self.raw.pid

    @property
    def exe(self) -> str | None:
        return self.raw.exe

    @property
    def exe_name(self) -> str:
        return exe_basename(self.raw.exe)

    @property
    def cls(self) -> str:
        return self.raw.cls

    @property
    def title(self) -> str:
        return self.raw.title

    @property
    def group(self) -> tuple[str, str]:
        return ((self.raw.exe or "").lower(), self.raw.cls)


@dataclass
class SavedEntry:
    """レイアウトファイルの windows[] の1件(§9.2)。"""

    exe: str
    cls: str
    title_regex: str | None
    title_at_save: str = field(repr=False)
    hwnd: int
    pid: int
    proc_start: int | None
    show: str
    normal_rect: Rect
    screen_rect: Rect | None
    normal_rect_raw: Rect | None = None  # スナップ時の rcNormalPosition の原値(参考用)
    snapped: bool = False

    @property
    def exe_name(self) -> str:
        return exe_basename(self.exe)

    @property
    def group(self) -> tuple[str, str]:
        return (self.exe.lower(), self.cls)

    def to_json(self) -> dict[str, Any]:
        return {
            "exe": self.exe, "class": self.cls, "title_regex": self.title_regex, "title_at_save": self.title_at_save,
            "hwnd": self.hwnd, "pid": self.pid, "proc_start": self.proc_start, "show": self.show,
            "normal_rect": list(self.normal_rect), "screen_rect": list(self.screen_rect) if self.screen_rect else None,
            "snapped": self.snapped, "normal_rect_raw": list(self.normal_rect_raw) if self.normal_rect_raw else None,
        }

    @classmethod
    def from_json(cls, d: Any) -> SavedEntry:
        if not isinstance(d, dict):
            raise ValueError("windows の要素がオブジェクトではありません")
        nr = rect_of(d.get("normal_rect"))
        if nr is None or not isinstance(d.get("exe"), str) or not isinstance(d.get("class"), str):
            raise ValueError("windows の要素に exe / class / normal_rect がありません")
        tr = d.get("title_regex")
        ps = d.get("proc_start")
        show = d.get("show")
        return cls(
            exe=d["exe"], cls=d["class"], title_regex=tr if isinstance(tr, str) and tr else None,
            title_at_save=str(d.get("title_at_save") or ""), hwnd=int(d.get("hwnd") or 0), pid=int(d.get("pid") or 0),
            proc_start=int(ps) if isinstance(ps, int) and not isinstance(ps, bool) else None,
            show="maximized" if show == "maximized" else "normal", normal_rect=nr, screen_rect=rect_of(d.get("screen_rect")),
            normal_rect_raw=rect_of(d.get("normal_rect_raw")), snapped=d.get("snapped") is True,
        )


@dataclass
class Layout:
    signature: str
    monitors: list[dict[str, Any]]
    saved_at: str
    windows: list[SavedEntry]
    schema: int = LAYOUT_SCHEMA

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "signature": self.signature, "monitors": self.monitors,
            "saved_at": self.saved_at, "windows": [w.to_json() for w in self.windows],
        }

    @classmethod
    def from_json(cls, d: Any) -> Layout:
        if not isinstance(d, dict):
            raise ValueError("最上位がオブジェクトではありません")
        sig = d.get("signature")
        wins = d.get("windows")
        if not isinstance(sig, str) or not isinstance(wins, list):
            raise ValueError("signature / windows がありません")
        mons = as_list(d.get("monitors"))
        return cls(signature=sig, monitors=[m for m in mons if isinstance(m, dict)], saved_at=str(d.get("saved_at") or ""),
                   windows=[SavedEntry.from_json(x) for x in wins], schema=int(d.get("schema") or LAYOUT_SCHEMA))


def validate_preset_name(name: str) -> str | None:
    """プリセット名の問題(無ければ None)。"""
    n = name.strip()
    if not n:
        return "名前が空です"
    if len(n) > MAX_PRESET_NAME:
        return f"名前は {MAX_PRESET_NAME} 文字以内にしてください"
    if n in RESERVED_PRESET_NAMES:
        return f"「{n}」は自動スナップショット用の名前なので使えません"
    return None


@dataclass
class Preset:
    """構成シグネチャの中の名前付き配置1つ。id はファイル内で一意(名前を変えても変わらない)。"""

    id: str
    name: str
    saved_at: str
    windows: list[SavedEntry]

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "saved_at": self.saved_at, "windows": [w.to_json() for w in self.windows]}

    @classmethod
    def from_json(cls, d: Any) -> Preset:
        if not isinstance(d, dict):
            raise ValueError("presets の要素がオブジェクトではありません")
        pid, name, wins = d.get("id"), d.get("name"), d.get("windows")
        if not isinstance(pid, str) or not pid or not isinstance(name, str) or not name or not isinstance(wins, list):
            raise ValueError("presets の要素に id / name / windows がありません")
        return cls(id=pid, name=name, saved_at=str(d.get("saved_at") or ""), windows=[SavedEntry.from_json(x) for x in wins])


@dataclass
class PresetSet:
    """layouts/<sig>.json(schema 2)。v0.1 の schema 1(1構成1レイアウト)も読み、1つのプリセットとして扱う。
    書くときは v0.1 が読めるよう、最上位の windows / saved_at に既定プリセットの写しを置く(読むときは presets が正)。"""

    signature: str
    monitors: list[dict[str, Any]]
    presets: list[Preset]
    default_id: str | None
    from_v1: bool = False  # schema 1 から読んだ(次に書くとき移行する)

    def default(self) -> Preset | None:
        return next((p for p in self.presets if p.id == self.default_id), self.presets[0] if self.presets else None)

    def by_id(self, pid: str) -> Preset | None:
        return next((p for p in self.presets if p.id == pid), None)

    def by_name(self, name: str) -> Preset | None:
        n = name.strip()
        return next((p for p in self.presets if p.name == n), None)

    def layout_of(self, p: Preset) -> Layout:
        return Layout(signature=self.signature, monitors=self.monitors, saved_at=p.saved_at, windows=p.windows)

    def new_id(self) -> str:
        used = {p.id for p in self.presets}
        n = len(self.presets) + 1
        while f"p{n}" in used:
            n += 1
        return f"p{n}"

    def to_json(self) -> dict[str, Any]:
        d = self.default()
        return {
            "schema": PRESET_SCHEMA, "signature": self.signature, "monitors": self.monitors,
            "default": d.id if d else None, "presets": [p.to_json() for p in self.presets],
            # v0.1 互換の写し(既定プリセット)
            "saved_at": d.saved_at if d else "", "windows": [w.to_json() for w in d.windows] if d else [],
        }

    @classmethod
    def from_json(cls, d: Any) -> PresetSet:
        if not isinstance(d, dict):
            raise ValueError("最上位がオブジェクトではありません")
        if not isinstance(d.get("presets"), list):  # schema 1(v0.1)
            lay = Layout.from_json(d)
            p = Preset(id="p1", name=MIGRATED_PRESET_NAME, saved_at=lay.saved_at, windows=lay.windows)
            return cls(signature=lay.signature, monitors=lay.monitors, presets=[p], default_id="p1", from_v1=True)
        sig = d.get("signature")
        if not isinstance(sig, str):
            raise ValueError("signature がありません")
        presets = [Preset.from_json(x) for x in d["presets"]]
        if len({p.id for p in presets}) != len(presets):
            raise ValueError("presets の id が重複しています")
        default = d.get("default")
        default_id = default if isinstance(default, str) and any(p.id == default for p in presets) else (
            presets[0].id if presets else None)
        mons = as_list(d.get("monitors"))
        return cls(signature=sig, monitors=[m for m in mons if isinstance(m, dict)], presets=presets, default_id=default_id)


@dataclass
class AutoSlots:
    """layouts/<sig>.auto.json。自動スナップショットの「最新」と「構成が変わる前」。利用者のプリセットとは別ファイル。"""

    signature: str
    monitors: list[dict[str, Any]]
    slots: dict[str, Layout] = field(default_factory=dict)  # "latest" / "before_change"

    def to_json(self) -> dict[str, Any]:
        return {"schema": PRESET_SCHEMA, "signature": self.signature, "monitors": self.monitors,
                "slots": {k: {"saved_at": v.saved_at, "windows": [w.to_json() for w in v.windows]}
                          for k, v in self.slots.items()}}

    @classmethod
    def from_json(cls, d: Any) -> AutoSlots:
        if not isinstance(d, dict) or not isinstance(d.get("signature"), str) or not isinstance(d.get("slots"), dict):
            raise ValueError("自動スナップショットの形が違います")
        sig = str(d["signature"])
        mons = [m for m in as_list(d.get("monitors")) if isinstance(m, dict)]
        slots: dict[str, Layout] = {}
        for k, v in d["slots"].items():
            if k not in AUTO_SLOT_KEYS.values() or not isinstance(v, dict) or not isinstance(v.get("windows"), list):
                continue
            slots[k] = Layout(signature=sig, monitors=mons, saved_at=str(v.get("saved_at") or ""),
                              windows=[SavedEntry.from_json(x) for x in v["windows"]])
        return cls(signature=sig, monitors=mons, slots=slots)


def same_windows(a: list[SavedEntry], b: list[SavedEntry]) -> bool:
    """自動スナップショットの書き込みを減らすための比較(同じなら書かない)。タイトルは比べない。"""
    def key(e: SavedEntry) -> tuple[Any, ...]:
        return (e.exe.lower(), e.cls, e.hwnd, e.pid, -1 if e.proc_start is None else e.proc_start, e.show, e.normal_rect)

    return sorted(map(key, a)) == sorted(map(key, b))


@dataclass
class SigResult:
    """構成シグネチャの計算結果。signature が None なら reason に理由。"""

    signature: str | None
    reason: str | None
    monitors: list[RawMonitor]
    normalized: list[dict[str, Any]]
