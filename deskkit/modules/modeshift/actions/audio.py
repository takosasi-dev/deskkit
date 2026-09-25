# master_volume / app_volume / mic_volume: Core Audio の公開 COM インタフェースを ctypes の vtable 経由で直接呼ぶ(D-8。
# 追加ライブラリなし)。呼び出したスレッドで CoInitializeEx し、取得したインタフェースはすべて Release する。設定直後に
# 読み戻した値を snapshot の「書いた値」に記録する(FR-15 / FR-16)。マイクは既定の録音デバイスの IAudioEndpointVolume。
from __future__ import annotations

import ctypes
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from ctypes import wintypes as w
from typing import Any

from deskkit.modules.modeshift.actions.common import ExecEnv, PlanEnv
from deskkit.modules.modeshift.model import FAILED, OK, SKIPPED, Step, pct
from deskkit.modules.modeshift.system import MasterState, SessionInfo

# ------------------------------------------------------------------ vtable の順序(Windows SDK ヘッダの宣言順)
# どのインタフェースも先頭は IUnknown: 0 QueryInterface / 1 AddRef / 2 Release。
#
# mmdeviceapi.h  IMMDeviceEnumerator : IUnknown
#   3 EnumAudioEndpoints  4 GetDefaultAudioEndpoint  5 GetDevice
#   6 RegisterEndpointNotificationCallback  7 UnregisterEndpointNotificationCallback
# mmdeviceapi.h  IMMDevice : IUnknown
#   3 Activate  4 OpenPropertyStore  5 GetId  6 GetState
# endpointvolume.h  IAudioEndpointVolume : IUnknown
#   3 RegisterControlChangeNotify  4 UnregisterControlChangeNotify  5 GetChannelCount
#   6 SetMasterVolumeLevel  7 SetMasterVolumeLevelScalar  8 GetMasterVolumeLevel  9 GetMasterVolumeLevelScalar
#   10 SetChannelVolumeLevel  11 SetChannelVolumeLevelScalar  12 GetChannelVolumeLevel  13 GetChannelVolumeLevelScalar
#   14 SetMute  15 GetMute  16 GetVolumeStepInfo  17 VolumeStepUp  18 VolumeStepDown
#   19 QueryHardwareSupport  20 GetVolumeRange
# audiopolicy.h  IAudioSessionManager : IUnknown
#   3 GetAudioSessionControl  4 GetSimpleAudioVolume
# audiopolicy.h  IAudioSessionManager2 : IAudioSessionManager
#   5 GetSessionEnumerator  6 RegisterSessionNotification  7 UnregisterSessionNotification
#   8 RegisterDuckNotification  9 UnregisterDuckNotification
# audiopolicy.h  IAudioSessionEnumerator : IUnknown
#   3 GetCount  4 GetSession
# audiopolicy.h  IAudioSessionControl : IUnknown
#   3 GetState  4 GetDisplayName  5 SetDisplayName  6 GetIconPath  7 SetIconPath
#   8 GetGroupingParam  9 SetGroupingParam  10 RegisterAudioSessionNotification  11 UnregisterAudioSessionNotification
# audiopolicy.h  IAudioSessionControl2 : IAudioSessionControl
#   12 GetSessionIdentifier  13 GetSessionInstanceIdentifier  14 GetProcessId  15 IsSystemSoundsSession
#   16 SetDuckingPreference
# audioclient.h  ISimpleAudioVolume : IUnknown
#   3 SetMasterVolume  4 GetMasterVolume  5 SetMute  6 GetMute
IDX_RELEASE = 2
IDX_QI = 0
IDX_ENUM_GET_DEFAULT = 4
IDX_DEV_ACTIVATE = 3
IDX_DEV_GET_ID = 5
IDX_EPV_SET_SCALAR = 7
IDX_EPV_GET_SCALAR = 9
IDX_EPV_SET_MUTE = 14
IDX_EPV_GET_MUTE = 15
IDX_ASM2_GET_ENUM = 5
IDX_SENUM_COUNT = 3
IDX_SENUM_GET = 4
IDX_ASC_GET_STATE = 3
IDX_ASC2_INSTANCE_ID = 13
IDX_ASC2_PID = 14
IDX_ASC2_IS_SYSTEM = 15
IDX_SAV_SET = 3
IDX_SAV_GET = 4
IDX_SAV_GET_MUTE = 6

