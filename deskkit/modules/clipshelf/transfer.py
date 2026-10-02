# 定型文の書き出し・読み込み(v0.4.1。仕様書 §13 Q-9 への答え)。
# 書き出すのは定型文の名前と本文だけ(履歴・ピン・日時・コピー元は書かない)。形式は UTF-8 の JSON で版番号付き。
# DB の中は DPAPI で暗号化しているが、書き出したファイルは平文になる(画面の確認の窓で必ず知らせる)。
# ここは純粋な処理だけで、画面・DB・ログには触れない。例外のメッセージにもパス・本文を入れない(INV-1)。
from __future__ import annotations

import json
import os
import secrets
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

FORMAT_ID = "deskkit.clipshelf.snippets"
FORMAT_VERSION = 1
MAX_FILE_BYTES = 8 * 1024 * 1024   # 読み込むファイルの大きさの上限(これより大きいものは開かない)
MAX_SNIPPETS = 1000                # 定型文の件数の上限(読み込みで足すときだけ見る)
DEFAULT_NAME = "無題の定型文"          # 名前が空のとき(module.save_snippet と同じ)

# 読み込めなかった理由(ログ・ops に出してよいのはこのコードだけ)
ERR_TOO_LARGE = "too_large"
ERR_NOT_UTF8 = "not_utf8"
ERR_NOT_JSON = "not_json"
ERR_WRONG_FORMAT = "wrong_format"
ERR_NEWER_VERSION = "newer_version"
ERR_BAD_ENTRY = "bad_entry"
ERR_UNREADABLE = "unreadable"
ERR_CANNOT_REPLACE = "cannot_replace"

REASON_TEXTS: dict[str, str] = {
    ERR_TOO_LARGE: f"ファイルが大きすぎます({MAX_FILE_BYTES // (1024 * 1024)} MB まで)。",
    ERR_NOT_UTF8: "文字コードが UTF-8 ではありません。",
    ERR_NOT_JSON: "JSON として読めません。",
    ERR_WRONG_FORMAT: "ClipShelf の定型文のファイルではありません。",
    ERR_NEWER_VERSION: "新しい版の DeskKit で書き出したファイルです。DeskKit を更新してから読み込んでください。",
    ERR_BAD_ENTRY: "定型文の中に、名前か本文の形が合わないものがあります。",
    ERR_UNREADABLE: "ファイルを開けません。",
    ERR_CANNOT_REPLACE: "同じ名前のファイルをごみ箱へ移せませんでした。別の名前で書き出してください。",
}


class TransferError(Exception):
    """読み込めない・書き出せない。code は理由コード(本文・パスを含めない)。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

    @property
    def text(self) -> str:
        return REASON_TEXTS.get(self.code, "ファイルを扱えませんでした。")


@dataclass(frozen=True)
class Snippet:
    name: str
    text: str

    def __repr__(self) -> str:  # 本文を repr に出さない
        return "Snippet(...)"


@dataclass
class ImportPlan:
    to_add: list[Snippet] = field(default_factory=list)
    duplicates: int = 0     # 同じ名前・同じ本文が既にある(ファイルの中の重複も含む)
    over_limit: int = 0     # 件数の上限を超えるので足さない


def norm_name(name: str | None) -> str:
    return (name or "").strip() or DEFAULT_NAME


# ------------------------------------------------------------------ 書き出し
def build_export(snippets: Iterable[Snippet], now: datetime) -> bytes:
    """書き出すファイルの中身(UTF-8、BOM なし、末尾改行)。並びは名前順。"""
    items = sorted(snippets, key=lambda s: (norm_name(s.name).casefold(), s.text))
    doc = {
        "format": FORMAT_ID,
        "version": FORMAT_VERSION,
        "exported_at": now.astimezone().isoformat(timespec="seconds"),
        "note": "ClipShelf の定型文です。このファイルは暗号化されていません。",
        "snippets": [{"name": norm_name(s.name), "text": s.text} for s in items],
    }
    return (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _recycle_one(p: Path) -> bool:
    from deskkit import fileops

    return p in fileops.recycle([p]).sent


def write_file(path: Path, data: bytes, recycle: Callable[[Path], bool] | None = None) -> None:
    """一時ファイルに書いてから置き換える(途中で止まっても半端なファイルを残さない)。失敗は TransferError。
    一時ファイルは乱数の名前で新しく作る(利用者の同じ名前のファイルに触れない)。書き出し先に同じ名前のファイルが
    あれば、先にごみ箱へ送ってから置き換える(v0.4.1 レビュー 5。完全には消さない)。送れなければ置き換えない。"""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.part")
    made = False
    try:
        with open(tmp, "xb") as f:
            made = True
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if path.exists() and not (recycle or _recycle_one)(path):
            raise TransferError(ERR_CANNOT_REPLACE)
        os.replace(tmp, path)
    except (OSError, TransferError) as e:
        if made:
            try:
                tmp.unlink(missing_ok=True)  # 自分が作った一時ファイルだけ
            except OSError:
                pass
        if isinstance(e, TransferError):
            raise
        raise TransferError(ERR_UNREADABLE) from None


# ------------------------------------------------------------------ 読み込み
def read_file(path: Path) -> list[Snippet]:
    """ファイルを読んで定型文の一覧にする。大きすぎる・読めない・形が違うときは TransferError。"""
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise TransferError(ERR_TOO_LARGE)
        with open(path, "rb") as f:
            raw = f.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise TransferError(ERR_UNREADABLE) from None
    return parse(raw)


def parse(raw: bytes) -> list[Snippet]:
    if len(raw) > MAX_FILE_BYTES:
        raise TransferError(ERR_TOO_LARGE)
    try:
        text = raw.decode("utf-8-sig")  # メモ帳で保存し直した BOM 付きも受ける
    except UnicodeDecodeError:
        raise TransferError(ERR_NOT_UTF8) from None
    try:
        doc = json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        raise TransferError(ERR_NOT_JSON) from None
    if not isinstance(doc, dict) or doc.get("format") != FORMAT_ID:
        raise TransferError(ERR_WRONG_FORMAT)
    version = doc.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise TransferError(ERR_WRONG_FORMAT)
    if version > FORMAT_VERSION:
        raise TransferError(ERR_NEWER_VERSION)
    entries = doc.get("snippets")
    if not isinstance(entries, list):
        raise TransferError(ERR_WRONG_FORMAT)
    out: list[Snippet] = []
    for e in entries:
        if not isinstance(e, dict):
            raise TransferError(ERR_BAD_ENTRY)
        name, body = e.get("name", ""), e.get("text")
        if name is None:
            name = ""
        if not isinstance(name, str) or not isinstance(body, str) or not body.strip():
            raise TransferError(ERR_BAD_ENTRY)  # 画面でも本文が空の定型文は保存できない
        out.append(Snippet(norm_name(name), body))
    return out


def plan_import(existing: Sequence[Snippet], incoming: Sequence[Snippet], limit: int = MAX_SNIPPETS) -> ImportPlan:
    """既存に足す分を決める。同じ名前・同じ本文のものは足さない。上限を超える分は足さずに数える。"""
    seen = {(norm_name(s.name), s.text) for s in existing}
    room = max(0, limit - len(existing))
    plan = ImportPlan()
    for s in incoming:
        key = (norm_name(s.name), s.text)
        if key in seen:
            plan.duplicates += 1
            continue
        seen.add(key)
        if len(plan.to_add) >= room:
            plan.over_limit += 1
            continue
        plan.to_add.append(Snippet(key[0], key[1]))
    return plan
