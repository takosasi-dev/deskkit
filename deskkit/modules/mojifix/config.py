# MojiFix の設定(仕様書 §9 の `modules.mojifix`)。足りないキー・型や範囲の合わない値は既定値で補う。
# 画面の選択(書き出しの形・改行・くっつける・zip の設定・サブフォルダ)を変えたら保存する。
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TEXT_OUTPUTS = ("excel", "sjis", "utf8")
NEWLINES = ("keep", "crlf", "lf")
ZIP_GB_MIN, ZIP_GB_MAX = 1, 200

DEFAULTS: dict[str, Any] = {
    "text_output": "excel",
    "newline": "keep",
    "text_compose": False,
    "zip_compose": True,
    "zip_skip_mac_files": True,
    "zip_max_total_gb": 20,
    "rename_recursive": True,
}


@dataclass(frozen=True)
class Config:
    text_output: str
    newline: str
    text_compose: bool
    zip_compose: bool
    zip_skip_mac_files: bool
    zip_max_total_gb: int
    rename_recursive: bool


def _valid(key: str, v: Any) -> bool:
    if key == "text_output":
        return v in TEXT_OUTPUTS
    if key == "newline":
        return v in NEWLINES
    if key == "zip_max_total_gb":
        return isinstance(v, int) and not isinstance(v, bool) and ZIP_GB_MIN <= v <= ZIP_GB_MAX
    return isinstance(v, bool)


def normalize(section: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """(補った設定, 書き戻しが要るか)。知らないキーはそのまま残す。"""
    out = dict(section)
    changed = False
    for k, d in DEFAULTS.items():
        if k not in out or not _valid(k, out[k]):
            out[k] = d
            changed = True
    return out, changed


def parse(section: dict[str, Any]) -> Config:
    s, _ = normalize(section)
    return Config(str(s["text_output"]), str(s["newline"]), bool(s["text_compose"]), bool(s["zip_compose"]),
                  bool(s["zip_skip_mac_files"]), int(s["zip_max_total_gb"]), bool(s["rename_recursive"]))