CLSID_MMDeviceEnumerator = "BCDE0395-E52F-467C-8E3D-C4579291692E"
IID_IMMDeviceEnumerator = "A95664D2-9614-4F35-A746-DE8DB63617E6"
IID_IAudioEndpointVolume = "5CDF2C82-841E-4546-9722-0CF74078229A"
IID_IAudioSessionManager2 = "77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F"
IID_IAudioSessionControl2 = "BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D"
IID_ISimpleAudioVolume = "87CE5498-68D6-44E5-9215-6DA47EF883D8"

CLSCTX_ALL = 0x17            # INPROC_SERVER | INPROC_HANDLER | LOCAL_SERVER | REMOTE_SERVER
COINIT_MULTITHREADED = 0x0
E_RENDER = 0                 # EDataFlow.eRender
E_CAPTURE = 1                # EDataFlow.eCapture(マイク)
E_MULTIMEDIA = 1             # ERole.eMultimedia
S_OK = 0
RPC_E_CHANGED_MODE = -2147417850    # 0x80010106
E_NOTFOUND = -2147023728            # 0x80070490(既定のデバイスが無い)
AUDIO_SESSION_STATE_EXPIRED = 2

HRESULT = ctypes.c_long


class GUID(ctypes.Structure):
    _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, text: str) -> GUID:
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


_ole32 = ctypes.WinDLL("ole32", use_last_error=True)
_ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, w.DWORD]
_ole32.CoInitializeEx.restype = HRESULT
_ole32.CoUninitialize.argtypes = []
_ole32.CoUninitialize.restype = None
_ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, w.DWORD, ctypes.POINTER(GUID),
                                    ctypes.POINTER(ctypes.c_void_p)]
_ole32.CoCreateInstance.restype = HRESULT
_ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
_ole32.CoTaskMemFree.restype = None


class ComError(OSError):
    def __init__(self, what: str, hr: int) -> None:
        super().__init__(f"{what} が失敗 (HRESULT 0x{hr & 0xFFFFFFFF:08X})")
        self.hr = hr


def _vtbl_entry(p: int, index: int) -> int:
    vtbl = ctypes.cast(ctypes.c_void_p(p), ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return int(vtbl[index] or 0)


def _call(p: int, index: int, argtypes: list[Any], *args: Any, what: str = "COM") -> int:
    proto = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, *argtypes)
    hr = int(proto(_vtbl_entry(p, index))(ctypes.c_void_p(p), *args))
    if hr < 0:
        raise ComError(what, hr)
    return hr


def _release(p: int | None) -> None:
    if p:
        proto = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        proto(_vtbl_entry(p, IDX_RELEASE))(ctypes.c_void_p(p))


