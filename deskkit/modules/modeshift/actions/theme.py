# theme(v0.2): アプリ(と任意で Windows)のダーク/ライトを HKCU の Personalize キーで切り替え、
# WM_SETTINGCHANGE("ImmersiveColorSet")を SMTO_ABORTIFHUNG つきで全ウィンドウへ送る。HKCU だけなので管理者権限は不要。
# 書いた直後に読み戻した値を snapshot の「書いた値」に記録する(元に戻す用)。ゲーム中は送らない(C-4)。
from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes as w
from typing import Any

from deskkit.modules.modeshift.actions.common import ExecEnv, PlanEnv
from deskkit.modules.modeshift.model import FAILED, GAME_REASON, OK, SKIPPED, Step
from deskkit.modules.modeshift.system import ThemeState

log = logging.getLogger("deskkit.modeshift")

PERSONALIZE_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
VALUE_APPS = "AppsUseLightTheme"       # 1 = ライト / 0 = ダーク
VALUE_SYSTEM = "SystemUsesLightTheme"  # タスクバー・スタートなど
THEMES = ("dark", "light")
THEME_LABELS = {"dark": "ダーク", "light": "ライト"}

# winuser.h
HWND_BROADCAST = 0xFFFF
WM_SETTINGCHANGE = 0x001A
SMTO_ABORTIFHUNG = 0x0002
BROADCAST_TIMEOUT_MS = 1000   # ウィンドウ1つあたり(応答しないウィンドウは SMTO_ABORTIFHUNG で待たない)


def theme_text(apps: str | None, system: str | None) -> str:
    parts = []
    if apps is not None:
        parts.append(f"アプリ: {THEME_LABELS.get(apps, apps)}")
    if system is not None:
        parts.append(f"Windows: {THEME_LABELS.get(system, system)}")
    return "・".join(parts) or "変えない"


def state_text(st: ThemeState | None) -> str:
    if st is None:
        return "読めない"
    return f"アプリ: {THEME_LABELS.get(st.apps or '', '不明')}・Windows: {THEME_LABELS.get(st.system or '', '不明')}"


def _to_theme(v: Any) -> str | None:
    if isinstance(v, int) and not isinstance(v, bool) and v in (0, 1):
        return "light" if v == 1 else "dark"
    return None


class RegistryTheme:
    """ThemeApi の実物(winreg。HKCU だけを読み書きする)。"""

    def _read(self) -> ThemeState:
        import winreg

        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, PERSONALIZE_KEY, 0, winreg.KEY_QUERY_VALUE)
        except OSError:
            return ThemeState(None, None)
        vals: dict[str, str | None] = {}
        with key:
            for name in (VALUE_APPS, VALUE_SYSTEM):
                try:
                    v, typ = winreg.QueryValueEx(key, name)
                except OSError:
                    vals[name] = None
                    continue
                vals[name] = _to_theme(v) if typ == winreg.REG_DWORD else None
        return ThemeState(vals[VALUE_APPS], vals[VALUE_SYSTEM])

    def get(self) -> ThemeState:
        return self._read()

    def set(self, apps: str | None, system: str | None) -> tuple[ThemeState | None, str]:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, PERSONALIZE_KEY, 0,
                                winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
            for name, val in ((VALUE_APPS, apps), (VALUE_SYSTEM, system)):
                if val in THEMES:
                    winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, 1 if val == "light" else 0)
        note = broadcast_color_set()
        return self._read(), note


def broadcast_color_set() -> str:
    """WM_SETTINGCHANGE("ImmersiveColorSet")を全トップレベルウィンドウへ送る(応答しないウィンドウは待たない)。"""
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    fn = u32.SendMessageTimeoutW
    fn.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPCWSTR, w.UINT, w.UINT, ctypes.POINTER(ctypes.c_size_t)]
    fn.restype = ctypes.c_ssize_t   # LRESULT
    res = ctypes.c_size_t()
    ok = fn(w.HWND(HWND_BROADCAST), WM_SETTINGCHANGE, 0, "ImmersiveColorSet", SMTO_ABORTIFHUNG,
            BROADCAST_TIMEOUT_MS, ctypes.byref(res))
    if not ok:
        err = ctypes.get_last_error()
        log.info("WM_SETTINGCHANGE の送信が途中で終わりました(Win32 エラー %s)", err)
        return "一部のアプリには変更を知らせられなかった(再起動で反映)"
    return ""


# ------------------------------------------------------------------ 計画と実行
def _same(cur: ThemeState, apps: str | None, system: str | None) -> bool:
    return (apps is None or cur.apps == apps) and (system is None or cur.system == system)


def plan(a: dict[str, Any], env: PlanEnv) -> Step:
    apps, system = a.get("apps"), a.get("system")
    cur = env.theme()
    step = Step(0, "theme", "アプリのテーマ", state_text(cur), theme_text(apps, system), True,
                params={"apps": apps, "system": system})
    if env.in_game:
        step.planned, step.reason = False, GAME_REASON
    elif cur is None:
        step.planned, step.reason = False, "テーマの設定を読めない"
    elif _same(cur, apps, system):
        step.planned, step.reason = False, "既に同じ値"
    return step


def run(step: Step, env: ExecEnv) -> tuple[str, str]:
    if env.in_game:
        return SKIPPED, GAME_REASON
    apps, system = step.params.get("apps"), step.params.get("system")
    cur = env.backends.theme.get()
    if _same(cur, apps, system):
        return SKIPPED, "既に同じ値"
    rb, note = env.backends.theme.set(apps, system)
    if rb is None or not _same(rb, apps, system):
        return FAILED, "書いた値を読み戻せない"
    if env.snapshot is not None and env.snapshot.get("theme") is not None:
        env.snapshot["theme"]["written"] = {"apps": rb.apps, "system": rb.system}
    return OK, "読み戻して一致を確認" + (f"({note})" if note else "")
