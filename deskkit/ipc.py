# 単一インスタンスと CLI 転送(D-7)。QLocalServer(Windows では名前付きパイプ)をユーザー限定で開く。
# 1行の JSON {"args": [...]} を受け、{"code": int, "out": str} を1行で返す。IPC 以外の通信はしない(C-6)。
from __future__ import annotations

import ctypes
import getpass
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger("deskkit.host.ipc")

EXIT_NOT_RUNNING = 10
EXIT_TIMEOUT = 12
# H-8: 1回の転送で運べる量。Windows のコマンドラインは 32,767 文字までなので、200 個・合計 32,000 文字のパスは
# CLI の引数としては上限に近い。IPC 側はその数倍(日本語のパスは UTF-8 で 1 文字 3 バイト)を受けられるようにしておく
MAX_FORWARD_PATHS = 200
MAX_FORWARD_CHARS = 32000
MAX_REQUEST_BYTES = 1 << 20  # 1 行の要求の上限(同じユーザーのプロセスしか繋げないが、際限なく溜めない)
OPEN_LAUNCH_WAIT_S = 15.0  # H-B: `<module> open ...` で host を起動したとき、転送できるまで待つ最大時間


def server_name() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "user"
    digest = hashlib.sha256(user.lower().encode("utf-8")).hexdigest()[:12]
    home = os.environ.get("DESKKIT_HOME")
    if home:
        # テスト・自己検査用にデータ置き場を分けた DeskKit は、別のインスタンスとして扱う(本番の常駐に繋がない)
        return f"DeskKit-{digest}-{hashlib.sha256(home.lower().encode('utf-8')).hexdigest()[:8]}"
    return f"DeskKit-{digest}"


_mutex_handle: int | None = None


def acquire_instance_mutex(name: str | None = None) -> bool:
    """同一ユーザーで既に起動していれば False。"""
    global _mutex_handle
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.CloseHandle.restype = ctypes.c_int
    h = k32.CreateMutexW(None, False, "Local\\" + (name or server_name()))
    already = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
    if h and already:
        # 相手のミューテックスへのハンドルを持ち続けると、相手が終わっても消えなくなる(再試行が永久に失敗する)
        k32.CloseHandle(h)
        return False
    _mutex_handle = h
    return bool(h)


def wait_instance_mutex(timeout_s: float, interval_s: float = 0.5, name: str | None = None) -> bool:
    """更新・再起動の直後用: 旧プロセスが終わってミューテックスが空くまで待って取る。取れなければ False。"""
    deadline = time.monotonic() + timeout_s
    while True:
        if acquire_instance_mutex(name):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval_s)


class IpcServer(QObject):
    def __init__(self, handler: Callable[[list[str]], tuple[int, str]]) -> None:
        super().__init__()
        self._handler = handler
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._on_new)
        self._buffers: dict[QLocalSocket, bytes] = {}

    def listen(self, name: str | None = None) -> bool:
        name = name or server_name()
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
            end = self._buffers[sock].find(b"\n")
            if end > MAX_REQUEST_BYTES or (end < 0 and len(self._buffers[sock]) > MAX_REQUEST_BYTES):
                log.warning("IPC 要求が大きすぎるため切断しました(%d バイト)", len(self._buffers[sock]))
                self._buffers[sock] = b""
                sock.abort()
                return
            if end < 0:
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


def forward(args: list[str], timeout_ms: int = 60000, *, name: str | None = None, connect_ms: int = 1500) -> tuple[int, str]:
    """起動中の host へ引数を転送する。host が無ければ (10, ...)、応答が無ければ (12, ...)。"""
    sock = QLocalSocket()
    sock.connectToServer(name or server_name())
    if not sock.waitForConnected(connect_ms):
        return EXIT_NOT_RUNNING, "DeskKit が起動していません"
    sock.write((json.dumps({"args": args}, ensure_ascii=False) + "\n").encode("utf-8"))
    sock.flush()
    # 大きな要求(H-8: パス 200 個で 100KB 前後)はパイプの送信バッファに入りきらない。イベントループの無い CLI 側では
    # flush だけでは残りが送られないので、書き終わるまで待つ
    while sock.bytesToWrite() > 0:
        if not sock.waitForBytesWritten(timeout_ms):
            return EXIT_TIMEOUT, "DeskKit へ送り切れませんでした(タイムアウト)"
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


def is_open_command(args: list[str]) -> bool:
    """`<module> open <paths...>`(SendPrep の「送る」など)か。host が起動していなければ起動してから転送する(H-B)。"""
    return len(args) >= 2 and not args[0].startswith("-") and args[1] == "open"


def forward_or_launch(args: list[str], launch: Callable[[], None], *, wait_s: float = OPEN_LAUNCH_WAIT_S,
                      interval_s: float = 0.3, forward_fn: Callable[[list[str]], tuple[int, str]] | None = None,
                      clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> tuple[int, str]:
    """転送し、host が起動していなければ launch() で起動して、転送できるまで最大 wait_s 秒待つ。
    起動に失敗した・時間内に繋がらないときは (10, 理由)。host に繋がったあとの結果(モジュールが無効なら 11 など)はそのまま返す。"""
    fwd = forward_fn or (lambda a: forward(a, connect_ms=500))
    code, out = fwd(args)
    if code != EXIT_NOT_RUNNING:
        return code, out
    try:
        launch()
    except OSError as e:
        log.error("DeskKit を起動できませんでした: %s", type(e).__name__)
        return EXIT_NOT_RUNNING, "DeskKit を起動できませんでした"
    deadline = clock() + wait_s
    while clock() < deadline:
        sleep(interval_s)
        code, out = fwd(args)
        if code != EXIT_NOT_RUNNING:
            return code, out
    return EXIT_NOT_RUNNING, f"DeskKit が {wait_s:.0f} 秒以内に起動しませんでした"
