# CLI ハンドラ(FR-4)。host が `deskkit mode ...` / `deskkit modeshift ...` から転送した引数列を解釈し、
# (終了コード, 短い文字列) を返す。終了コードは §9.1(0/1/2/4/5/6/7)。Qt に依存しない。
from __future__ import annotations

from typing import TYPE_CHECKING

from deskkit.modules.modeshift.service import EXIT_ARGS

if TYPE_CHECKING:
    from deskkit.modules.modeshift.service import ModeShiftService

USAGE = """使い方:
  deskkit mode <name>               モード適用(未確認モードは実行せず終了コード 5)
  deskkit mode <name> --dry-run     計画(Plan)を表で出力するだけ
  deskkit mode --undo [--dry-run]   直前の切替の前の状態へ戻す
  deskkit mode --list               モード一覧(無効は理由つき)
  deskkit mode --status             現在のモード・最終切替時刻・undo 可否
  deskkit mode --selftest           偽の実装で一巡して検査"""


def handle(service: ModeShiftService | None, args: list[str]) -> tuple[int, str]:
    a = [str(x) for x in args]
    if not a or a[0] in ("-h", "--help", "help"):
        return EXIT_ARGS, USAGE
    dry = "--dry-run" in a
    rest = [x for x in a if x != "--dry-run"]
    if rest == ["--selftest"] and not dry:
        from deskkit.modules.modeshift import selftest

        code = selftest.run(quiet=True)
        return (0, "selftest: 合格") if code == 0 else (1, "selftest: 不合格(ログを確認してください)")
    if service is None:
        return EXIT_ARGS, "ModeShift が起動していません"
    if rest == ["--list"] and not dry:
        return 0, service.list_text()
    if rest == ["--status"] and not dry:
        return 0, service.status_text()
    if rest == ["--undo"]:
        return service.request_undo(dry_run=dry, source="cli")
    if len(rest) == 1 and not rest[0].startswith("-"):
        return service.switch_mode(rest[0], dry_run=dry, source="cli")
    return EXIT_ARGS, "引数を解釈できません\n" + USAGE
