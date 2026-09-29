# OverlayKit のうち DeskKit が依存する HotkeyRegistry 部分だけを同梱した最小実装。
# インタフェースは OverlayKit 仕様書 §9.1 に合わせる(register / register_all / unregister / triggered)。(v0.4)probe を足した。
from overlaykit.errors import HotkeyConflictError, HotkeyError, OverlayKitError
from overlaykit.hotkey import HotkeyRegistry, ProbeBatch, ProbeDone, ProbeResult, ProbeTask, RegisterResult

__all__ = ["HotkeyConflictError", "HotkeyError", "HotkeyRegistry", "OverlayKitError", "ProbeBatch", "ProbeDone",
           "ProbeResult", "ProbeTask", "RegisterResult"]
