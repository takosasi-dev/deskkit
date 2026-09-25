# 切替・dry-run・確認フロー・undo・busy・layout.apply・ops.jsonl の受け入れ基準(偽の OS 実装で)。
# AC-3 / AC-4(論理) / AC-5 / AC-10 / AC-13 / AC-16 / AC-17 と FR-14 / FR-20 / FR-21 / D-6 の周辺。
from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

from deskkit.modules.modeshift import cli
from deskkit.modules.modeshift.fakes import GUID_A, GUID_B, FakeForeground

from .conftest import Env


def _sha_prefix(p: Path, n: int) -> str:
    return hashlib.sha256(p.read_bytes()[:n]).hexdigest()


# ---------------------------------------------------------------- AC-3
def test_ac3_dry_run_changes_nothing(env: Env) -> None:
    procs_before = dict(env.sys.procs.procs)
    master_before = env.sys.audio.master
    code, text = cli.handle(env.svc, ["game", "--dry-run"])
    assert code == 0
    assert "#\t種別\t対象\t現在\t変更後\t予定" in text.splitlines()
    assert env.sys.power.active == GUID_A and not env.sys.power.set_calls
    assert env.sys.audio.master == master_before and not env.sys.audio.master_calls and not env.sys.audio.session_calls
    assert env.sys.procs.procs == procs_before and not env.sys.procs.posted and not env.sys.launcher.launched
    assert not env.sys.opener.opened and not env.ctx.emitted
    rows = env.ops()
    assert len(rows) == 8 and all(r["dry_run"] is True for r in rows)
    assert not (env.tmp / "data" / "snapshot.json").exists()


# ---------------------------------------------------------------- AC-4(論理)
def test_ac4_unconfirmed_cli_5_then_preview_confirms(env_unconfirmed: Env) -> None:
    e = env_unconfirmed
    assert cli.handle(e.svc, ["game"])[0] == 5
    assert not e.ops() and e.sys.power.active == GUID_A
    # トレイから → プレビュー → 実行
    e.svc.switch_mode("game", dry_run=False, source="tray")
    assert len(e.ui.previews) == 1 and e.ui.previews[0].needs_confirmation
    assert e.svc.execute_from_preview(e.ui.previews[0])[0] == 0
    game = e.ctx.settings_dict()["modes"][0]
    assert game["confirmed_hash"] == e.svc.config.mode("game").def_hash  # type: ignore[union-attr]
    assert cli.handle(e.svc, ["game"])[0] == 0


def test_definition_change_requires_reconfirm(env: Env) -> None:
    sec = env.ctx.settings_dict()
    sec["modes"][1]["actions"][0]["level"] = 0.25
    env.ctx.write_settings(sec)
    env.svc.reload()
    assert cli.handle(env.svc, ["study"])[0] == 5


def test_preview_always_policy(tmp_path: Path) -> None:
    e = Env(tmp_path, confirm=True, edit=lambda s: s.update(preview="always"))
    e.svc.switch_mode("study", dry_run=False, source="hotkey")
    assert len(e.ui.previews) == 1 and not e.ui.previews[0].needs_confirmation
    assert not e.sys.audio.master_calls
    assert cli.handle(e.svc, ["study"])[0] == 0      # CLI はプレビューを出さない
    assert e.sys.audio.master_calls


def test_plan_is_executed_once_and_same_object(env_unconfirmed: Env) -> None:
    e = env_unconfirmed
    e.svc.switch_mode("game", dry_run=True, source="tray")
    plan = e.ui.previews[0]
    sig = plan.signature()
    assert e.svc.execute_from_preview(plan)[0] == 0
    assert plan.signature() == sig and all(s.result for s in plan.steps)
    assert e.svc.execute_from_preview(plan)[0] == 1   # 同じ Plan は二度実行しない


# ---------------------------------------------------------------- AC-5
def test_ac5_same_plan_from_all_entrypoints(env: Env) -> None:
    code, text = cli.handle(env.svc, ["game", "--dry-run"])
    assert code == 0
    sources = []
    for src in ("tray", "hotkey"):
        env.svc.switch_mode("game", dry_run=True, source=src)
        sources.append(env.ui.previews[-1])
    cli_rows = [ln.split("\t") for ln in text.splitlines() if ln[:1].isdigit()]
    cli_sig = [(r[1], r[2], r[4]) for r in cli_rows]
    for p in sources:
        assert [(s.type_label, s.target, s.new) for s in p.steps] == cli_sig
    assert sources[0].signature() == sources[1].signature()


