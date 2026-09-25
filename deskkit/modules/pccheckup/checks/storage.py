# 「容量」の診断 S1〜S6(§6.4)。ドライブの空き・ごみ箱・古い一時ファイル・ダウンロード・大きいフォルダ・ストレージ センサー。
# フォルダの走査はリパースポイントをたどらない(P-6)。S5 は最大 60 秒で打ち切り、それまでの合計に「途中まで」と付ける(FR-8)。
# 空にする・消す操作はしない。ごみ箱を空にするのは利用者がエクスプローラーで行う(VINV-2)。
from __future__ import annotations

from deskkit.modules.pccheckup.checks.base import (
    Action,
    Cancel,
    Check,
    Finding,
    GiB,
    MiB,
    Row,
    Status,
    fmt_bytes,
    worst,
)
from deskkit.modules.pccheckup.checks.perf import disk_status
from deskkit.modules.pccheckup.probes import DiskInfo, FolderSize, Probes, SizeInfo

# ---- しきい値(P-2)。S1 は P3 と同じ(perf.disk_status)
RECYCLE_INFO = 1 * GiB     # ごみ箱がこれ以上で info
TEMP_WARN = 1 * GiB        # 7 日を超えた一時ファイルの合計
TEMP_INFO = 100 * MiB
DOWNLOADS_INFO = 10 * GiB
TOP_FOLDERS = 10
# ---- 時間の上限(FR-8: S5 を除いて 10 秒以内。S5 は 60 秒で打ち切る)
TEMP_LIMIT_S = 3.0
DOWNLOADS_LIMIT_S = 4.0
FOLDERS_LIMIT_S = 60.0

PARTIAL = "途中まで"
DENIED = "一部読めませんでした"


def _suffix(s: SizeInfo) -> str:
    return f"({PARTIAL})" if s.partial else ""


def _notes(s: SizeInfo) -> str:
    out = []
    if s.partial:
        out.append("時間がかかるため途中で打ち切りました。途中までの合計です。")
    if s.denied:
        out.append("一部のフォルダは読めませんでした。")
    return "".join(out)


# ------------------------------------------------------------------ S1 固定ドライブの空き
def judge_drives(drives: list[DiskInfo]) -> Finding:
    if not drives:
        return Finding("S1", "unknown", "ドライブの空きを読めませんでした", "固定ドライブが見つかりませんでした。")
    rows: list[Row] = []
    statuses: list[Status] = []
    for d in drives:
        st = disk_status(d)
        statuses.append(st)
        pct = d.free / d.total * 100 if d.total > 0 else 0.0
        rows.append(Row(d.root.rstrip("\\"), f"空き {fmt_bytes(d.free)} / {fmt_bytes(d.total)}({pct:.0f}%)", st))
    status = worst(*statuses)
    tightest = min(drives, key=lambda d: d.free / d.total if d.total > 0 else 0.0)
    value = f"{tightest.root.rstrip(chr(92))} 空き {fmt_bytes(tightest.free)}"
    if status in ("good", "unknown"):
        return Finding("S1", status, "ドライブの空きは十分です" if status == "good" else "ドライブの空きを一部読めませんでした",
                       f"{len(drives)} 台の固定ドライブの空きを調べました。", (), value, tuple(rows))
    names = "・".join(r.label for r, s in zip(rows, statuses, strict=True) if s in ("warn", "bad"))
    title = "空きがほとんど無いドライブがあります" if status == "bad" else "空きが少なくなっているドライブがあります"
    acts = (Action("この下の「ごみ箱」「古い一時ファイル」「ダウンロード」「大きいフォルダ」の項目で、空けられる場所を確かめてください。"),)
    return Finding("S1", status, title, f"{names} の空きが少なくなっています。", acts, value, tuple(rows))


# ------------------------------------------------------------------ S2 ごみ箱
def judge_recycle_bin(n: int) -> Finding:
    btn = Action("空にするのは、ごみ箱を開いて『ごみ箱を空にする』を押してください。", "uri", "shell:RecycleBinFolder", "ごみ箱を開く")
    if n >= RECYCLE_INFO:
        return Finding("S2", "info", f"ごみ箱を空にすると {fmt_bytes(n)} 空きます",
                       f"ごみ箱に {fmt_bytes(n)} 入っています。ごみ箱の中のファイルも、ディスクの容量を使っています。", (btn,),
                       fmt_bytes(n))
    return Finding("S2", "good", "ごみ箱はあまり容量を使っていません", f"ごみ箱に入っているのは {fmt_bytes(n)} です。", (), fmt_bytes(n))


