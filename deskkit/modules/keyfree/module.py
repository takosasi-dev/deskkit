# KeyFree モジュール本体。一覧(scan.Scanner)・押して確かめる(trykey.TryKey)・CLI(keyfree scan / check)・トレイ・利用状況・診断を束ねる。
# 利用者が頼んだときだけ試す(INV-5: start() とタイマーでは試さない)。結果はメモリだけ(K-8)。
# ログ・ops.jsonl・diagnostics・usage には件数と所要時間だけ(組み合わせ・キーの名前・エラー番号は書かない。INV-4)。
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Signal

from deskkit.modules.keyfree import combos, scan, trykey
from deskkit.modules.keyfree._win32 import KeyboardApi
from deskkit.modules.keyfree.combos import Combo
from deskkit.modules.keyfree.oplog import OpLog
from deskkit.usage import UsageSeries, count_jsonl

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

DEFAULTS: dict[str, Any] = {
    "include_win": False,
    "batch_size": 32,
    "try_seconds": 10,
    "recommend_count": 8,
    "recommend_order": list(combos.DEFAULT_ORDER),
}
RANGES = {"batch_size": (8, 128), "try_seconds": (5, 30), "recommend_count": (1, 20)}
CLI_CHECK_TIMEOUT_S = 10.0
CLI_SCAN_TIMEOUT_S = 45.0     # IPC の相手は 60 秒で待つのをやめるので、それより短く
TRAY_LABEL = "空いているショートカットを探す"

# CLI の終了コード(FR-14)
EXIT_FREE, EXIT_SYNTAX, EXIT_USED, EXIT_DESKKIT, EXIT_UNAVAILABLE, EXIT_CANCELLED, EXIT_BUSY = 0, 1, 20, 21, 22, 23, 24

TEXT_BLOCKED = "ゲームや全画面の画面が前にあるので止めました"
TEXT_BUSY = "ほかの調べ物が終わるまで待ってください"
TEXT_UNSUPPORTED = "この DeskKit では使えません。DeskKit を新しくしてください"
TEXT_RELEASE_FAILED = "キーを返せませんでした。DeskKit を終了して起動し直してください"
TEXT_NO_MODS = "Ctrl・Alt・Shift・Windows キーのどれかを入れてください"
TEXT_BAD_SYNTAX = "キーの書き方が分かりません(例: Ctrl+Alt+K)"
TEXT_CANCELLED = "途中で止めました"
TEXT_FAILED = "調べる途中で問題が起きました。もう一度調べてください"

# use_for_quick_action の戻り値(v0.4.1)
QUICK_HOLDER = "host.quick"
QUICK_OK, QUICK_CONFLICT, QUICK_ERROR, QUICK_UNSUPPORTED = "quick_ok", "quick_conflict", "quick_error", "quick_unsupported"