# ---------------------------------------------------------------- 実行・undo・D-6(AC-10)
def test_execute_then_undo_restores_power_and_volume_only(env: Env) -> None:
    assert cli.handle(env.svc, ["game"])[0] == 0
    assert env.sys.power.active == GUID_B
    launched = len(env.sys.launcher.launched)
    code, text = cli.handle(env.svc, ["--undo"])
    assert code == 0, text
    assert env.sys.power.active == GUID_A
    assert env.sys.audio.master is not None and abs(env.sys.audio.master.level - 0.62) < 1e-9
    assert abs(env.sys.audio.sessions["sess-chat-1"].level - 1.0) < 1e-9
    assert len(env.sys.launcher.launched) == launched and not env.sys.procs.forced   # INV-4
    assert cli.handle(env.svc, ["--undo"])[0] == 7


def test_ac10_manual_change_is_not_undone(env: Env) -> None:
    cli.handle(env.svc, ["game"])
    env.sys.audio.set_master(0.77, None)          # 手で変えた
    code, text = cli.handle(env.svc, ["--undo", "--dry-run"])
    assert code == 0 and "手動で変更済み" in text
    code, text = cli.handle(env.svc, ["--undo"])
    assert "手動で変更済み" in text
    assert env.sys.audio.master is not None and abs(env.sys.audio.master.level - 0.77) < 1e-9
    assert env.sys.power.active == GUID_A


def test_undo_skips_when_device_changed_or_process_gone(env: Env) -> None:
    from deskkit.modules.modeshift.system import MasterState

    cli.handle(env.svc, ["game"])
    m = env.sys.audio.master
    assert m is not None
    env.sys.audio.master = MasterState(m.level, m.mute, "{other-device}")
    chat_pid = next(p for p, e in env.sys.procs.procs.items() if e == "chat.exe")
    del env.sys.procs.procs[chat_pid]
    env.sys.procs.add("chat.exe")                 # 同名の別 PID には書かない
    code, text = cli.handle(env.svc, ["--undo"])
    assert "既定の再生デバイスが変わった" in text and "プロセスなし" in text
    assert env.sys.audio.master is not None and abs(env.sys.audio.master.level - 0.3) < 1e-9


def test_undo_without_snapshot_or_corrupt(env: Env) -> None:
    assert cli.handle(env.svc, ["--undo"])[0] == 7
    (env.tmp / "data" / "snapshot.json").write_text("{壊れた", encoding="utf-8")
    assert cli.handle(env.svc, ["--undo"])[0] == 7


def test_snapshot_save_failure_blocks_execution(env: Env) -> None:
    def boom(_snap: object) -> None:
        raise OSError("disk full")

    env.svc.snapshots.save = boom  # type: ignore[method-assign,assignment]
    code, _ = cli.handle(env.svc, ["game"])
    assert code == 4
    assert env.sys.power.active == GUID_A and not env.sys.launcher.launched
    assert all(r["result"] == "failed" for r in env.ops())


# ---------------------------------------------------------------- close_app / FR-14
def test_close_still_running_exit_4(env: Env) -> None:
    env.sys.procs.close_behavior["mail.exe"] = "notray"
    code, text = cli.handle(env.svc, ["game"])
    assert code == 4 and "終了せず" in text
    assert not env.sys.procs.forced


def test_force_kill_only_with_all_conditions(tmp_path: Path) -> None:
    def edit(s: dict) -> None:  # type: ignore[type-arg]
        s["allow_force_kill"] = True
        s["modes"][0]["actions"][3]["force_on_timeout"] = True

    e = Env(tmp_path, confirm=True, edit=edit)
    e.sys.procs.close_behavior["mail.exe"] = "stay"
    assert cli.handle(e.svc, ["game"])[0] == 4          # CLI からは強制終了しない
    assert not e.ui.force_asked and not e.sys.procs.forced
    # 対話的な入口: 確認で「しない」→ 何もしない
    e.svc.switch_mode("game", dry_run=False, source="hotkey")
    assert e.ui.force_asked and not e.sys.procs.forced
    # 承認 → 強制終了
    e.ui.force_answer = True
    e.svc.switch_mode("game", dry_run=False, source="hotkey")
    assert e.sys.procs.forced


def _force_env(tmp_path: Path) -> Env:
    def edit(s: dict) -> None:  # type: ignore[type-arg]
        s["allow_force_kill"] = True
        s["modes"][0]["actions"][3]["force_on_timeout"] = True

    e = Env(tmp_path, confirm=True, edit=edit)
    e.sys.procs.close_behavior["mail.exe"] = "stay"
    e.ui.force_answer = True
    return e


