# 単一インスタンスと CLI 転送(D-7)。QLocalServer(Windows では名前付きパイプ)をユーザー限定で開く。
# 1行の JSON {"args": [...]} を受け、{"code": int, "out": str} を1行で返す。IPC 以外の通信はしない(C-6)。
from __future__ import annotations

import ctypes
import getpass
import hashlib
import json
import logging
from collections.abc import Callable

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger("deskkit.host.ipc")

EXIT_NOT_RUNNING = 10
EXIT_TIMEOUT = 12


def server_name() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "user"
    digest = hashlib.sha256(user.lower().encode("utf-8")).hexdigest()[:12]
    return f"DeskKit-{digest}"


_mutex_handle: int | None = None


def acquire_instance_mutex() -> bool:
    """同一ユーザーで既に起動していれば False。"""
    global _mutex_handle
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    k32.CreateMutexW.restype = ctypes.c_void_p
    h = k32.CreateMutexW(None, False, "Local\\" + server_name())
    already = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
    _mutex_handle = h
    return bool(h) and not already


class IpcServer(QObject):
    def __init__(self, handler: Callable[[list[str]], tuple[int, str]]) -> None:
        super().__init__()
        self._handler = handler
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._on_new)
        self._buffers: dict[QLocalSocket, bytes] = {}

    def listen(self) -> bool:
        name = server_name()
        if not self._server.listen(name):
            QLocalServer.removeServer(name)
            if not self._server.listen(name):
                log.error("IPC サーバーを開けません: %s", self._server.errorString())
                return False
        log.info("IPC サーバーを開きました")
        return True

    def close(self) -> None:
        self._server.close()

    def _on_new(self) -> None:
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            self._buffers[sock] = b""
            sock.readyRead.connect(lambda s=sock: self._on_ready(s))
            sock.disconnected.connect(lambda s=sock: self._drop(s))

    def _drop(self, sock: QLocalSocket) -> None:
        self._buffers.pop(sock, None)
        sock.deleteLater()

    def _on_ready(self, sock: QLocalSocket) -> None:
        try:
            self._buffers[sock] = self._buffers.get(sock, b"") + bytes(sock.readAll().data())
            if b"\n" not in self._buffers[sock]:
                return
            line = self._buffers[sock].split(b"\n", 1)[0]
            try:
                req = json.loads(line.decode("utf-8"))
                args = [str(a) for a in req.get("args", [])]
            except (ValueError, AttributeError):
                code, out = 1, "不正な要求です"
            else:
                code, out = self._handler(args)
            sock.write((json.dumps({"code": code, "out": out}, ensure_ascii=False) + "\n").encode("utf-8"))
            sock.flush()
            sock.disconnectFromServer()
        except Exception:  # noqa: BLE001 - スロット内の例外を Qt へ漏らさない(INV-5)
            log.exception("IPC 要求の処理で例外")


def forward(args: list[str], timeout_ms: int = 60000) -> tuple[int, str]:
    """起動中の host へ引数を転送する。host が無ければ (10, ...)、応答が無ければ (12, ...)。"""
    sock = QLocalSocket()
    sock.connectToServer(server_name())
    if not sock.waitForConnected(1500):
        return EXIT_NOT_RUNNING, "DeskKit が起動していません"
    sock.write((json.dumps({"args": args}, ensure_ascii=False) + "\n").encode("utf-8"))
    sock.flush()
    buf = b""
    while b"\n" not in buf:
        if not sock.waitForReadyRead(timeout_ms):
            if sock.state() == QLocalSocket.LocalSocketState.UnconnectedState and buf:
                break
            return EXIT_TIMEOUT, "DeskKit から応答がありません(タイムアウト)"
        buf += bytes(sock.readAll().data())
    try:
        resp = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
        return int(resp.get("code", 1)), str(resp.get("out", ""))
    except (ValueError, AttributeError):
        return 1, "応答を解釈できません"
