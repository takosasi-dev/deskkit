# ModeShift の中核。3つの入口はすべて switch_mode(name, dry_run, source) に集まり(FR-5)、同じ planner の Plan を
# プレビュー・実行の両方に使う。実行は別スレッドで行い結果はメインスレッドへ返す(UI を止めない)。実行中の別要求は
# 拒否する(FR-21)。未確認モードの確認フロー(FR-8)・ゲーム中の抑止(FR-20)・undo(FR-19)・状態(FR-22)もここ。
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from deskkit.modules.modeshift import undo
from deskkit.modules.modeshift.actions.common import ExecEnv
from deskkit.modules.modeshift.config import Config, ModeDef, validate_section
from deskkit.modules.modeshift.executor import execute
from deskkit.modules.modeshift.model import (
    FAILED,
    INTERACTIVE_SOURCES,
    STILL_RUNNING,
    Plan,
    Step,
    now_iso,
    plan_to_tsv,
)
from deskkit.modules.modeshift.oplog import OpLog
from deskkit.modules.modeshift.planner import build_plan
from deskkit.modules.modeshift.system import Backends

T = TypeVar("T")
log = logging.getLogger("deskkit.modeshift")

# §9.1 の終了コード
EXIT_OK, EXIT_ARGS, EXIT_NO_MODE, EXIT_PARTIAL, EXIT_UNCONFIRMED, EXIT_BUSY, EXIT_NO_UNDO = 0, 1, 2, 4, 5, 6, 7


class UiHooks(Protocol):
    def show_preview(self, plan: Plan) -> None: ...
    def plan_progress(self, plan: Plan, step: Step) -> None: ...
    def plan_finished(self, plan: Plan) -> None: ...
    def confirm_force(self, exe: str, pid: int) -> bool: ...   # 作業スレッドから呼ばれる(内部でメインスレッドを待つ)


class NullUi:
    def show_preview(self, plan: Plan) -> None:
        log.info("プレビュー表示先がありません run_id=%s", plan.run_id)

    def plan_progress(self, plan: Plan, step: Step) -> None:
        pass

    def plan_finished(self, plan: Plan) -> None:
        pass

    def confirm_force(self, exe: str, pid: int) -> bool:
        return False


class _Failed:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


def _run(job: Callable[[], T]) -> T | _Failed:
    try:
        return job()
    except Exception as e:  # noqa: BLE001
        log.exception("作業スレッドで例外")
        return _Failed(e)


class Worker:
    """計画作成・実行を別スレッドで走らせ、完了をメインスレッドへ返す。threaded=False ならその場で実行(テスト用)。"""

    def __init__(self, post_main: Callable[[Callable[[], None]], None], threaded: bool = True) -> None:
        self._post_main = post_main
        self.threaded = threaded
        self._threads: list[threading.Thread] = []

    def submit(self, job: Callable[[], Any], done: Callable[[Any], None]) -> None:
        if not self.threaded:
            done(_run(job))
            return

        def body() -> None:
            r = _run(job)
            self._post_main(lambda: done(r))

        t = threading.Thread(target=body, name="modeshift-worker", daemon=True)
        self._threads = [x for x in self._threads if x.is_alive()] + [t]
        t.start()

    def run_blocking(self, job: Callable[[], Any]) -> Any:
        """CLI 用: 結果を待って返す。メインスレッドならローカルのイベントループを回して UI を止めない。"""
        if not self.threaded:
            return _run(job)
        from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

        if QCoreApplication.instance() is None or threading.current_thread() is not threading.main_thread():
            return _run(job)
        box: dict[str, Any] = {}
        t = threading.Thread(target=lambda: box.__setitem__("r", _run(job)), name="modeshift-worker", daemon=True)
        self._threads = [x for x in self._threads if x.is_alive()] + [t]
        t.start()
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(30)
        timer.timeout.connect(lambda: None if t.is_alive() else loop.quit())
        timer.start()
        if t.is_alive():
            loop.exec()
        timer.stop()
        t.join()
        return box.get("r")

    def join(self, timeout_s: float) -> None:
        for t in list(self._threads):
            t.join(timeout_s)