def test_force_kill_does_not_hit_reused_pid(tmp_path: Path) -> None:
    """確認ダイアログを待つ間に対象が終わり、同じ PID が別のプロセスに再利用されても、承認で別のプロセスを終了させない。"""
    e = _force_env(tmp_path)
    mail_pid = next(p for p, x in e.sys.procs.procs.items() if x == "mail.exe")

    def reuse(_exe: str, pid: int) -> None:
        e.sys.procs.reuse_pid(pid, "notes.exe")     # mail.exe は自分で終わり、PID が notes.exe に使われた

    e.ui.on_force_ask = reuse
    e.svc.switch_mode("game", dry_run=False, source="hotkey")
    assert e.ui.force_asked == [("mail.exe", mail_pid)]
    assert not e.sys.procs.forced
    assert e.sys.procs.procs.get(mail_pid) == "notes.exe"            # 別のプロセスは生きている
    assert e.sys.procs.handles and all(h.closed for h in e.sys.procs.handles)   # ハンドルは必ず閉じる
    row = next(r for r in e.ops() if r["type"] == "close_app")
    assert row["result"] == "ok"                                     # 対象はもう居ない


def test_force_kill_same_exe_new_instance_is_not_killed(tmp_path: Path) -> None:
    """再利用先が同じ exe 名の新しいプロセスでも、確認前に開いたハンドルの相手(元のプロセス)以外は終了させない。"""
    e = _force_env(tmp_path)
    mail_pid = next(p for p, x in e.sys.procs.procs.items() if x == "mail.exe")
    e.ui.on_force_ask = lambda _exe, pid: e.sys.procs.reuse_pid(pid, "mail.exe")
    e.svc.switch_mode("game", dry_run=False, source="hotkey")
    assert not e.sys.procs.forced and e.sys.procs.procs.get(mail_pid) == "mail.exe"
    assert all(h.closed for h in e.sys.procs.handles)


def test_force_kill_uses_handle_and_closes_it(tmp_path: Path) -> None:
    e = _force_env(tmp_path)
    mail_pid = next(p for p, x in e.sys.procs.procs.items() if x == "mail.exe")
    e.svc.switch_mode("game", dry_run=False, source="hotkey")
    assert e.sys.procs.forced == [mail_pid] and mail_pid not in e.sys.procs.procs
    assert len(e.sys.procs.handles) == 1 and e.sys.procs.handles[0].closed
    # 確認で「しない」でもハンドルは閉じる
    e2 = _force_env(tmp_path / "b")
    e2.ui.force_answer = False
    e2.svc.switch_mode("game", dry_run=False, source="hotkey")
    assert not e2.sys.procs.forced and e2.sys.procs.handles and all(h.closed for h in e2.sys.procs.handles)


def test_force_kill_skips_process_that_cannot_be_opened(tmp_path: Path) -> None:
    e = _force_env(tmp_path)
    e.sys.procs.open_for_force = lambda _pid, _exe: None  # type: ignore[method-assign,assignment]   # 昇格プロセス等
    code, _ = e.svc.switch_mode("game", dry_run=False, source="hotkey")
    assert code == 0 and not e.ui.force_asked and not e.sys.procs.forced
    row = next(r for r in e.ops() if r["type"] == "close_app")
    assert row["result"] == "still_running"


def test_close_never_targets_game_exe(env: Env) -> None:
    env.sys.procs.add("game.exe")
    env.ctx._games = frozenset({"game.exe", "mail.exe"})   # 後からゲーム扱いになった
    cli.handle(env.svc, ["game"])
    assert not any(any(env.sys.procs.procs.get(p) == "mail.exe" for p in s) for s in env.sys.procs.posted)


# ---------------------------------------------------------------- FR-20 ゲーム中
def test_fr20_game_foreground_skips_focus_steps(env: Env) -> None:
    env.ctx.fg = FakeForeground(exe="game.exe", is_game=True, is_fullscreen=True)
    env.svc.switch_mode("game", dry_run=False, source="hotkey")
    rows = {r["type"]: r for r in env.ops()}
    for t in ("launch_app", "close_app", "open_path", "open_url"):
        assert rows[t]["result"] == "skipped" and rows[t]["reason"] == "ゲーム中"
    assert rows["power_plan"]["result"] == "ok" and env.sys.power.active == GUID_B
    assert rows["layout_apply"]["result"] == "ok"
    assert not env.sys.launcher.launched and not env.sys.opener.opened and not env.ui.previews
    assert any("ゲーム中のため 4 件をスキップ" in n[1] for n in env.ctx.notifications)


def test_fr20_unconfirmed_in_game_does_nothing(env_unconfirmed: Env) -> None:
    e = env_unconfirmed
    e.ctx.fg = FakeForeground(exe="game.exe", is_game=True)
    code, _ = e.svc.switch_mode("game", dry_run=False, source="hotkey")
    assert code == 5 and not e.ui.previews and not e.ops()