def normalize(section: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """型・範囲の合わない値を既定値に戻す。(設定, 変えたか)。"""
    out = {k: v for k, v in section.items() if k != "enabled"}
    changed = False
    for k, v in DEFAULTS.items():
        cur = out.get(k)
        if isinstance(v, bool):
            ok = isinstance(cur, bool)
        elif isinstance(v, int):
            ok = isinstance(cur, int) and not isinstance(cur, bool)
        else:
            ok = isinstance(cur, list)
        if not ok:
            out[k] = list(v) if isinstance(v, list) else v
            changed = True
    for k, (lo, hi) in RANGES.items():
        n = max(lo, min(hi, int(out[k])))
        if n != out[k]:
            out[k] = n
            changed = True
    order = combos.normalize_order(out["recommend_order"])
    if order != out["recommend_order"]:
        out["recommend_order"] = order
        changed = True
    return out, changed


def state_text(state: str, error: int = 0) -> str:
    return {scan.FREE: "空き", scan.USED: "使用中", scan.DESKKIT: "DeskKit",
            scan.ERROR: f"調べられない(エラー {error})" if error else "調べられない",
            scan.RESERVED: "調べられない(Windows 用)"}.get(state, "調べられない")


class Notifier(QObject):
    changed = Signal()


class KeyFreeModule:
    def __init__(self, ctx: Any, *, kb: KeyboardApi | None = None, mono: Callable[[], float] = time.monotonic,
                 wall: Callable[[], datetime] = datetime.now) -> None:
        self.ctx = ctx
        self.log: logging.Logger = ctx.log
        section = dict(ctx.settings_dict())
        self.cfg, changed = normalize(section)
        if changed:
            try:
                ctx.write_settings(self.cfg)
            except Exception as e:  # noqa: BLE001 - 書き戻せなくても既定値で動く
                self.log.warning("既定値の書き戻しに失敗: %s", type(e).__name__)
        if kb is None:
            from deskkit.modules.keyfree._win32 import RealKeyboard

            kb = RealKeyboard()
        self.kb: KeyboardApi = kb
        self._mono = mono
        self._wall = wall
        self.notifier = Notifier()
        self.ops = OpLog(ctx.data_dir / "ops.jsonl", lambda: self._wall(), self.log)
        self.scanner = scan.Scanner(ctx, kb, on_change=self._changed, on_finished=self._on_scan_finished, log=self.log,
                                    mono=mono, wall=wall)
        self.trykey = trykey.TryKey(ctx, on_change=self._changed, log=self.log, mono=mono)
        self.notice: tuple[str, str] | None = None     # 直近の「始められなかった」わけ(画面の知らせ)
        self.last_full: scan.ScanResult | None = None  # 最後に終わった一覧(診断用)
        self._stopping = False

    # ================================================================ ライフサイクル
    def start(self) -> None:
        self._stopping = False
        self.ops.prune()
        self.ctx.add_tray_action(TRAY_LABEL, self.ctx.show_page)   # FR-15(トレイ項目はクイックアクションにも自動で入る)
        self._update_status()
        self.log.info("keyfree started probe_supported=%s", self.scanner.supported())

    def stop(self) -> None:
        """FR-16: 一覧の途中なら cancel、押して確かめるの途中なら unregister してから戻る。"""
        self._stopping = True
        self.scanner.cancel("stop")
        self.trykey.stop()
        self.log.info("keyfree stopped")

    def create_page(self) -> QWidget:
        from deskkit.modules.keyfree.page import KeyFreePage

        return KeyFreePage(self)

    def _changed(self) -> None:
        self.notifier.changed.emit()

    # ================================================================ 設定
    def set_option(self, key: str, value: Any) -> str | None:
        cfg, _ = normalize({**self.cfg, key: value})
        try:
            self.ctx.write_settings(cfg)
        except Exception as e:  # noqa: BLE001
            return f"設定を保存できませんでした({type(e).__name__})"
        self.cfg = cfg
        self._changed()
        return None

    # ================================================================ 一覧(FR-1〜FR-8)
    def supported(self) -> bool:
        return self.scanner.supported()

    def begin_scan(self, *, include_win: bool | None = None) -> str:
        """「始める」。押して確かめるの途中なら先に返す(§10)。戻り値は scan.STARTED か断ったわけ。"""
        if self.scanner.running:
            return scan.RUNNING                     # 連打は無視(§10)
        self.trykey.stop()
        self.notice = None
        inc = bool(self.cfg["include_win"]) if include_win is None else include_win
        r = self.scanner.start(inc, int(self.cfg["batch_size"]))
        if r == scan.BLOCKED:
            self.notice = ("warn", TEXT_BLOCKED)
            self.log.info("scan blocked: foreground")
        elif r == scan.UNSUPPORTED:
            self.notice = ("error", TEXT_UNSUPPORTED)
        elif r == scan.RUNNING:
            self.notice = ("warn", TEXT_BUSY)
        elif r == scan.FAILED:
            self.notice = ("error", TEXT_FAILED)
        self._update_status()
        self._changed()
        return r

    def cancel_scan(self) -> None:
        self.scanner.cancel("user")

    def _on_scan_finished(self, res: scan.ScanResult) -> None:
        self.last_full = res
        n = res.counts()
        self.ops.write("scan", tried=n["tried"], free=n["free"], used=n["used"], deskkit=n["deskkit"],
                       unavailable=n["unavailable"], pressed=n["pressed"], ms=res.ms)
        self._update_status()

    def result(self) -> scan.ScanResult | None:
        return self.scanner.result

    def recommendations(self, count: int | None = None) -> list[Combo]:
        res = self.scanner.result
        if res is None or res.running:
            return []
        n = int(self.cfg["recommend_count"]) if count is None else count
        return combos.recommend(res.state, self.cfg["recommend_order"], n)

    # ================================================================ 1つだけ(FR-5・FR-9)
    def parse_text(self, text: str) -> tuple[Combo | None, str | None]:
        """(組, 断るわけ)。修飾キーが無い書き方と、読めない書き方を分けて返す。"""
        t = text.strip()
        hk = self.ctx.hotkeys
        parsed = hk.parse(t) if t else None
        if parsed is not None:
            return (int(parsed[0]) & combos.MOD_MASK, int(parsed[1])), None
        if t and "+" not in t and hk.parse("Ctrl+" + t) is not None:
            return None, TEXT_NO_MODS
        return None, TEXT_BAD_SYNTAX

    def check(self, combo: Combo, on_result: Callable[[scan.SingleResult], None]) -> str:
        if self.trykey.active is not None:
            self.trykey.stop()
        r = self.scanner.check_one(combo[0], combo[1], self._wrap_single(on_result))
        if r == scan.BLOCKED:
            self.log.info("check blocked: foreground")
        return r

    def _wrap_single(self, on_result: Callable[[scan.SingleResult], None]) -> Callable[[scan.SingleResult], None]:
        def done(r: scan.SingleResult) -> None:
            self.ops.write("check", pressed=len(r.pressed))
            self._update_status()
            on_result(r)

        return done

    # ================================================================ 押して確かめる(FR-12)
    def try_key(self, combo: Combo) -> str | None:
        if self.scanner.running or self.scanner.single_running:
            return scan.RUNNING
        r = self.trykey.start(combo, int(self.cfg["try_seconds"]))
        if r == trykey.CONFLICT:
            self.scanner.mark_used(combo)           # §10: 直前にほかのアプリが取った
        return r

    # ================================================================ 表記
    def format(self, combo: Combo) -> str:
        return str(self.ctx.hotkeys.format(combo[0], combo[1]))

    def note_copy(self) -> None:
        self.ops.write("copy")

    # ================================================================ クイックアクションのキーにする(v0.4.1、仕様書 Q-4 の見直し)
    def use_for_quick_action(self, combo: Combo) -> str:
        """空きの組を DeskKit のクイックアクションのキーにする。戻り値は QUICK_* のどれか。
        押して確かめるの途中なら先に返す(同じ組を預かっていると登録できないため)。"""
        fn = getattr(self.ctx, "set_quick_action_hotkey", None)
        if not callable(fn):
            return QUICK_UNSUPPORTED
        if self.scanner.running or self.scanner.single_running:
            return scan.RUNNING
        self.trykey.stop()
        try:
            ok = bool(fn(self.format(combo), revert_on_fail=True))   # 取れなければ前のキーのまま(使えていたキーを失わない)
        except Exception as e:  # noqa: BLE001 - 設定を書けないときなど。画面で知らせる
            self.log.warning("quick action key failed: %s", type(e).__name__)
            return QUICK_ERROR
        self.ops.write("quick_key", ok=ok)
        if ok:
            self.scanner.mark_holder(combo, QUICK_HOLDER)
        else:
            self.scanner.mark_used(combo)            # 直前にほかのアプリが取った
        self._update_status()
        self._changed()
        return QUICK_OK if ok else QUICK_CONFLICT

    def snapshot(self) -> Any | None:
        fn = getattr(getattr(self.ctx, "hotkeys", None), "snapshot", None)
        if not callable(fn):
            return None
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            self.log.warning("snapshot failed: %s", type(e).__name__)
            return None

    # ================================================================ CLI(FR-14)
    def handle_cli(self, args: list[str]) -> tuple[int, str]:
        if args[:1] == ["check"] and len(args) >= 2:
            return self._cli_check(" ".join(args[1:]))
        if args == ["scan"]:
            return self._cli_scan()
        return 2, "使い方: keyfree scan / keyfree check <組み合わせ>(例: keyfree check Ctrl+Alt+K)"

    def _wait(self, done: Callable[[], bool], timeout_s: float) -> bool:
        """GUI スレッドを塞がずに待つ(handle_cli は GUI スレッドで呼ばれ、on_done はイベントで届く)。"""
        if done():
            return True
        from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

        if QCoreApplication.instance() is None:
            return done()
        end = self._mono() + timeout_s
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(20)

        def check() -> None:
            if done() or self._stopping or self._mono() >= end:
                loop.quit()

        timer.timeout.connect(check)
        timer.start()
        loop.exec()
        timer.stop()
        return done()

    def _cli_check(self, text: str) -> tuple[int, str]:
        combo, why = self.parse_text(text)
        if combo is None:
            return EXIT_SYNTAX, why or TEXT_BAD_SYNTAX
        box: list[scan.SingleResult] = []
        r = self.check(combo, box.append)
        refused = {scan.UNSUPPORTED: (EXIT_UNAVAILABLE, TEXT_UNSUPPORTED), scan.RUNNING: (EXIT_BUSY, TEXT_BUSY),
                   scan.BLOCKED: (EXIT_CANCELLED, TEXT_BLOCKED), scan.FAILED: (EXIT_UNAVAILABLE, TEXT_FAILED)}
        if r in refused:
            return refused[r]
        if not self._wait(lambda: bool(box), CLI_CHECK_TIMEOUT_S):
            self.scanner.cancel("timeout")
            return EXIT_CANCELLED, "時間内に終わらなかったので止めました"
        res = box[0]
        special = {"busy": (EXIT_BUSY, TEXT_BUSY), "cancelled": (EXIT_CANCELLED, TEXT_CANCELLED),
                   "release_failed": (EXIT_UNAVAILABLE, TEXT_RELEASE_FAILED)}
        if res.state in special:
            return special[res.state]
        codes = {scan.FREE: EXIT_FREE, scan.USED: EXIT_USED, scan.DESKKIT: EXIT_DESKKIT}
        out = f"{self.format(combo)}: {state_text(res.state, res.error)}"
        if res.state == scan.DESKKIT and res.holder:
            out += f"({res.holder})"
        return codes.get(res.state, EXIT_UNAVAILABLE), out

    def _cli_scan(self) -> tuple[int, str]:
        r = self.begin_scan(include_win=False)    # CLI は既定の範囲(K-4 の4組)
        refused = {scan.UNSUPPORTED: (EXIT_UNAVAILABLE, TEXT_UNSUPPORTED), scan.RUNNING: (EXIT_BUSY, TEXT_BUSY),
                   scan.BLOCKED: (EXIT_CANCELLED, TEXT_BLOCKED), scan.FAILED: (EXIT_UNAVAILABLE, TEXT_FAILED)}
        if r in refused:
            return refused[r]
        if not self._wait(lambda: not self.scanner.running, CLI_SCAN_TIMEOUT_S):
            self.scanner.cancel("timeout")
            self._wait(lambda: not self.scanner.running, 3.0)
            return EXIT_CANCELLED, "時間内に終わらなかったので止めました"
        res = self.scanner.result
        if self.scanner.last_busy:
            return EXIT_BUSY, TEXT_BUSY
        if res is None or res.reason == "cancelled":
            fg = res is not None and res.stop_why == "foreground"
            return EXIT_CANCELLED, TEXT_BLOCKED if fg else TEXT_CANCELLED
        if res.reason == "release_failed":
            return EXIT_UNAVAILABLE, TEXT_RELEASE_FAILED
        if res.reason != "finished":
            return EXIT_UNAVAILABLE, TEXT_FAILED
        free = [c for c in res.plan.combos() if res.state(c) == scan.FREE and not combos.windows_mark(*c)]
        return EXIT_FREE, "\n".join(self.format(c) for c in free)

    # ================================================================ 状態・利用状況・診断
    def minutes_ago(self) -> int | None:
        res = self.scanner.result
        if res is None or res.finished_wall is None:
            return None
        return max(0, int((self._wall() - res.finished_wall).total_seconds() // 60))

    def _update_status(self) -> None:
        res = self.scanner.result
        if not self.scanner.supported():
            text = "この DeskKit では使えません"
        elif res is None:
            text = "まだ調べていません"
        elif res.running:
            text = "調べています"
        else:
            text = f"空き {res.counts()['free']} 組"
        try:
            self.ctx.set_tray_status(text)
        except Exception as e:  # noqa: BLE001 - 状態表示の失敗で止めない
            self.log.warning("status update failed: %s", type(e).__name__)

    def usage(self, days: int) -> list[UsageSeries]:
        path = self.ops.path
        scans = count_jsonl(path, days, lambda r: r.get("event") in ("scan", "check"))
        copies = count_jsonl(path, days, lambda r: r.get("event") == "copy")
        return [
            UsageSeries("scans", "調べた回数", scans, "回", primary=True,
                        hint="v0.4.0 の公開から 90 日で 0 回なら README の紹介から外す(R-3)"),
            UsageSeries("copies", "コピーした回数", copies, "回", good_when="neutral"),
        ]

    def diagnostics(self) -> dict[str, str | int | bool]:
        """FR-20: 直近の一覧の件数と所要時間(ms)だけ。組み合わせ・キーの名前・エラー番号は入れない(INV-4)。"""
        res = self.last_full
        zero = {"tried": 0, "free": 0, "used": 0, "deskkit": 0, "unavailable": 0, "pressed": 0}
        n = res.counts() if res is not None else zero
        return {"tried": n["tried"], "free": n["free"], "used": n["used"], "deskkit": n["deskkit"],
                "unavailable": n["unavailable"], "pressed": n["pressed"], "ms": res.ms if res is not None else 0}
