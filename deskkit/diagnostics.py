# 診断レポート(H4)。不具合の相談に貼れる、DeskKit の状態の要約テキストを作る。
# 載せるのは版・OS・DPI・モニタ・モジュールの状態・例外の回数・ホットキー・更新設定・一時停止など「状態」だけ。
# パス・ユーザー名・exe 名・ウィンドウタイトル・クリップボードや URL の中身は載せない(scrub で最後にもう一度消す)。
from __future__ import annotations

import datetime as _dt
import getpass
import os
import platform
import re
import sys
from pathlib import Path
from typing import Any

from deskkit import APP_NAME, __version__, catalog, paths

_MAX_VALUE = 160
_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\)[^\s'\"<>|]*")
_PATH_TAIL_RE = re.compile(r"<path>[^\s'\"<>|]*")
_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
_EXE_RE = re.compile(r"[^\s'\"\\/:<>|]+\.(?:exe|dll|lnk|bat|cmd|ps1|msi)\b", re.IGNORECASE)
# 例外の回数のキーのうち、そのまま載せてよい接頭辞(tray:/quick: はトレイ項目の名前=利用者のモード名などを含むので伏せる)
_SAFE_ERROR_PREFIXES = ("event:", "native:", "timer:", "hotkey:", "notify:", "usage", "handle_cli", "call_soon")


def _secrets() -> list[str]:
    out: set[str] = set()
    for v in (os.environ.get("USERPROFILE"), os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA"),
              os.environ.get("HOMEPATH"), os.environ.get("ONEDRIVE"), str(Path.home())):
        if v and len(v) >= 3:
            out.add(v)
    for v in (os.environ.get("USERNAME"), _user()):
        if v and len(v) >= 2:
            out.add(v)
    return sorted(out, key=len, reverse=True)  # 長いもの(パス)から先に消す


def _user() -> str | None:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return None


def scrub(text: object, limit: int = _MAX_VALUE) -> str:
    """パス・URL・exe 名・ユーザー名を伏せ字にし、長さを切る。"""
    s = str(text)
    for sec in _secrets():
        s = re.sub(re.escape(sec), "<user>" if "\\" not in sec and "/" not in sec else "<path>", s, flags=re.IGNORECASE)
    s = _URL_RE.sub("<url>", s)
    s = _PATH_RE.sub("<path>", s)
    s = _PATH_TAIL_RE.sub("<path>", s)  # 伏せたプロファイルの下のフォルダ名・ファイル名も消す
    s = _EXE_RE.sub("<exe>", s)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _os_text() -> str:
    try:
        v = sys.getwindowsversion()  # type: ignore[attr-defined,unused-ignore]
        return f"Windows {v.major}.{v.minor} build {v.build}"
    except AttributeError:
        return f"{platform.system()} {platform.release()}"


def _monitors() -> str:
    try:
        from PySide6.QtGui import QGuiApplication

        screens = QGuiApplication.screens()
        scales = [f"{round(sc.devicePixelRatio() * 100)}%" for sc in screens]
        return f"{len(screens)} 台(倍率 {', '.join(scales) or '—'})"
    except Exception:  # noqa: BLE001
        return "不明"


def _yn(v: object) -> str:
    return "はい" if v else "いいえ"


def _error_counts(ctx: Any) -> str:
    totals: dict[str, int] = dict(getattr(ctx, "error_totals", {}) or {})
    if not totals:
        return "0"
    shown: dict[str, int] = {}
    for key, n in totals.items():
        k = key if key.startswith(_SAFE_ERROR_PREFIXES) else key.split(":", 1)[0] + ":*"
        shown[k] = shown.get(k, 0) + int(n)
    parts = ", ".join(f"{scrub(k, 60)}={n}" for k, n in sorted(shown.items()))
    return f"{sum(totals.values())}({parts})"


def _module_diag(mod: Any) -> list[str]:
    fn = getattr(mod, "diagnostics", None)
    if fn is None or not callable(fn):
        return []
    try:
        got = fn()
    except Exception as e:  # noqa: BLE001 - 診断の失敗でレポート全体を失敗させない
        return [f"    diagnostics(): 例外 {type(e).__name__}"]
    if not isinstance(got, dict):
        return ["    diagnostics(): dict ではありません"]
    out = []
    for k, v in list(got.items())[:40]:
        if isinstance(v, bool):
            val = "true" if v else "false"
        elif isinstance(v, int):
            val = str(v)
        elif isinstance(v, str):
            val = scrub(v)
        else:
            val = f"<{type(v).__name__}>"
        out.append(f"    {scrub(k, 60)}: {val}")
    return out


def build_report(host: Any, now: _dt.datetime | None = None) -> str:
    ts = now or _dt.datetime.now()
    hs: dict[str, Any] = host.settings.host()
    lines: list[str] = [
        f"# {APP_NAME} 診断レポート",
        f"作成: {ts:%Y-%m-%d %H:%M}",
        f"版: {__version__}({'exe' if paths.is_frozen() else 'ソース実行'})",
        f"Python {platform.python_version()}",
        f"OS: {_os_text()}",
        f"DPI awareness: {host.dpi_awareness}",
        f"モニタ: {_monitors()}",
        f"テーマ: {hs.get('theme')} / 通知: {hs.get('notification_style')} / ゲーム・全画面中の通知の保留: {_yn(hs.get('hold_notifications'))}",
        f"全画面の判定: {hs.get('fullscreen_detection')} / ゲームとして扱うアプリ: {len(host.settings.game_processes())} 件",
    ]
    sn = getattr(host, "snooze", None)
    if sn is not None:
        lines.append(f"一時停止: {sn.status_text() or 'なし'} / Windows の通知状態に連動: {_yn(hs.get('snooze_follow_quns'))}")
    hold = getattr(host, "hold", None)
    if hold is not None:
        lines.append(f"保留中の通知: {hold.count} 件")

    lines += ["", "## モジュール"]
    errors = getattr(host.settings, "module_errors", {}) or {}
    slots = host.loader.slots
    names = list(dict.fromkeys([*catalog.MODULE_NAMES, *slots.keys()]))
    for name in names:
        if name.startswith("_"):
            continue
        slot = slots.get(name)
        enabled = bool(host.settings.module_section(name).get("enabled", False))
        state = slot.state if slot else "disabled"
        row = f"- {name}: 有効={_yn(enabled)} 状態={state}"
        if slot is not None and slot.reason:
            row += f" 理由={scrub(slot.reason)}"
        if name in errors:
            row += f" 設定エラー={scrub(errors[name])}"
        ctx = slot.ctx if slot else None
        if ctx is not None:
            row += f" 例外(今回の起動から)={_error_counts(ctx)}"
        lines.append(row)
        if slot is not None and slot.module is not None and state == "running":
            lines += _module_diag(slot.module)

    lines += ["", "## ホットキー"]
    hub = host.hotkeys
    regs = sorted(hub.registry.names())
    lines.append(f"登録: {len(regs)} 件")
    for n in regs:
        lines.append(f"- {scrub(n, 80)} = {hub.combo_text(n) or '?'}")
    failed = {n: t for n, t in getattr(hub, "failed", {}).items() if n not in regs}
    if failed:
        lines.append(f"登録できなかった: {len(failed)} 件")
        for n, t in sorted(failed.items()):
            lines.append(f"- {scrub(n, 80)} = {scrub(t, 60)}")

    lines += ["", "## アップデート"]
    upd = getattr(host, "updates", None)
    if upd is not None:
        c = upd.cfg()
        lines.append(f"自動確認: {_yn(c.get('auto_check'))} / 取得先: {scrub(c.get('repo'), 80)} / "
                     f"前回の確認: {scrub(c.get('last_check') or 'なし', 40)} / 状態: {upd.state}")
        if c.get("skip_version"):
            lines.append(f"スキップする版: {scrub(c.get('skip_version'), 20)}")

    snaps = getattr(host, "snapshots", None)
    if snaps is not None:
        try:
            n = len(snaps.list())
        except OSError:
            n = -1
        lines += ["", "## 設定の世代", f"保存済み: {n} 世代(上限 {hs.get('settings_history_keep')})"]
    return "\n".join(lines) + "\n"