# ---------------------------------------------------------------- AC-13 busy
def test_ac13_busy_rejects_second_request(tmp_path: Path) -> None:
    e = Env(tmp_path, confirm=True, threaded=True)
    e.sys.procs.close_behavior["mail.exe"] = "stay"
    gate = threading.Event()
    e.sys.procs.gate = gate
    assert e.svc.switch_mode("game", dry_run=False, source="hotkey")[0] == 0
    deadline = time.time() + 5
    while not e.svc.busy and time.time() < deadline:
        e.ctx.pump(0.01)
    # 1回目の計画 → 実行が始まるまで回す
    while not e.sys.procs.posted and time.time() < deadline:
        e.ctx.pump(0.01)
    assert e.svc.busy
    code, _ = e.svc.switch_mode("game", dry_run=False, source="hotkey")
    assert code == 6
    assert cli.handle(e.svc, ["--undo"])[0] == 6
    assert any("受け付けませんでした" in n[1] for n in e.ctx.notifications)
    gate.set()
    while e.svc.busy and time.time() < deadline:
        e.ctx.pump(0.02)
    assert not e.svc.busy
    run_ids = {r["run_id"] for r in e.ops()}
    assert len(run_ids) == 1


# ---------------------------------------------------------------- AC-16
def test_ac16_layout_apply_payload(env: Env) -> None:
    got: list[dict] = []  # type: ignore[type-arg]
    env.ctx.on("layout.apply", lambda p: got.append(dict(p)))
    code, _ = cli.handle(env.svc, ["game"])
    assert code == 0
    run_id = env.ops()[-1]["run_id"]
    assert got == [{"layout": "game", "source": "modeshift", "request_id": f"{run_id}-8", "wait_s": 3.0}]


def test_layout_wait_zero_when_previous_step_did_not_launch(env: Env) -> None:
    got: list[dict] = []  # type: ignore[type-arg]
    env.ctx.on("layout.apply", lambda p: got.append(dict(p)))
    cli.handle(env.svc, ["game"])
    cli.handle(env.svc, ["game"])      # 2回目は起動済みでスキップ → wait_s=0
    assert got[-1]["wait_s"] == 0


# ---------------------------------------------------------------- AC-17
def test_ac17_ops_append_only_and_count(env: Env) -> None:
    cli.handle(env.svc, ["game", "--dry-run"])
    n1 = env.ops_path.stat().st_size
    h1 = _sha_prefix(env.ops_path, n1)
    cli.handle(env.svc, ["game"])
    cli.handle(env.svc, ["study", "--dry-run"])
    cli.handle(env.svc, ["--undo"])
    rows = env.ops()
    assert _sha_prefix(env.ops_path, n1) == h1
    by_run: dict[str, int] = {}
    for r in rows:
        by_run[r["run_id"]] = by_run.get(r["run_id"], 0) + 1
    counts = sorted(by_run.values())
    assert counts == sorted([8, 8, 1, 3])   # dry-run 8 / 本番 8 / study dry-run 1 / undo 3 項目
    assert all(set(r) >= {"ts", "run_id", "source", "mode", "dry_run", "step", "type", "target", "result", "reason"} for r in rows)
    text = env.ops_path.read_text(encoding="utf-8")
    assert "token=secret" not in text and "?" not in text                 # クエリを書かない
    assert "example.com/page" not in text and "https://" not in text      # 利用者の判断: ドメインのみ
    assert {r["target"] for r in rows if r["type"] == "open_url"} == {"example.com"}


def test_open_url_log_has_domain_only_but_preview_keeps_path(env: Env) -> None:
    env.svc.switch_mode("game", dry_run=True, source="tray")
    step = next(s for s in env.ui.previews[-1].steps if s.type == "open_url")
    assert step.target == "https://example.com/page?…"                    # 画面(ログではない)は従来どおり
    cli.handle(env.svc, ["game"])
    rows = [r for r in env.ops() if r["type"] == "open_url"]
    assert rows and all(r["target"] == "example.com" for r in rows)
    assert "/page" not in env.ops_path.read_text(encoding="utf-8")


def test_status_and_unknown(env: Env) -> None:
    cli.handle(env.svc, ["game"])
    code, text = cli.handle(env.svc, ["--status"])
    assert code == 0 and "現在のモード: ゲーム" in text and "元に戻す: 可" in text and "bad_type" in text
    assert cli.handle(env.svc, ["nope"])[0] == 2
    assert cli.handle(env.svc, ["bad_url"])[0] == 2
    assert cli.handle(env.svc, [])[0] == 1
    assert cli.handle(env.svc, ["a", "b"])[0] == 1
