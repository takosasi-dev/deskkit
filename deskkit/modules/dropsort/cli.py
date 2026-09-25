# 明示コマンド(§9.4): status / sort-existing [--apply] / archive-now [--apply] / undo [--count N]。
# host の handle_cli と単独起動(python -m deskkit.modules.dropsort.cli)の両方がここを使う。
# 終了コード: 0 正常 / 1 設定エラー / 2 ロック取得失敗 / 3 一部が拒否・失敗 / 4 ダウンロードフォルダを解決できない。
from __future__ import annotations

import argparse
import ntpath
import sys
from typing import Any

from deskkit.modules.dropsort.oplog import LockBusyError
from deskkit.modules.dropsort.service import OP_TEXT, BatchResult, DropSortService, reason_text

EXIT_OK, EXIT_CONFIG, EXIT_LOCK, EXIT_PARTIAL, EXIT_UNRESOLVED = 0, 1, 2, 3, 4
USAGE = ("使い方: dropsort status | sort-existing [--apply] | archive-now [--apply] | undo [--count N] | --selftest")


class _ArgError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:
        raise _ArgError(message)


def _parser() -> _Parser:
    p = _Parser(prog="dropsort", add_help=False)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("status", add_help=False)
    s = sub.add_parser("sort-existing", add_help=False)
    s.add_argument("--apply", action="store_true")
    a = sub.add_parser("archive-now", add_help=False)
    a.add_argument("--apply", action="store_true")
    u = sub.add_parser("undo", add_help=False)
    u.add_argument("--count", type=int, default=None)
    return p


def _line(i: dict[str, Any]) -> str:
    op = str(i.get("op"))
    s = f"[{OP_TEXT.get(op, op)}] {i.get('name') or ntpath.basename(str(i.get('src') or ''))}"
    if i.get("dst") and op not in ("flagged", "no_match", "skip"):
        s += f" → {i.get('dst')}"
    if i.get("rule"):
        s += f"  (ルール: {i.get('rule')})"
    if i.get("reason"):
        s += f"  理由: {reason_text(str(i.get('reason')))}"
    return s


def format_batch(title: str, r: BatchResult, apply: bool) -> str:
    lines = [f"{title}({'本番' if apply else '試運転 — 何も動かしていません'})", f"ダウンロードフォルダ: {r.downloads}"]
    shown = [i for i in r.items if i.get("op") != "no_match"]
    for i in shown:
        lines.append("  " + _line(i))
    nm = r.count("no_match")
    if nm:
        lines.append(f"  (どのルールにも当たらないファイル: {nm} 件)")
    if not r.items:
        lines.append("  対象のファイルはありません")
    ops = ("move", "archive", "refused", "failed", "flagged") if apply else ("would_move", "would_archive", "would_refuse", "flagged")
    lines.append("集計: " + " / ".join(f"{OP_TEXT[o]} {r.count(o)}" for o in ops))
    if not apply and r.count("would_move", "would_archive"):
        lines.append("実際に動かすには --apply を付けて実行してください。")
    return "\n".join(lines)


def format_status(st: dict[str, Any]) -> str:
    lines = [f"監視先: {st['downloads'] or '(解決できません)'}",
             f"状態: {'一時停止中' if st['paused'] else '動作中'}  監視方式: {st['watch_mode']}",
             f"ルール: {len(st['rules'])} 件 " + ", ".join(f"{n}({m})" for n, m in st["rules"])]
    if st["invalid_rules"]:
        lines.append("無効/使えないルール:")
        lines += [f"  {n}: {msg}" for n, msg in st["invalid_rules"]]
    lines.append("直近の操作:")
    if not st["recent"]:
        lines.append("  (なし)")
    for r in reversed(st["recent"]):
        lines.append(f"  {r.get('ts', '')} " + _line(dict(r, name=ntpath.basename(str(r.get('src') or '')))))
    return "\n".join(lines)


def run_command(service: DropSortService, args: list[str], *, lock_timeout: float = 3.0) -> tuple[int, str]:
    try:
        ns = _parser().parse_args(args)
    except _ArgError as e:
        return EXIT_CONFIG, f"引数を解釈できません: {e}\n{USAGE}"
    if ns.cmd is None:
        return EXIT_CONFIG, USAGE
    try:
        if ns.cmd == "status":
            st = service.status()
            return (EXIT_OK if st["downloads"] else EXIT_UNRESOLVED), format_status(st)
        if ns.cmd in ("sort-existing", "archive-now"):
            fn = service.sort_existing if ns.cmd == "sort-existing" else service.archive_now
            r = fn(ns.apply, timeout=lock_timeout)
            if r.error == "unresolved":
                return EXIT_UNRESOLVED, "ダウンロードフォルダを解決できません"
            title = "既存ファイルの整理" if ns.cmd == "sort-existing" else "アーカイブの評価"
            code = EXIT_PARTIAL if (ns.apply and r.count("refused", "failed")) else EXIT_OK
            return code, format_batch(title, r, ns.apply)
        if ns.cmd == "undo":
            n = ns.count if ns.count is not None else service.cfg.undo_default_count
            if n < 1:
                return EXIT_CONFIG, "--count は 1 以上にしてください"
            u = service.undo(n, timeout=lock_timeout)
            if u.nothing:
                return EXIT_OK, "元に戻せる操作はありません"
            lines = [f"元に戻した: {len(u.restored)} 件"]
            lines += [f"  {i.get('src')} → {i.get('dst')}" for i in u.restored]
            if u.problems:
                lines.append(f"戻せなかった: {len(u.problems)} 件")
                lines += [f"  {i.get('src')}  理由: {reason_text(str(i.get('reason')))}" for i in u.problems]
            return (EXIT_PARTIAL if u.problems else EXIT_OK), "\n".join(lines)
    except LockBusyError as e:
        return EXIT_LOCK, str(e)
    return EXIT_CONFIG, USAGE


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    if "--selftest" in args:
        from deskkit.modules.dropsort import selftest

        return selftest.run()
    from deskkit import paths
    from deskkit.modules.dropsort._win32 import RealWin32
    from deskkit.modules.dropsort.config import ConfigError, fill_defaults, load_config
    from deskkit.settings import SettingsStore

    section: dict[str, Any] = {}
    if paths.settings_path().exists():  # 無ければ既定値で動かす(設定ファイルは作らない)
        store = SettingsStore(paths.settings_path())
        lr = store.load()
        if not lr.ok:
            print(f"設定を読めません: {lr.error}")
            return EXIT_CONFIG
        section = store.module_section("dropsort")
    try:
        sec, _ = fill_defaults(section)
        cfg = load_config(sec)
    except ConfigError as e:
        print(f"設定エラー: {e}")
        return EXIT_CONFIG
    svc = DropSortService(RealWin32(), paths.data_dir("dropsort"), cfg)
    code, text = run_command(svc, args)
    print(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
