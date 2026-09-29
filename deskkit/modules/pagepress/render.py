# 描画スレッド(P-2・INV-2)。pypdfium2 を import してよい唯一のファイルで、PDFium の関数はこのスレッドの中でだけ呼ぶ。
# 依頼は文書ごとに「見えている範囲 + 前後」をまとめて差し替え、範囲から外れた待ちの依頼は捨てる。文書を閉じるときは
# 描いている1ページを終えてから閉じる(§10)。一覧の画像の行のサムネイルも同じスレッドで Pillow で作る。ディスクには書かない(INV-6)。
from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_STOPPED = "stopped"
STATE_LOAD_FAILED = "load_failed"

_log = logging.getLogger("deskkit.pagepress")


class Backend(Protocol):
    def open(self, path: str) -> Any: ...
    def render(self, doc: Any, index: int, side: int) -> Any: ...   # QImage(長辺 side)。描けなければ None
    def close(self, doc: Any) -> None: ...
    def version(self) -> str: ...


def _qimage_rgb(data: bytes, w: int, h: int, stride: int) -> Any:
    from PySide6.QtGui import QImage

    return QImage(data, w, h, stride, QImage.Format.Format_RGB888).copy()  # バッファの寿命から切り離す


class PdfiumBackend:
    """本物の PDFium。最初の呼び出し(描画スレッドの中)で pypdfium2 を読む。"""

    def __init__(self) -> None:
        self._mod: Any = None

    def _lib(self) -> Any:
        if self._mod is None:
            import pypdfium2

            self._mod = pypdfium2
        return self._mod

    def open(self, path: str) -> Any:
        pdfium = self._lib()
        doc = pdfium.PdfDocument(path)  # INV-1: PDFium にはパスを渡す(読むだけ)
        try:
            doc.init_forms()  # 入力欄の値も描く(P-2)
        except Exception:  # noqa: BLE001 - 入力欄を描けなくてもページは描く
            pass
        return doc

    def render(self, doc: Any, index: int, side: int) -> Any:
        page = doc[index]
        try:
            w, h = page.get_size()
            scale = side / max(1.0, float(max(w, h)))
            bm = page.render(scale=scale, may_draw_forms=True, rev_byteorder=True)
            try:
                if bm.mode != "RGB":
                    return None
                return _qimage_rgb(bytes(bm.buffer), int(bm.width), int(bm.height), int(bm.stride))
            finally:
                bm.close()
        finally:
            page.close()

    def close(self, doc: Any) -> None:
        doc.close()

    def version(self) -> str:
        try:
            return str(self._lib().PDFIUM_INFO)
        except Exception:  # noqa: BLE001
            return "unknown"


def load_image_thumb(path: str, side: int) -> Any:
    """画像の行のサムネイル(向きを直した QImage)。読めなければ None。"""
    from PIL import Image, ImageOps

    try:
        if path.lower().endswith((".heic", ".heif")):
            from deskkit.modules.pagepress.reader import register_heif

            register_heif()
        with Image.open(path) as im:
            if (im.format or "").upper() == "JPEG":
                im.draft("RGB", (side * 2, side * 2))
            im.load()
            fixed = ImageOps.exif_transpose(im)
            rgb = fixed.convert("RGBA")
            bg = Image.new("RGB", rgb.size, (255, 255, 255))
            bg.paste(rgb, mask=rgb.getchannel("A"))
            bg.thumbnail((side, side), Image.Resampling.LANCZOS)
            return _qimage_rgb(bg.tobytes("raw", "RGB"), bg.width, bg.height, bg.width * 3)
    except Exception:  # noqa: BLE001 - 読めない画像は「表示できません」
        return None


@dataclass
class _Task:
    kind: str                     # "page" | "first" | "close"
    doc_id: int = 0
    index: int = 0
    side: int = 160
    path: str = ""
    key: Any = None
    cb: Callable[..., None] | None = None
    img_kind: str = ""            # "first" の種類("pdf" | "image")


_FAILED = object()


