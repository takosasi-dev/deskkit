# テストと --selftest 用の偽 Win32Api。モニタとウィンドウを固定データで持ち、set_placement の呼び出しを記録する。
# 実機のウィンドウには一切触れない。
from __future__ import annotations

from dataclasses import dataclass, field, replace

from .model import SW_SHOWMAXIMIZED, SW_SHOWNORMAL, Placement, RawMonitor, RawWindow, Rect


def fake_monitor(device: str, rect: Rect, *, primary: bool = False, dpi: int = 96, work: Rect | None = None,
                 mid: str | None = "auto") -> RawMonitor:
    ident = f"\\\\?\\DISPLAY#FAKE{device[-1]}#{device}" if mid == "auto" else mid
    return RawMonitor(device=device, rect=rect, work=work or (rect[0], rect[1], rect[2], rect[3] - 40), primary=primary,
                      dpi=dpi, ids={"device_interface": ident, "displayconfig_path": ident,
                                    "edid": f"FAK-{device[-1]}" if ident else None})


def fake_window(hwnd: int, pid: int, exe: str | None, cls: str, title: str = "", *, rect: Rect = (100, 100, 900, 700),
                maximized: bool = False, visible: bool = True, iconic: bool = False, toolwindow: bool = False,
                owned: bool = False, cloaked: bool = False, proc_start: int | None = 1000) -> RawWindow:
    show = 2 if iconic else (SW_SHOWMAXIMIZED if maximized else SW_SHOWNORMAL)
    return RawWindow(hwnd=hwnd, pid=pid, cls=cls, title=title, visible=visible, iconic=iconic, toolwindow=toolwindow,
                     owned=owned, cloaked=cloaked, exe=exe, exe_error=0 if exe else 5, proc_start=proc_start,
                     placement=Placement(show, rect), screen_rect=rect)


@dataclass
class FakeWin32:
    monitors: list[RawMonitor] = field(default_factory=list)
    windows: list[RawWindow] = field(default_factory=list)
    pid: int = 4242
    foreground: int = 0
    fail_hwnds: dict[int, int] = field(default_factory=dict)  # hwnd → 返す Win32 エラー
    set_calls: list[tuple[int, Placement]] = field(default_factory=list)
    steal_focus: bool = False  # True なら set_placement が foreground を奪う(FR-16 の検査用)
    elevated_pids: set[int] = field(default_factory=set)  # 昇格しているプロセス
    unknown_elevation_pids: set[int] = field(default_factory=set)  # 昇格状態を読めないプロセス
    enum_calls: int = 0  # enum_windows の呼び出し回数(ポーリングの負荷検査用)

    def enum_monitors(self) -> list[RawMonitor]:
        return list(self.monitors)

    def enum_windows(self) -> list[int]:
        self.enum_calls += 1
        return [w.hwnd for w in self.windows]

    def describe_window(self, hwnd: int) -> RawWindow | None:
        return next((w for w in self.windows if w.hwnd == hwnd), None)

    def get_placement(self, hwnd: int) -> Placement | None:
        w = self.describe_window(hwnd)
        return w.placement if w else None

    def set_placement(self, hwnd: int, placement: Placement) -> int:
        self.set_calls.append((hwnd, placement))
        if hwnd in self.fail_hwnds:
            return self.fail_hwnds[hwnd]
        for i, w in enumerate(self.windows):
            if w.hwnd == hwnd:
                cmd = SW_SHOWMAXIMIZED if placement.show_cmd == SW_SHOWMAXIMIZED else SW_SHOWNORMAL
                self.windows[i] = replace(w, placement=Placement(cmd, placement.normal_rect), screen_rect=placement.normal_rect)
                if self.steal_focus:
                    self.foreground = hwnd
                return 0
        return 1400  # ERROR_INVALID_WINDOW_HANDLE

    def is_window(self, hwnd: int) -> bool:
        return any(w.hwnd == hwnd for w in self.windows)

    def foreground_window(self) -> int:
        return self.foreground

    def current_pid(self) -> int:
        return self.pid

    def is_elevated(self, pid: int) -> bool | None:
        if pid in self.unknown_elevation_pids:
            return None
        return pid in self.elevated_pids
