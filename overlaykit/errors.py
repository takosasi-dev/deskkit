# OverlayKit の例外階層(仕様書 §9.1 errors.py)。
class OverlayKitError(Exception):
    pass


class NotSupportedError(OverlayKitError):
    pass


class HotkeyError(OverlayKitError):
    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = code


class HotkeyConflictError(HotkeyError):
    """RegisterHotKey が ERROR_HOTKEY_ALREADY_REGISTERED で失敗した(他アプリと競合)。"""