class StateStore:
    """state.json: 現在のモード・最後の切替時刻(FR-22 の表示用)。"""

    def __init__(self, path: Any) -> None:
        self.path = path
        self.data: dict[str, Any] = {"current_mode": None, "last_switch_at": None, "last_run_id": None}
        try:
            v = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(v, dict):
                self.data.update(v)
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=str(self.path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("state.json を保存できません: %s", e)


class ModeShiftService:
    def __init__(self, ctx: Any, backends: Backends, ui: UiHooks | None = None, *, threaded: bool = True) -> None:
        self.ctx = ctx
        self.backends = backends
        self.ui: UiHooks = ui or NullUi()
        self.oplog = OpLog(ctx.data_dir / "ops.jsonl")
        self.snapshots = undo.SnapshotStore(ctx.data_dir / "snapshot.json")
        self.state = StateStore(ctx.data_dir / "state.json")
        self.worker = Worker(self._post_main, threaded)
        self._abort = threading.Event()
        self._busy = False
        self._busy_lock = threading.Lock()
        self._listeners: list[Callable[[], None]] = []
        self.running_plan: Plan | None = None
        self.last_result = "none"   # 診断用: 直前の実行の結果コード(none / ok / partial / error)
        self.config: Config = validate_section(ctx.settings_dict(), ctx.game_processes())

    # ---------------------------------------------------------------- 基本
    def _post_main(self, fn: Callable[[], None]) -> None:
        if threading.current_thread() is threading.main_thread():
            fn()
        else:
            self.ctx.call_soon(fn)

    def add_listener(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[], None]) -> None:
        self._listeners = [f for f in self._listeners if f is not fn and f != fn]

    def changed(self) -> None:
        """状態・設定が変わった(画面・トレイの更新はメインスレッドで)。"""
        self._post_main(self._notify_listeners)

    def _notify_listeners(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except RuntimeError:          # 破棄済みの画面
                self.remove_listener(fn)
            except Exception:  # noqa: BLE001
                log.exception("listener で例外")

    def reload(self) -> None:
        self.config = validate_section(self.ctx.settings_dict(), self.ctx.game_processes())
        self.changed()

    def shutdown(self) -> None:
        """host 終了・モジュール停止: 実行中の Step の完了は待たずに中断を要求する。"""
        self._abort.set()
        self.worker.join(2.0)

    @property
    def aborting(self) -> bool:
        return self._abort.is_set()

    @property
    def busy(self) -> bool:
        return self._busy

    def _try_busy(self) -> bool:
        with self._busy_lock:
            if self._busy:
                return False
            self._busy = True
        self.changed()
        return True

    def _release(self) -> None:
        with self._busy_lock:
            self._busy = False
        self.running_plan = None
        self.changed()

    def _notify(self, text: str, level: str = "info", title: str = "ModeShift") -> None:
        try:
            self.ctx.notify(title, text, level=level)
        except Exception:  # noqa: BLE001
            log.exception("通知に失敗")

    def _busy_reply(self) -> tuple[int, str]:
        self._notify("切り替えの実行中のため、新しい要求は受け付けませんでした", "warn")
        return EXIT_BUSY, "別の切替が実行中です"

    def in_game(self) -> bool:
        """foreground がゲーム、または全画面(判定不能も含む)なら True(FR-20 / C-4)。"""
        try:
            fg = self.ctx.foreground()
        except Exception:  # noqa: BLE001 - 読めないときは作用しない側に倒す
            log.exception("foreground を読めません")
            return True
        return bool(fg.is_game or fg.is_fullscreen is not False)

    def current_mode(self) -> ModeDef | None:
        name = self.state.data.get("current_mode")
        return self.config.mode(name) if isinstance(name, str) else None

    def undo_info(self) -> dict[str, Any] | None:
        return self.snapshots.available()

    # ---------------------------------------------------------------- 計画
    def make_plan(self, mode: ModeDef, *, source: str, dry_run: bool, in_game: bool) -> Plan:
        return build_plan(mode, self.backends, game_processes=self.ctx.game_processes(), in_game=in_game,
                          source=source, dry_run=dry_run)

    # ---------------------------------------------------------------- 入口(FR-5)
    def switch_mode(self, name: str, *, dry_run: bool, source: str) -> tuple[int, str]:
        if self._busy:
            return self._busy_reply()
        mode = self.config.mode(name)
        if mode is None:
            return EXIT_NO_MODE, f"モード '{name}' はありません"
        if not mode.valid:
            return EXIT_NO_MODE, f"モード '{name}' は無効です: {mode.reason()}"
        in_game = self.in_game()
        if source in INTERACTIVE_SOURCES:
            return self._switch_interactive(mode, dry_run, source, in_game)
        return self._switch_batch(mode, dry_run, source, in_game)

    def _switch_interactive(self, mode: ModeDef, dry_run: bool, source: str, in_game: bool) -> tuple[int, str]:
        confirmed = mode.confirmed
        want_preview = dry_run or not confirmed or self.config.preview == "always"
        if want_preview and in_game:
            if dry_run or not confirmed:
                msg = "ゲーム中のためプレビューを表示しませんでした"
                if not confirmed and not dry_run:
                    msg += "。未確認のモードは確認が済むまで実行しません"
                log.info("%s mode=%s source=%s", msg, mode.name, source)
                self._notify(msg, "warn")
                return (EXIT_UNCONFIRMED if not dry_run else EXIT_OK), msg
            log.info("ゲーム中のためプレビューを省いて実行 mode=%s", mode.name)
            want_preview = False
        if not self._try_busy():
            return self._busy_reply()

        def done(res: Any) -> None:
            if isinstance(res, _Failed):
                self._release()
                self._notify(f"計画を作れませんでした({type(res.exc).__name__})", "error")
                return
            plan: Plan = res
            if want_preview:
                self._release()
                if dry_run:
                    self._log_dry(plan)
                self.ui.show_preview(plan)
            else:
                self._start_execution(plan)

        self.worker.submit(lambda: self.make_plan(mode, source=source, dry_run=dry_run or want_preview, in_game=in_game), done)
        return EXIT_OK, "受け付けました"

    def _switch_batch(self, mode: ModeDef, dry_run: bool, source: str, in_game: bool) -> tuple[int, str]:
        if dry_run:
            if not self._try_busy():
                return self._busy_reply()
            try:
                res = self.worker.run_blocking(lambda: self.make_plan(mode, source=source, dry_run=True, in_game=in_game))
            finally:
                self._release()
            if isinstance(res, _Failed):
                return EXIT_ARGS, f"計画を作れませんでした({type(res.exc).__name__})"
            self._log_dry(res)
            return EXIT_OK, plan_to_tsv(res)
        if not mode.confirmed:
            msg = f"モード「{mode.label}」は未確認のため実行しません(トレイかホットキーから実行し、プレビューで確認してください)"
            if source == "auto":
                self._notify(msg, "warn")
            return EXIT_UNCONFIRMED, msg
        if not self._try_busy():
            return self._busy_reply()
        if source == "auto":
            def planned(res: Any) -> None:
                if isinstance(res, _Failed):
                    self._release()
                    self._notify(f"計画を作れませんでした({type(res.exc).__name__})", "error")
                    return
                self._start_execution(res)

            self.worker.submit(lambda: self.make_plan(mode, source=source, dry_run=False, in_game=in_game), planned)
            return EXIT_OK, "受け付けました"
        # CLI: 結果の終了コードを返すため待つ(待つ間もイベントループは回す)
        try:
            res = self.worker.run_blocking(lambda: self.make_plan(mode, source=source, dry_run=False, in_game=in_game))
            if isinstance(res, _Failed):
                self._release()
                return EXIT_ARGS, f"計画を作れませんでした({type(res.exc).__name__})"
            plan: Plan = res
            return self._execute_blocking(plan)
        except Exception:
            self._release()
            raise

    # ---------------------------------------------------------------- undo(FR-19)
    def request_undo(self, *, dry_run: bool, source: str) -> tuple[int, str]:
        if self._busy:
            return self._busy_reply()
        snap = self.snapshots.available()
        if snap is None:
            return EXIT_NO_UNDO, "元に戻せる記録がありません"
        interactive = source in INTERACTIVE_SOURCES
        in_game = self.in_game()
        want_preview = interactive and (dry_run or self.config.preview == "always")
        if want_preview and in_game:
            if dry_run:
                self._notify("ゲーム中のためプレビューを表示しませんでした", "warn")
                return EXIT_OK, "ゲーム中のためプレビューを表示しませんでした"
            want_preview = False
        if not self._try_busy():
            return self._busy_reply()

        def job() -> Plan:
            return undo.build_undo_plan(snap, self.backends, source=source, dry_run=dry_run or want_preview,
                                        in_game=in_game)

        if interactive or source == "auto":
            def done(res: Any) -> None:
                if isinstance(res, _Failed):
                    self._release()
                    self._notify(f"元に戻す計画を作れませんでした({type(res.exc).__name__})", "error")
                    return
                if want_preview:
                    self._release()
                    if dry_run:
                        self._log_dry(res)
                    self.ui.show_preview(res)
                else:
                    self._start_execution(res)

            self.worker.submit(job, done)
            return EXIT_OK, "受け付けました"
        res = self.worker.run_blocking(job)
        if isinstance(res, _Failed):
            self._release()
            return EXIT_ARGS, f"元に戻す計画を作れませんでした({type(res.exc).__name__})"
        if dry_run:
            self._release()
            self._log_dry(res)
            return EXIT_OK, plan_to_tsv(res)
        return self._execute_blocking(res)

    # ---------------------------------------------------------------- プレビューの「実行」(FR-8)
    def execute_from_preview(self, plan: Plan) -> tuple[int, str]:
        if plan.executed:
            return EXIT_ARGS, "この計画は実行済みです"
        if plan.kind == "switch":
            mode = self.config.mode(plan.mode)
            if mode is None or not mode.valid or mode.def_hash != plan.def_hash:
                self._notify("プレビューの後にモードの定義が変わりました。プレビューし直してください", "warn")
                return EXIT_NO_MODE, "定義が変わりました"
        else:
            snap = self.snapshots.available()
            if snap is None or snap.get("run_id") != plan.extra.get("snapshot_run_id"):
                self._notify("元に戻す記録が変わりました。プレビューし直してください", "warn")
                return EXIT_NO_UNDO, "記録が変わりました"
        if not self._try_busy():
            return self._busy_reply()
        if plan.kind == "switch" and plan.needs_confirmation:
            self._confirm(plan)
        self._start_execution(plan)
        return EXIT_OK, "実行を開始しました"

    def _confirm(self, plan: Plan) -> None:
        """利用者が「実行」を押した → confirmed_hash を今の定義ハッシュにする。"""
        try:
            sec = self.ctx.settings_dict()
            for m in sec.get("modes", []):
                if isinstance(m, dict) and m.get("name") == plan.mode:
                    m["confirmed_hash"] = plan.def_hash
            self.ctx.write_settings(sec)
            self.config = validate_section(self.ctx.settings_dict(), self.ctx.game_processes())
        except Exception as e:  # noqa: BLE001 - 保存できなくても今回の実行は続ける
            log.exception("confirmed_hash を保存できません")
            self._notify(f"確認済みの記録を保存できませんでした({type(e).__name__})", "warn")

    # ---------------------------------------------------------------- 実行
    def _make_env(self, plan: Plan) -> ExecEnv:
        in_game = self.in_game()
        allow_force = self.config.allow_force_kill and plan.source in INTERACTIVE_SOURCES and not in_game
        return ExecEnv(
            backends=self.backends, game_processes=frozenset(self.ctx.game_processes()), in_game=in_game,
            abort=self._abort, emit=self._emit_main, run_id=plan.run_id,
            confirm_force=self.ui.confirm_force if allow_force else None,
        )

    def _emit_main(self, event: str, payload: dict[str, Any]) -> None:
        self._post_main(lambda: self.ctx.emit(event, payload))

    def _exec_job(self, plan: Plan, env: ExecEnv) -> Callable[[], Plan]:
        mode_from = self.state.data.get("current_mode")

        def on_step(step: Step) -> None:
            self._post_main(lambda: self.ui.plan_progress(plan, step))

        def job() -> Plan:
            return execute(plan, env, self.oplog, self.snapshots if plan.kind == "switch" else None,
                           mode_from=mode_from, on_step=on_step)
        return job

    def _start_execution(self, plan: Plan) -> None:
        """busy を持った状態で呼ぶ。完了時に busy を放す。"""
        self.running_plan = plan
        env = self._make_env(plan)
        self.worker.submit(self._exec_job(plan, env), lambda res: self._finish(plan, res))

    def _execute_blocking(self, plan: Plan) -> tuple[int, str]:
        self.running_plan = plan
        env = self._make_env(plan)
        res = self.worker.run_blocking(self._exec_job(plan, env))
        self._finish(plan, res)
        if isinstance(res, _Failed):
            return EXIT_PARTIAL, f"実行中に例外({type(res.exc).__name__})"
        return plan.exit_code(), plan_to_tsv(plan, with_results=True)

    def _finish(self, plan: Plan, res: Any) -> None:
        try:
            if isinstance(res, _Failed):
                self.last_result = "error"
                self._notify(f"実行中に例外が起きました({type(res.exc).__name__})", "error")
                return
            self.last_result = "partial" if plan.exit_code() else "ok"
            c = plan.counts()
            if plan.kind == "switch":
                self.state.data.update(current_mode=plan.mode, last_switch_at=now_iso(), last_run_id=plan.run_id)
                self.state.save()
                try:
                    self.ctx.emit("modeshift.switched", {"mode": plan.mode, "run_id": plan.run_id, "failed": c[FAILED]})
                except Exception:  # noqa: BLE001
                    log.exception("modeshift.switched を送れません")
                head = f"「{plan.label}」に切り替えました"
            else:
                snap = self.snapshots.load()
                if snap is not None and snap.get("run_id") == plan.extra.get("snapshot_run_id"):
                    snap["undone_at"] = now_iso()
                    snap["undo_run_id"] = plan.run_id
                    try:
                        self.snapshots.save(snap)
                    except OSError as e:
                        log.error("snapshot.json を更新できません: %s", e)
                self.state.data.update(current_mode=plan.extra.get("mode_from"), last_switch_at=now_iso(),
                                       last_run_id=plan.run_id)
                self.state.save()
                # 契約 §2: 元に戻す(手動・自動切替の on_exit・CLI)が終わった。mode は戻す前のモード名
                try:
                    self.ctx.emit("modeshift.reverted", {"mode": plan.mode, "run_id": plan.run_id})
                except Exception:  # noqa: BLE001
                    log.exception("modeshift.reverted を送れません")
                head = f"「{plan.label}」の適用前へ戻しました" if plan.steps else "戻す項目はありませんでした"
            self._notify(self._summary(plan, head), "warn" if plan.exit_code() else "ok")
        finally:
            self._release()
            try:
                self.ui.plan_finished(plan)
            except Exception:  # noqa: BLE001
                log.exception("plan_finished で例外")

    @staticmethod
    def _summary(plan: Plan, head: str) -> str:
        c = plan.counts()
        parts = []
        if c[FAILED]:
            parts.append(f"失敗 {c[FAILED]}")
        if c[STILL_RUNNING]:
            exes = ", ".join(s.target for s in plan.steps if s.result == STILL_RUNNING)
            parts.append(f"終了しなかったアプリ {c[STILL_RUNNING]}({exes}。トレイに残っている可能性)")
        if c["skipped"]:
            parts.append(f"スキップ {c['skipped']}")
        text = head + ("(" + " / ".join(parts) + ")" if parts else "")
        g = plan.game_skipped()
        if g:
            text += f"\nゲーム中のため {g} 件をスキップ"
        return text

    def _log_dry(self, plan: Plan) -> None:
        try:
            self.oplog.append_dry_run(plan)
        except OSError as e:
            log.error("ops.jsonl に書けません: %s", e)

    # ---------------------------------------------------------------- 一覧・状態(FR-22)
    def list_text(self) -> str:
        lines = ["名前\t表示名\t状態\tホットキー\t理由"]
        for m in self.config.modes:
            st = "無効" if not m.valid else ("確認済み" if m.confirmed else "未確認")
            reason = m.reason() if not m.valid else " / ".join(m.warnings)
            lines.append("\t".join([m.name, m.label, st, m.hotkey or "-", reason]))
        if not self.config.modes:
            lines.append("(モードがありません)")
        return "\n".join(lines)

    def status_text(self) -> str:
        cur = self.state.data.get("current_mode")
        m = self.config.mode(cur) if isinstance(cur, str) else None
        snap = self.snapshots.available()
        lines = [
            f"現在のモード: {m.label + ' (' + m.name + ')' if m else (cur or 'なし')}",
            f"最後の切替: {self.state.data.get('last_switch_at') or 'なし'}",
            f"元に戻す: {'可(「' + str(snap.get('label_to')) + '」適用前へ)' if snap else '不可'}",
            f"実行中: {'はい' if self._busy else 'いいえ'}",
        ]
        bad = self.config.invalid_modes()
        if bad:
            lines.append("無効なモード:")
            lines += [f"  {x.name}: {x.reason()}" for x in bad]
        if self.config.auto_error and self.config.auto_enabled:
            lines.append(f"自動切替: {self.config.auto_error}")
        return "\n".join(lines)