def _qi(p: int, iid: str) -> int:
    out = ctypes.c_void_p()
    g = GUID.of(iid)
    _call(p, IDX_QI, [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(g), ctypes.byref(out),
          what="QueryInterface")
    return int(out.value or 0)


@contextmanager
def com_scope() -> Iterator[None]:
    """呼び出したスレッドで COM を初期化する。既に別モード(Qt のメインスレッドの STA 等)なら解除はしない。"""
    hr = int(_ole32.CoInitializeEx(None, COINIT_MULTITHREADED))
    if hr < 0 and hr != RPC_E_CHANGED_MODE:
        raise ComError("CoInitializeEx", hr)
    try:
        yield
    finally:
        if hr >= 0:     # S_OK / S_FALSE のときは対になる CoUninitialize が必要
            _ole32.CoUninitialize()


@contextmanager
def _ref(p: int) -> Iterator[int]:
    try:
        yield p
    finally:
        _release(p)


def _enumerator() -> int:
    out = ctypes.c_void_p()
    clsid, iid = GUID.of(CLSID_MMDeviceEnumerator), GUID.of(IID_IMMDeviceEnumerator)
    hr = int(_ole32.CoCreateInstance(ctypes.byref(clsid), None, CLSCTX_ALL, ctypes.byref(iid), ctypes.byref(out)))
    if hr < 0:
        raise ComError("CoCreateInstance(MMDeviceEnumerator)", hr)
    return int(out.value or 0)


def _default_device(enum: int, flow: int = E_RENDER) -> int | None:
    out = ctypes.c_void_p()
    try:
        _call(enum, IDX_ENUM_GET_DEFAULT, [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
              flow, E_MULTIMEDIA, ctypes.byref(out), what="GetDefaultAudioEndpoint")
    except ComError as e:
        if e.hr == E_NOTFOUND:
            return None
        raise
    return int(out.value or 0) or None


def _device_id(dev: int) -> str | None:
    s = ctypes.c_void_p()
    _call(dev, IDX_DEV_GET_ID, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(s), what="IMMDevice::GetId")
    if not s.value:
        return None
    try:
        return ctypes.wstring_at(s.value)
    finally:
        _ole32.CoTaskMemFree(s)


def _activate(dev: int, iid: str) -> int:
    out = ctypes.c_void_p()
    g = GUID.of(iid)
    _call(dev, IDX_DEV_ACTIVATE, [ctypes.POINTER(GUID), w.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
          ctypes.byref(g), CLSCTX_ALL, None, ctypes.byref(out), what="IMMDevice::Activate")
    return int(out.value or 0)


def _epv_read(epv: int) -> tuple[float, bool]:
    lv = ctypes.c_float()
    _call(epv, IDX_EPV_GET_SCALAR, [ctypes.POINTER(ctypes.c_float)], ctypes.byref(lv), what="GetMasterVolumeLevelScalar")
    mute = w.BOOL()
    _call(epv, IDX_EPV_GET_MUTE, [ctypes.POINTER(w.BOOL)], ctypes.byref(mute), what="GetMute")
    return float(lv.value), bool(mute.value)


class CoreAudio:
    """AudioApi の実物。メソッドごとに COM を初期化し、使ったインタフェースを全部 Release する。"""

    def _with_endpoint(self, fn: Callable[[int, str | None], Any], flow: int = E_RENDER) -> Any:
        with com_scope(), _ref(_enumerator()) as enum:
            dev = _default_device(enum, flow)
            if dev is None:
                return None
            with _ref(dev):
                dev_id = _device_id(dev)
                with _ref(_activate(dev, IID_IAudioEndpointVolume)) as epv:
                    return fn(epv, dev_id)

    def get_master(self) -> MasterState | None:
        return self._get_endpoint(E_RENDER)

    def set_master(self, level: float | None, mute: bool | None) -> MasterState | None:
        return self._set_endpoint(E_RENDER, level, mute)

    def get_capture(self) -> MasterState | None:
        """既定の録音デバイス(マイク)の音量とミュート。デバイスが無ければ None。"""
        return self._get_endpoint(E_CAPTURE)

    def set_capture(self, level: float | None, mute: bool | None) -> MasterState | None:
        return self._set_endpoint(E_CAPTURE, level, mute)

    def _get_endpoint(self, flow: int) -> MasterState | None:
        def read(epv: int, dev_id: str | None) -> MasterState:
            lv, mute = _epv_read(epv)
            return MasterState(lv, mute, dev_id)

        return self._with_endpoint(read, flow)  # type: ignore[no-any-return]

    def _set_endpoint(self, flow: int, level: float | None, mute: bool | None) -> MasterState | None:
        def write(epv: int, dev_id: str | None) -> MasterState:
            if level is not None:
                v = max(0.0, min(1.0, float(level)))
                _call(epv, IDX_EPV_SET_SCALAR, [ctypes.c_float, ctypes.POINTER(GUID)], ctypes.c_float(v), None,
                      what="SetMasterVolumeLevelScalar")
            if mute is not None:
                _call(epv, IDX_EPV_SET_MUTE, [w.BOOL, ctypes.POINTER(GUID)], w.BOOL(1 if mute else 0), None, what="SetMute")
            lv, mu = _epv_read(epv)   # 設定直後に読み戻す(FR-15)
            return MasterState(lv, mu, dev_id)

        return self._with_endpoint(write, flow)  # type: ignore[no-any-return]

    def _each_session(self, fn: Callable[[str, int, int], None]) -> None:
        """既定の再生デバイスの各セッションについて fn(instance_id, pid, ISimpleAudioVolume*) を呼ぶ。"""
        with com_scope(), _ref(_enumerator()) as enum:
            dev = _default_device(enum)
            if dev is None:
                return
            with _ref(dev), _ref(_activate(dev, IID_IAudioSessionManager2)) as mgr:
                senum_p = ctypes.c_void_p()
                _call(mgr, IDX_ASM2_GET_ENUM, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(senum_p),
                      what="GetSessionEnumerator")
                with _ref(int(senum_p.value or 0)) as senum:
                    count = ctypes.c_int()
                    _call(senum, IDX_SENUM_COUNT, [ctypes.POINTER(ctypes.c_int)], ctypes.byref(count), what="GetCount")
                    for i in range(count.value):
                        ctl_p = ctypes.c_void_p()
                        _call(senum, IDX_SENUM_GET, [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)], i, ctypes.byref(ctl_p),
                              what="GetSession")
                        with _ref(int(ctl_p.value or 0)) as ctl:
                            self._visit(ctl, fn)

    @staticmethod
    def _visit(ctl: int, fn: Callable[[str, int, int], None]) -> None:
        state = ctypes.c_int()
        _call(ctl, IDX_ASC_GET_STATE, [ctypes.POINTER(ctypes.c_int)], ctypes.byref(state), what="GetState")
        if state.value == AUDIO_SESSION_STATE_EXPIRED:
            return
        with _ref(_qi(ctl, IID_IAudioSessionControl2)) as ctl2:
            if _call(ctl2, IDX_ASC2_IS_SYSTEM, [], what="IsSystemSoundsSession") == S_OK:
                return  # システム音のセッションは対象外
            pid = w.DWORD()
            try:
                _call(ctl2, IDX_ASC2_PID, [ctypes.POINTER(w.DWORD)], ctypes.byref(pid), what="GetProcessId")
            except ComError:
                return  # 複数プロセスにまたがるセッション等
            sid_p = ctypes.c_void_p()
            _call(ctl2, IDX_ASC2_INSTANCE_ID, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(sid_p),
                  what="GetSessionInstanceIdentifier")
            try:
                sid = ctypes.wstring_at(sid_p.value) if sid_p.value else ""
            finally:
                if sid_p.value:
                    _ole32.CoTaskMemFree(sid_p)
            if not pid.value or not sid:
                return
            with _ref(_qi(ctl, IID_ISimpleAudioVolume)) as vol:
                fn(sid, int(pid.value), vol)

    @staticmethod
    def _sav_read(vol: int) -> tuple[float, bool]:
        lv = ctypes.c_float()
        _call(vol, IDX_SAV_GET, [ctypes.POINTER(ctypes.c_float)], ctypes.byref(lv), what="ISimpleAudioVolume::GetMasterVolume")
        mute = w.BOOL()
        _call(vol, IDX_SAV_GET_MUTE, [ctypes.POINTER(w.BOOL)], ctypes.byref(mute), what="ISimpleAudioVolume::GetMute")
        return float(lv.value), bool(mute.value)

    def list_sessions(self) -> list[SessionInfo]:
        out: list[SessionInfo] = []

        def collect(sid: str, pid: int, vol: int) -> None:
            lv, mute = self._sav_read(vol)
            out.append(SessionInfo(sid, pid, lv, mute))

        self._each_session(collect)
        return out

    def set_sessions(self, levels: dict[str, float]) -> dict[str, float | None]:
        result: dict[str, float | None] = {sid: None for sid in levels}

        def apply(sid: str, _pid: int, vol: int) -> None:
            if sid not in levels:
                return
            v = max(0.0, min(1.0, float(levels[sid])))
            try:
                _call(vol, IDX_SAV_SET, [ctypes.c_float, ctypes.POINTER(GUID)], ctypes.c_float(v), None,
                      what="ISimpleAudioVolume::SetMasterVolume")
                result[sid] = self._sav_read(vol)[0]   # 読み戻し
            except ComError:
                result[sid] = None

        self._each_session(apply)
        return result


# ------------------------------------------------------------------ 計画と実行
EPS = 0.005   # 音量の一致判定の許容幅(スカラー)


def _same_level(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and abs(a - b) < EPS


def _master_text(level: float | None, mute: bool | None) -> str:
    lv = "音量は変えない" if level is None else pct(level)
    mu = "" if mute is None else ("・ミュート" if mute else "・ミュート解除")
    return lv + mu


def plan_master(a: dict[str, Any], env: PlanEnv) -> Step:
    level, mute = a.get("level"), a.get("mute")
    m = env.master()
    cur = f"{pct(m.level)}・{'ミュート中' if m.mute else 'ミュートなし'}" if m else "読めない"
    step = Step(0, "master_volume", "マスター音量", cur, _master_text(level, mute), True,
                params={"level": level, "mute": mute})
    if m is None:
        step.planned, step.reason = False, "既定の再生デバイスが無い/読めない"
    elif (level is None or _same_level(m.level, level)) and (mute is None or m.mute == mute):
        step.planned, step.reason = False, "既に同じ値"
    return step


def run_master(step: Step, env: ExecEnv) -> tuple[str, str]:
    level, mute = step.params.get("level"), step.params.get("mute")
    cur = env.backends.audio.get_master()
    if cur is None:
        return FAILED, "既定の再生デバイスを読めない"
    if (level is None or _same_level(cur.level, level)) and (mute is None or cur.mute == mute):
        return SKIPPED, "既に同じ値"
    rb = env.backends.audio.set_master(level, mute)
    if rb is None:
        return FAILED, "設定できない(再生デバイスが無い)"
    if env.snapshot is not None and env.snapshot.get("master") is not None:
        env.snapshot["master"]["written"] = {"level": rb.level, "mute": rb.mute}
    return OK, f"読み戻し {pct(rb.level)}" + ("・ミュート" if rb.mute else "")


def plan_app(a: dict[str, Any], env: PlanEnv) -> Step:
    exe: str = a["exe"]
    level = float(a["level"])
    pids = set(env.pids_of(exe))
    sess = [s for s in env.sessions() if s.pid in pids]
    if sess:
        lv = sorted({round(s.level * 100) for s in sess})
        cur = (f"{lv[0]}%" if len(lv) == 1 else f"{lv[0]}〜{lv[-1]}%") + f"({len(sess)} セッション)"
    else:
        cur = "セッションなし" if pids else "プロセスなし"
    params = {"exe": exe, "level": level, "sessions": [{"id": s.session_id, "pid": s.pid} for s in sess]}
    step = Step(0, "app_volume", exe, cur, pct(level), True, params=params)
    if not sess:
        step.planned, step.reason = False, "セッションなし"
    elif all(_same_level(s.level, level) for s in sess):
        step.planned, step.reason = False, "既に同じ値"
    return step


def run_app(step: Step, env: ExecEnv) -> tuple[str, str]:
    level = float(step.params["level"])
    now = {s.session_id: s for s in env.backends.audio.list_sessions()}
    # Plan に列挙したセッションだけ(後から現れたセッションにはさかのぼって設定しない)
    targets = {x["id"]: level for x in step.params.get("sessions", []) if x["id"] in now}
    if not targets:
        return SKIPPED, "セッションなし(終了済み)"
    rb = env.backends.audio.set_sessions(targets)
    if env.snapshot is not None:
        for entry in env.snapshot.get("apps", []):
            if entry.get("session") in rb and rb[entry["session"]] is not None:
                entry["written"] = rb[entry["session"]]
    bad = [sid for sid, v in rb.items() if v is None]
    if len(bad) == len(targets):
        return FAILED, "音量を設定できない"
    return OK, f"{len(targets) - len(bad)} セッションを {pct(level)} に"


# ------------------------------------------------------------------ マイク(mic_volume。v0.2)
def mic_text(level: float | None, mute: bool | None) -> str:
    return _master_text(level, mute)


def plan_mic(a: dict[str, Any], env: PlanEnv) -> Step:
    level, mute = a.get("level"), a.get("mute")
    m = env.capture()
    cur = f"{pct(m.level)}・{'ミュート中' if m.mute else 'ミュートなし'}" if m else "読めない"
    step = Step(0, "mic_volume", "マイク", cur, mic_text(level, mute), True, params={"level": level, "mute": mute})
    if m is None:
        step.planned, step.reason = False, "既定の録音デバイスが無い/読めない"
    elif (level is None or _same_level(m.level, level)) and (mute is None or m.mute == mute):
        step.planned, step.reason = False, "既に同じ値"
    return step


def run_mic(step: Step, env: ExecEnv) -> tuple[str, str]:
    level, mute = step.params.get("level"), step.params.get("mute")
    cur = env.backends.audio.get_capture()
    if cur is None:
        return FAILED, "既定の録音デバイスを読めない"
    if (level is None or _same_level(cur.level, level)) and (mute is None or cur.mute == mute):
        return SKIPPED, "既に同じ値"
    rb = env.backends.audio.set_capture(level, mute)
    if rb is None:
        return FAILED, "設定できない(録音デバイスが無い)"
    if env.snapshot is not None and env.snapshot.get("mic") is not None:
        env.snapshot["mic"]["written"] = {"level": rb.level, "mute": rb.mute}
    return OK, f"読み戻し {pct(rb.level)}" + ("・ミュート" if rb.mute else "")
