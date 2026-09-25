# 適用前スナップショット snapshot.json(1段。FR-9 / D-7)の保存・読み込みと、「元に戻す」の計画・実行。
# 戻すのは電源プラン・音量・マイク・テーマ(値として読んで書き戻せるもの。D-5 / INV-4)。現在値が「書いた値」のままの
# 項目だけを戻し、手で変えた項目は「手動で変更済み」でスキップ(D-6)。既定のデバイスが変わっていれば音量は戻さない。
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from deskkit.modules.modeshift.actions.audio import EPS
from deskkit.modules.modeshift.actions.common import ExecEnv
from deskkit.modules.modeshift.actions.theme import state_text, theme_text
from deskkit.modules.modeshift.model import FAILED, GAME_REASON, OK, SKIPPED, Plan, Step, new_run_id, now_iso, pct
from deskkit.modules.modeshift.system import Backends, MasterState, ThemeState

log = logging.getLogger("deskkit.modeshift")
MANUAL = "手動で変更済み"


class SnapshotStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any] | None:
        """無い・壊れている → None(壊れたファイルは読まずにログだけ。次の切替で上書きする)。"""
        if not self.path.exists():
            return None
        try:
            v = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("snapshot.json を読めません(%s)。元に戻す記録は無しとして扱います", type(e).__name__)
            return None
        if not isinstance(v, dict) or "mode_to" not in v:
            log.warning("snapshot.json の形が想定外です。元に戻す記録は無しとして扱います")
            return None
        return v

    def save(self, snap: dict[str, Any]) -> None:
        """一時ファイル + os.replace で原子的に書く。失敗は OSError。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".snapshot-", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def available(self) -> dict[str, Any] | None:
        s = self.load()
        return s if s is not None and not s.get("undone_at") else None


def take_snapshot(plan: Plan, backends: Backends, mode_from: str | None) -> dict[str, Any]:
    """この Plan が変更する項目(実行予定の電源・音量)の現在値を記録する。取れない値は null。"""
    snap: dict[str, Any] = {
        "taken_at": now_iso(), "run_id": plan.run_id, "mode_from": mode_from, "mode_to": plan.mode,
        "label_to": plan.label, "power": None, "master": None, "apps": [], "mic": None, "theme": None,
    }
    planned = [s for s in plan.steps if s.planned]
    if any(s.type == "power_plan" for s in planned):
        try:
            before = backends.power.get_active()
        except Exception:  # noqa: BLE001
            before = None
        snap["power"] = {"before": before, "written": None}
    if any(s.type == "master_volume" for s in planned):
        try:
            m = backends.audio.get_master()
        except Exception:  # noqa: BLE001
            m = None
        snap["master"] = {
            "before": {"level": m.level, "mute": m.mute} if m else None, "written": None,
            "device_id": m.device_id if m else None,
        }
    if any(s.type == "mic_volume" for s in planned):
        try:
            c = backends.audio.get_capture()
        except Exception:  # noqa: BLE001
            c = None
        snap["mic"] = {
            "before": {"level": c.level, "mute": c.mute} if c else None, "written": None,
            "device_id": c.device_id if c else None,
        }
    theme_steps = [s for s in planned if s.type == "theme"]
    if theme_steps:
        try:
            th: ThemeState | None = backends.theme.get()
        except Exception:  # noqa: BLE001
            th = None
        keys = sorted({k for s in theme_steps for k in ("apps", "system") if s.params.get(k) is not None})
        snap["theme"] = {"before": {"apps": th.apps, "system": th.system} if th else None, "written": None, "keys": keys}
    app_steps = [s for s in planned if s.type == "app_volume"]
    if app_steps:
        try:
            now = {x.session_id: x for x in backends.audio.list_sessions()}
        except Exception:  # noqa: BLE001
            now = {}
        seen: set[str] = set()
        for s in app_steps:
            for x in s.params.get("sessions", []):
                sid = x["id"]
                if sid in seen:
                    continue
                seen.add(sid)
                cur = now.get(sid)
                snap["apps"].append({"exe": s.params["exe"], "pid": x["pid"], "session": sid,
                                     "before": cur.level if cur else None, "written": None})
    return snap


# ------------------------------------------------------------------ 計画
def _endpoint_text(m: MasterState | None) -> str:
    return f"{pct(m.level)}・{'ミュート中' if m.mute else 'ミュートなし'}" if m else "読めない"


def _theme_keys(th: dict[str, Any]) -> list[str]:
    """戻す対象のテーマの項目(書いた項目のうち、切替前の値が取れているもの)。"""
    before = th.get("before") or {}
    return [k for k in th.get("keys", []) if k in ("apps", "system") and before.get(k) in ("dark", "light")]


def build_undo_plan(snap: dict[str, Any], backends: Backends, *, source: str, dry_run: bool,
                    run_id: str | None = None, in_game: bool = False) -> Plan:
    steps: list[Step] = []
    pw = snap.get("power") or {}
    if pw.get("before") and pw.get("written"):
        try:
            cur = backends.power.get_active()
        except Exception:  # noqa: BLE001
            cur = None
        try:
            names = {x.guid: x.name for x in backends.power.list_schemes()}
        except Exception:  # noqa: BLE001
            names = {}

        def nm(g: str | None) -> str:
            return f"{names[g]} ({g})" if g and names.get(g) else (g or "不明")

        st = Step(0, "power_plan", "電源プラン", nm(cur), nm(pw["before"]), True,
                  params={"guid": pw["before"], "expect": pw["written"]})
        if cur is None:
            st.planned, st.reason = False, "現在値を読めない"
        elif cur != pw["written"]:
            st.planned, st.reason = False, MANUAL
        elif cur == pw["before"]:
            st.planned, st.reason = False, "変更なし"
        steps.append(st)
    ms = snap.get("master") or {}
    if ms.get("before") and ms.get("written"):
        b, wr = ms["before"], ms["written"]
        try:
            cur_m = backends.audio.get_master()
        except Exception:  # noqa: BLE001
            cur_m = None
        cur_txt = f"{pct(cur_m.level)}・{'ミュート中' if cur_m.mute else 'ミュートなし'}" if cur_m else "読めない"
        st = Step(0, "master_volume", "マスター音量", cur_txt, f"{pct(b['level'])}・{'ミュート' if b['mute'] else 'ミュートなし'}",
                  True, params={"level": b["level"], "mute": b["mute"], "expect": wr, "device_id": ms.get("device_id")})
        if cur_m is None:
            st.planned, st.reason = False, "読めない"
        elif ms.get("device_id") and cur_m.device_id != ms.get("device_id"):
            st.planned, st.reason = False, "既定の再生デバイスが変わった"
        elif abs(cur_m.level - wr["level"]) >= EPS or cur_m.mute != wr["mute"]:
            st.planned, st.reason = False, MANUAL
        steps.append(st)
    mc = snap.get("mic") or {}
    if mc.get("before") and mc.get("written"):
        b, wr = mc["before"], mc["written"]
        try:
            cur_c = backends.audio.get_capture()
        except Exception:  # noqa: BLE001
            cur_c = None
        st = Step(0, "mic_volume", "マイク", _endpoint_text(cur_c), f"{pct(b['level'])}・{'ミュート' if b['mute'] else 'ミュートなし'}",
                  True, params={"level": b["level"], "mute": b["mute"], "expect": wr, "device_id": mc.get("device_id")})
        if cur_c is None:
            st.planned, st.reason = False, "読めない"
        elif mc.get("device_id") and cur_c.device_id != mc.get("device_id"):
            st.planned, st.reason = False, "既定の録音デバイスが変わった"
        elif abs(cur_c.level - wr["level"]) >= EPS or cur_c.mute != wr["mute"]:
            st.planned, st.reason = False, MANUAL
        steps.append(st)
    th = snap.get("theme") or {}
    keys = _theme_keys(th)
    if th.get("before") and th.get("written") and keys:
        tb, twr = th["before"], th["written"]
        try:
            cur_t: ThemeState | None = backends.theme.get()
        except Exception:  # noqa: BLE001
            cur_t = None
        want = {k: tb[k] for k in keys}
        st = Step(0, "theme", "アプリのテーマ", state_text(cur_t), theme_text(want.get("apps"), want.get("system")), True,
                  params={"apps": want.get("apps"), "system": want.get("system"), "expect": {k: twr.get(k) for k in keys}})
        if in_game:
            st.planned, st.reason = False, GAME_REASON
        elif cur_t is None:
            st.planned, st.reason = False, "読めない"
        elif any(getattr(cur_t, k) != twr.get(k) for k in keys):
            st.planned, st.reason = False, MANUAL
        elif all(getattr(cur_t, k) == want[k] for k in keys):
            st.planned, st.reason = False, "変更なし"
        steps.append(st)
    apps = [a for a in snap.get("apps", []) if a.get("before") is not None and a.get("written") is not None]
    if apps:
        try:
            procs = {p.pid: p.exe for p in backends.processes.list_processes()}
            now = {x.session_id: x for x in backends.audio.list_sessions()}
        except Exception:  # noqa: BLE001
            procs, now = {}, {}
        for a in apps:
            cur_s = now.get(a["session"])
            st = Step(0, "app_volume", a["exe"], pct(cur_s.level) if cur_s else "—", pct(a["before"]), True,
                      params={"session": a["session"], "pid": a["pid"], "exe": a["exe"], "level": a["before"],
                              "expect": a["written"]})
            if procs.get(a["pid"]) != a["exe"]:
                st.planned, st.reason = False, "プロセスなし"   # 別 PID の同名アプリには書かない
            elif cur_s is None:
                st.planned, st.reason = False, "セッションなし"
            elif abs(cur_s.level - a["written"]) >= EPS:
                st.planned, st.reason = False, MANUAL
            steps.append(st)
    for i, s in enumerate(steps, 1):
        s.index = i
    return Plan(run_id=run_id or new_run_id(), kind="undo", mode=str(snap.get("mode_to")),
                label=str(snap.get("label_to") or snap.get("mode_to")), source=source, dry_run=dry_run, steps=steps,
                extra={"mode_from": snap.get("mode_from"), "snapshot_run_id": snap.get("run_id")})


# ------------------------------------------------------------------ 実行(実行時にも D-6 を確かめ直す)
def run_power(step: Step, env: ExecEnv) -> tuple[str, str]:
    cur = env.backends.power.get_active()
    if cur is None:
        return FAILED, "出力書式が想定外"
    if cur != step.params["expect"]:
        return SKIPPED, MANUAL
    ok, why = env.backends.power.set_active(step.params["guid"])
    return (OK, why) if ok else (FAILED, why)


def run_master(step: Step, env: ExecEnv) -> tuple[str, str]:
    p = step.params
    cur = env.backends.audio.get_master()
    if cur is None:
        return FAILED, "既定の再生デバイスを読めない"
    if p.get("device_id") and cur.device_id != p["device_id"]:
        return SKIPPED, "既定の再生デバイスが変わった"
    if abs(cur.level - p["expect"]["level"]) >= EPS or cur.mute != p["expect"]["mute"]:
        return SKIPPED, MANUAL
    rb = env.backends.audio.set_master(p["level"], p["mute"])
    return (OK, f"読み戻し {pct(rb.level)}") if rb else (FAILED, "設定できない")


def run_app(step: Step, env: ExecEnv) -> tuple[str, str]:
    p = step.params
    procs = {x.pid: x.exe for x in env.backends.processes.list_processes()}
    if procs.get(p["pid"]) != p["exe"]:
        return SKIPPED, "プロセスなし"
    now = {x.session_id: x for x in env.backends.audio.list_sessions()}
    cur = now.get(p["session"])
    if cur is None:
        return SKIPPED, "セッションなし"
    if abs(cur.level - p["expect"]) >= EPS:
        return SKIPPED, MANUAL
    rb = env.backends.audio.set_sessions({p["session"]: p["level"]})
    v = rb.get(p["session"])
    return (OK, f"読み戻し {pct(v)}") if v is not None else (FAILED, "設定できない")


def run_mic(step: Step, env: ExecEnv) -> tuple[str, str]:
    p = step.params
    cur = env.backends.audio.get_capture()
    if cur is None:
        return FAILED, "既定の録音デバイスを読めない"
    if p.get("device_id") and cur.device_id != p["device_id"]:
        return SKIPPED, "既定の録音デバイスが変わった"
    if abs(cur.level - p["expect"]["level"]) >= EPS or cur.mute != p["expect"]["mute"]:
        return SKIPPED, MANUAL
    rb = env.backends.audio.set_capture(p["level"], p["mute"])
    return (OK, f"読み戻し {pct(rb.level)}" + ("・ミュート" if rb.mute else "")) if rb else (FAILED, "設定できない")


def run_theme(step: Step, env: ExecEnv) -> tuple[str, str]:
    if env.in_game:
        return SKIPPED, GAME_REASON
    p = step.params
    expect: dict[str, Any] = p["expect"]
    cur = env.backends.theme.get()
    if any(getattr(cur, k) != v for k, v in expect.items()):
        return SKIPPED, MANUAL
    rb, note = env.backends.theme.set(p.get("apps"), p.get("system"))
    if rb is None or any(getattr(rb, k) != p.get(k) for k in expect):
        return FAILED, "書いた値を読み戻せない"
    return OK, "読み戻して一致を確認" + (f"({note})" if note else "")