# ------------------------------------------------------------------ S3 古い一時ファイル
def judge_temp(s: SizeInfo) -> Finding:
    btn = Action("7 日より前の一時ファイルだけを、確認してからごみ箱へ送ります。", "cleanup", None, "古い一時ファイルをごみ箱へ")
    v = fmt_bytes(s.bytes) + _suffix(s)
    base = f"一時ファイルのフォルダに、7 日より前から更新されていないファイルが {s.files} 個({fmt_bytes(s.bytes)})あります。"
    base += _notes(s)
    acts = (btn,) if s.files > 0 else ()
    if s.bytes >= TEMP_WARN:
        return Finding("S3", "warn", "古い一時ファイルがたまっています", base, acts, v)
    if s.bytes >= TEMP_INFO:
        return Finding("S3", "info", "古い一時ファイルが少したまっています", base, acts, v)
    return Finding("S3", "good", "古い一時ファイルはほとんどありません", base, acts, v)


# ------------------------------------------------------------------ S4 ダウンロード
def judge_downloads(path: str, s: SizeInfo) -> Finding:
    v = fmt_bytes(s.bytes) + _suffix(s)
    detail = f"ダウンロードフォルダに {fmt_bytes(s.bytes)}({s.files} 個のファイル)入っています。" + _notes(s)
    btn = Action("いらないファイルがないか見てください。", "folder", path, "フォルダを開く")
    if s.bytes >= DOWNLOADS_INFO:
        return Finding("S4", "info", "ダウンロードフォルダが大きくなっています", detail, (btn,), v)
    return Finding("S4", "good", "ダウンロードフォルダは大きくありません", detail, (), v)


# ------------------------------------------------------------------ S5 大きいフォルダ
def judge_folders(folders: list[FolderSize]) -> Finding:
    ranked = sorted(folders, key=lambda f: f.size.bytes, reverse=True)[:TOP_FOLDERS]
    rows = []
    for f in ranked:
        note = PARTIAL if f.size.partial else (DENIED if f.size.denied else None)
        rows.append(Row(f.name, fmt_bytes(f.size.bytes), None, f.path, note))
    partial = any(f.size.partial for f in folders)
    detail = "ユーザーフォルダの中のフォルダを、大きい順に並べました。"
    if partial:
        detail += "60 秒で打ち切ったため、途中までの合計です。"
    value = f"{ranked[0].name} {fmt_bytes(ranked[0].size.bytes)}" if ranked else "なし"
    return Finding("S5", "info", f"大きいフォルダ(上位 {len(ranked)} 件)" + (f"・{PARTIAL}" if partial else ""), detail,
                   (Action("大きいフォルダを開いて、いらないものが無いか見てください。"),), value, tuple(rows))


# ------------------------------------------------------------------ S6 ストレージ センサー
def judge_storage_sense(enabled: bool) -> Finding:
    if enabled:
        return Finding("S6", "good", "ストレージ センサーはオンです", "一時ファイルなどは Windows が自動で片づけています。", (), "オン")
    return Finding("S6", "info", "ストレージ センサーはオフです",
                   "ストレージ センサーをオンにすると、一時ファイルが自動で片づきます。",
                   (Action("", "uri", "ms-settings:storagesense", "ストレージ センサーの設定を開く"),), "オフ")


def _run_downloads(p: Probes, c: Cancel) -> Finding:
    path, size = p.downloads(DOWNLOADS_LIMIT_S, c)
    return judge_downloads(path, size)


# S5 は最後に回す(ほかの結果を先に出すため。FR-8)
CHECKS: tuple[Check, ...] = (
    Check("S1", "storage", "ドライブの空きを調べています", lambda p, _c: judge_drives(p.fixed_drives())),
    Check("S2", "storage", "ごみ箱の大きさを調べています", lambda p, _c: judge_recycle_bin(p.recycle_bin_bytes())),
    Check("S6", "storage", "ストレージ センサーの設定を調べています", lambda p, _c: judge_storage_sense(p.storage_sense())),
    Check("S3", "storage", "古い一時ファイルを数えています", lambda p, c: judge_temp(p.old_temp(TEMP_LIMIT_S, c))),
    Check("S4", "storage", "ダウンロードフォルダの大きさを調べています", _run_downloads),
    Check("S5", "storage", "大きいフォルダを探しています(最大 60 秒)", lambda p, c: judge_folders(p.user_folders(FOLDERS_LIMIT_S, c))),
)
TITLES: dict[str, str] = {"S1": "ドライブの空き", "S2": "ごみ箱", "S3": "古い一時ファイル", "S4": "ダウンロードフォルダ",
                          "S5": "大きいフォルダ", "S6": "ストレージ センサー"}
