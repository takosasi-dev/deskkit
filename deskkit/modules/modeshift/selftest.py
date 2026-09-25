# FR-24: 偽の Win32 / Core Audio / powercfg で「検証 → 計画(dry-run) → 確認つき実行 → undo」を一巡し、
# Plan と ops.jsonl・snapshot.json の中身を検査して 0(合格)/ 1(不合格)を返す。
# 一時フォルダだけを使い、実機の電源プラン・音量・アプリ・%LOCALAPPDATA%\DeskKit には触らない。
from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable
from pathlib import Path

from deskkit.modules.modeshift import cli
from deskkit.modules.modeshift.fakes import GUID_A, GUID_B, FakeCtx, FakeUi, fake_system, populate, sample_section
from deskkit.modules.modeshift.service import ModeShiftService


def _lines(p: Path) -> list[dict[str, object]]:
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _head_hash(p: Path, n: int) -> str:
    return hashlib.sha256(p.read_bytes()[:n]).hexdigest()


def run(quiet: bool = False) -> int:
    failures: list[str] = []
    out: Callable[[str], None] = (lambda _s: None) if quiet else print

    def check(cond: bool, what: str) -> None:
        out(("  OK   " if cond else "  NG   ") + what)
        if not cond:
            failures.append(what)

    with tempfile.TemporaryDirectory(prefix="modeshift-selftest-") as td:
        base = Path(td)
        sysm = fake_system()
        populate(sysm)
        ctx = FakeCtx(base / "data", sample_section(base), games=frozenset({"game.exe"}))
        ui = FakeUi()
        svc = ModeShiftService(ctx, sysm.backends(), ui, threaded=False)
        ops = base / "data" / "ops.jsonl"

        out("[1] 設定の検証(FR-1)")
        cfg = svc.config
        check(cfg.mode("game") is not None and cfg.mode("game").valid, "game は有効")  # type: ignore[union-attr]
        for bad in ("bad_type", "bad_close", "bad_url"):
            m = cfg.mode(bad)
            check(m is not None and not m.valid and bool(m.reason()), f"{bad} は理由つきで無効")
        code, text = cli.handle(svc, ["--list"])
        check(code == 0 and "無効" in text and "未知のアクション種別" in text, "--list に無効と理由が出る")

        out("[2] 未確認モードを CLI で本番指定 → 5(FR-8)")
        code, _ = cli.handle(svc, ["game"])
        check(code == 5, "終了コード 5")
        check(sysm.power.active == GUID_A and not sysm.power.set_calls, "電源は変わらない")
        check(not ops.exists() or not _lines(ops), "ops.jsonl に行が増えない")

        out("[3] dry-run(AC-3)")
        master_before = sysm.audio.master
        code, text = cli.handle(svc, ["game", "--dry-run"])
        rows = _lines(ops)
        tl = text.splitlines()
        check(code == 0 and "#\t種別\t対象\t現在\t変更後\t予定" in tl and sum(x.startswith(("1\t", "8\t")) for x in tl) == 2,
              "終了コード 0 とタブ区切りの表")
        check(len(rows) == 8 and all(r["dry_run"] is True for r in rows), "ops.jsonl に dry_run: true の 8 行だけ")
        check(sysm.power.active == GUID_A and sysm.audio.master == master_before and not sysm.launcher.launched
              and not sysm.opener.opened and not sysm.procs.posted, "何も変わらない")
        check("secret" not in ops.read_text(encoding="utf-8"), "URL のクエリをログに書かない(INV-11)")

        out("[4] 3つの入口で同じ Plan(AC-5)")
        sig_cli = svc.make_plan(cfg.mode("game"), source="cli", dry_run=True, in_game=False).signature()  # type: ignore[arg-type]
        svc.switch_mode("game", dry_run=True, source="tray")
        svc.switch_mode("game", dry_run=True, source="hotkey")
        check(len(ui.previews) == 2 and all(p.signature() == sig_cli for p in ui.previews), "Step 列が一致")

        out("[5] トレイから実行 → プレビュー → 「実行」(FR-8 / FR-10)")
        head_n = ops.stat().st_size
        head_h = _head_hash(ops, head_n)
        n_before = len(_lines(ops))
        ui.previews.clear()
        seen: list[dict[str, object]] = []
        ctx.on("layout.apply", lambda p: seen.append(dict(p)))
        svc.switch_mode("game", dry_run=False, source="tray")
        check(len(ui.previews) == 1 and ui.previews[0].needs_confirmation, "未確認なのでプレビューが出る")
        plan = ui.previews[0]
        code, _ = svc.execute_from_preview(plan)
        check(code == 0 and plan.finished, "実行完了")
        game = svc.config.mode("game")
        check(game is not None and game.confirmed, "confirmed_hash が記録された")
        check(sysm.power.active == GUID_B, "電源プランが切り替わった(読み戻し一致)")
        check(sysm.audio.master is not None and abs(sysm.audio.master.level - 0.3) < 1e-9, "マスター音量 30%")
        check(abs(sysm.audio.sessions["sess-chat-1"].level - 0.4) < 1e-9, "chat.exe のセッション 40%")
        check(not any(e == "mail.exe" for e in sysm.procs.procs.values()), "mail.exe は WM_CLOSE で終了")
        check(len(sysm.launcher.launched) == 1 and sysm.launcher.launched[0][1] == ["--quiet"], "editor.exe を引数つきで起動")
        check(("url", "https://example.com/page?token=secret") in sysm.opener.opened, "URL を既定のハンドラへ")
        check(len(seen) == 1 and seen[0] == {"layout": "game", "source": "modeshift",
                                            "request_id": f"{plan.run_id}-8", "wait_s": 3.0},
              "layout.apply を §9.4 の形で1回送信(直前に起動したので wait_s=3)")
        rows = _lines(ops)
        new_rows = rows[n_before:]
        check(len(new_rows) == 8 and all(r["run_id"] == plan.run_id and r["dry_run"] is False for r in new_rows),
              "ops.jsonl に 8 行(同じ run_id)")
        check(all(r["result"] == "ok" for r in new_rows), "全 Step ok")
        check(_head_hash(ops, head_n) == head_h, "既存行は変わらない(AC-17)")
        check(any(t[0] == "modeshift.switched" for t in ctx.emitted), "modeshift.switched を送信")

        out("[6] snapshot と undo(FR-9 / FR-19)")
        snap = json.loads((base / "data" / "snapshot.json").read_text(encoding="utf-8"))
        check(snap["power"] == {"before": GUID_A, "written": GUID_B}, "snapshot.power")
        check(snap["master"]["before"]["level"] == 0.62 and snap["master"]["written"]["level"] == 0.3, "snapshot.master")
        check(snap["apps"][0]["before"] == 1.0 and snap["apps"][0]["written"] == 0.4, "snapshot.apps")
        code, text = cli.handle(svc, ["--undo", "--dry-run"])
        check(code == 0 and sysm.power.active == GUID_B, "undo の dry-run は何も変えない")
        code, text = cli.handle(svc, ["--undo"])
        check(code == 0, f"undo 終了コード 0({code})")
        check(sysm.power.active == GUID_A, "電源プランが戻った")
        check(sysm.audio.master is not None and abs(sysm.audio.master.level - 0.62) < 1e-9, "マスター音量が戻った")
        check(abs(sysm.audio.sessions["sess-chat-1"].level - 1.0) < 1e-9, "アプリ音量が戻った")
        check(len(sysm.launcher.launched) == 1 and not sysm.procs.forced, "undo でアプリを起動・終了しない(INV-4)")
        code, _ = cli.handle(svc, ["--undo"])
        check(code == 7, "2回目の undo は 7(1段のみ)")

        out("[7] 手で変えた項目は戻さない(D-6 / AC-10)")
        code, _ = cli.handle(svc, ["game"])
        check(code == 0, "確認済みなので CLI から 0")
        sysm.audio.set_master(0.8, None)   # 利用者が手で変えた
        code, text = cli.handle(svc, ["--undo"])
        check("手動で変更済み" in text, "出力に「手動で変更済み」")
        check(sysm.audio.master is not None and abs(sysm.audio.master.level - 0.8) < 1e-9, "マスター音量は手で変えた値のまま")
        check(sysm.power.active == GUID_A, "電源は戻る")
        code, text = cli.handle(svc, ["--status"])
        check(code == 0 and "現在のモード" in text, "--status")
        code, _ = cli.handle(svc, ["nope"])
        check(code == 2, "無いモードは 2")
        code, _ = cli.handle(svc, ["--bogus"])
        check(code == 1, "解釈できない引数は 1")

    out("selftest: " + ("合格" if not failures else f"不合格 {len(failures)} 件"))
    return 0 if not failures else 1