class Renderer:
    """描画スレッド1本と、その待ち行列。GUI スレッドから呼ぶ。結果は deliver(fn) で GUI スレッドへ返す。"""

    def __init__(self, deliver: Callable[[Callable[[], None]], None], *,
                 backend_factory: Callable[[], Backend] = PdfiumBackend,
                 image_loader: Callable[[str, int], Any] = load_image_thumb) -> None:
        self._deliver = deliver
        self._factory = backend_factory
        self._image_loader = image_loader
        self._q: deque[_Task] = deque()
        self._cv = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._paths: dict[int, str] = {}
        self._docs: dict[int, Any] = {}      # 描画スレッドだけが触る
        self._backend: Backend | None = None
        self.state = STATE_IDLE
        self.pdfium_version = "not_loaded"
        self.rendered = 0
        self.thread_id: int | None = None

    # ---------------------------------------------------------------- GUI スレッドから
    def open_doc(self, doc_id: int, path: Path | str) -> None:
        with self._cv:
            self._paths[doc_id] = str(path)

    def close_doc(self, doc_id: int) -> None:
        """その文書の待ちの依頼を捨て、描いている1ページのあとで閉じる。"""
        with self._cv:
            self._paths.pop(doc_id, None)
            self._q = deque(t for t in self._q if not (t.kind == "page" and t.doc_id == doc_id))
            self._push(_Task("close", doc_id))

    def want_pages(self, doc_id: int, indices: list[int], side: int, cb: Callable[[int, Any], None]) -> None:
        """文書の待ちの依頼を indices(この順に描く)で置き換える。外れた依頼は描かない(P-2)。"""
        with self._cv:
            self._q = deque(t for t in self._q if not (t.kind == "page" and t.doc_id == doc_id))
            if doc_id in self._paths:
                for i in indices:
                    self._push(_Task("page", doc_id, i, side, cb=cb))

    def request_first(self, key: Any, path: Path | str, kind: str, side: int, cb: Callable[[Any], None]) -> None:
        """一覧の行のサムネイル(PDF は1ページ目、画像はその画像)。"""
        with self._cv:
            self._push(_Task("first", 0, 0, side, str(path), key, cb, kind))

    def drop_first(self, keep: set[Any]) -> None:
        with self._cv:
            self._q = deque(t for t in self._q if t.kind != "first" or t.key in keep)

    def pending(self) -> int:
        with self._cv:
            return len(self._q)

    def stop(self, timeout: float = 5.0) -> bool:
        with self._cv:
            self._stopping = True
            self._q.clear()
            th = self._thread
            self._cv.notify_all()
        if th is not None and th is not threading.current_thread():
            th.join(timeout)
            ok = not th.is_alive()
        else:
            ok = True
        if self.state != STATE_LOAD_FAILED:
            self.state = STATE_STOPPED
        return ok

    def _push(self, t: _Task) -> None:
        if self._stopping:
            return
        self._q.append(t)
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="pagepress-render", daemon=True)
            self._thread.start()
        self._cv.notify_all()

    # ---------------------------------------------------------------- 描画スレッド
    def _get_backend(self) -> Backend | None:
        if self._backend is None and self.state != STATE_LOAD_FAILED:
            try:
                self._backend = self._factory()
                self.pdfium_version = self._backend.version()
                self.state = STATE_RUNNING
            except Exception as e:  # noqa: BLE001 - 読み込めなければ全部「表示できません」
                _log.error("renderer load failed: %s", type(e).__name__)
                self.state = STATE_LOAD_FAILED
        return self._backend

    def _send(self, fn: Callable[[], None]) -> None:
        def safe() -> None:
            try:
                fn()
            except RuntimeError:
                pass  # 画面が先に閉じられた
        try:
            self._deliver(safe)
        except Exception:  # noqa: BLE001
            pass

    def _doc(self, doc_id: int) -> Any:
        d = self._docs.get(doc_id)
        if d is not None:
            return None if d is _FAILED else d
        with self._cv:
            path = self._paths.get(doc_id)
        be = self._get_backend()
        if path is None or be is None:
            return None
        try:
            d = be.open(path)
        except Exception:  # noqa: BLE001 - PDFium が開けない PDF は「表示できません」(§10)
            d = _FAILED
        self._docs[doc_id] = d
        return None if d is _FAILED else d

    def _close(self, doc_id: int) -> None:
        d = self._docs.pop(doc_id, None)
        if d is not None and d is not _FAILED and self._backend is not None:
            try:
                self._backend.close(d)
            except Exception:  # noqa: BLE001
                pass

    def _run(self, t: _Task) -> None:
        if t.kind == "close":
            self._close(t.doc_id)
            return
        if t.kind == "page":
            doc = self._doc(t.doc_id)
            img = None
            if doc is not None and self._backend is not None:
                try:
                    img = self._backend.render(doc, t.index, t.side)
                except Exception:  # noqa: BLE001 - 描けないページは「表示できません」(FR-7)
                    img = None
            self.rendered += 1
            cb, idx = t.cb, t.index
            if cb is not None:
                self._send(lambda: cb(idx, img))
            return
        # first: 一覧の行
        img = None
        if t.img_kind == "image":
            img = self._image_loader(t.path, t.side)
        else:
            be = self._get_backend()
            if be is not None:
                d = None
                try:
                    d = be.open(t.path)
                    img = be.render(d, 0, t.side)
                except Exception:  # noqa: BLE001
                    img = None
                finally:
                    if d is not None:
                        try:
                            be.close(d)
                        except Exception:  # noqa: BLE001
                            pass
        cb2 = t.cb
        if cb2 is not None:
            self._send(lambda: cb2(img))

    def _loop(self) -> None:
        self.thread_id = threading.get_ident()
        while True:
            with self._cv:
                while not self._q and not self._stopping:
                    if not self._cv.wait(30.0) and not self._q and not self._docs:
                        self._thread = None  # 開いている文書が無く、しばらく依頼も無い。次の依頼で作り直す
                        return
                if self._stopping:
                    break
                t = self._q.popleft()
            try:
                self._run(t)
            except Exception as e:  # noqa: BLE001 - 1件の失敗で描画スレッドを止めない
                _log.error("render task failed: %s", type(e).__name__)
        for doc_id in list(self._docs):
            self._close(doc_id)
