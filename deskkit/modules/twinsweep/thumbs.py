# サムネイルとプレビューの読み込み(G-8・INV-5)。別スレッド(2本)で Pillow で縮小デコードして QImage にし、
# 画面のスレッドで QPixmap に変えて、メモリ上の LRU(最大 400 枚)にだけ持つ。ディスクには何も書かない。
# 回転(Orientation)を適用してから縮める(§10)。読めない画像は None を返す。
from __future__ import annotations

import functools
import threading
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

THUMB_SIDE = 160
MAX_THUMBS = 400

Key = tuple[str, int, int]  # (パス, 更新時刻, 長辺)


def load_qimage(path: str, side: int) -> Any:
    """長辺 side 以下の QImage(RGB)。どのスレッドから呼んでもよい。読めなければ None。"""
    from PySide6.QtGui import QImage

    from deskkit.modules.twinsweep.hashing import apply_orientation, open_image, orientation_of, pil_image

    try:
        pil = pil_image()

        with open_image(path) as img:
            orient = orientation_of(img)
            w, h = img.size
            want = (side, side)
            if orient in (5, 6, 7, 8):
                w, h = h, w
            if getattr(img, "format", None) == "JPEG" and max(w, h) > side:
                img.draft("RGB", (max(1, img.size[0] * side // max(img.size)), max(1, img.size[1] * side // max(img.size))))
            try:
                img.seek(0)
            except (EOFError, AttributeError, ValueError):
                pass
            rgb = img.convert("RGB")
            rgb.thumbnail(want, pil.Resampling.LANCZOS)
            rgb = apply_orientation(rgb, orient)
            if max(rgb.size) > side:
                rgb.thumbnail(want, pil.Resampling.LANCZOS)
            data = rgb.tobytes("raw", "RGB")
            qi = QImage(data, rgb.size[0], rgb.size[1], rgb.size[0] * 3, QImage.Format.Format_RGB888)
            return qi.copy()  # data の寿命から切り離す
    except Exception:  # noqa: BLE001 - 読めない画像は「読めない」だけ(FR-7)
        return None


class ThumbLoader:
    """サムネイルの非同期読み込みと LRU。request は画面のスレッドから呼ぶ。"""

    def __init__(self, call_soon: Callable[[Callable[[], None]], None], *, max_items: int = MAX_THUMBS, workers: int = 2,
                 loader: Callable[[str, int], Any] = load_qimage) -> None:
        self._call_soon = call_soon
        self._max = max_items
        self._loader = loader
        self._cache: OrderedDict[Key, Any] = OrderedDict()   # Key → QPixmap(None は読めなかった印)
        self._waiting: dict[Key, list[Callable[[Any], None]]] = {}
        self._pool: ThreadPoolExecutor | None = None
        self._workers = workers
        self._lock = threading.Lock()
        self._closed = False

    def __len__(self) -> int:
        return len(self._cache)

    def cached(self, key: Key) -> tuple[bool, Any]:
        if key in self._cache:
            self._cache.move_to_end(key)
            return True, self._cache[key]
        return False, None

    def request(self, path: str, mtime_ns: int, side: int, cb: Callable[[Any], None]) -> None:
        """読めたら cb(QPixmap | None) を画面のスレッドで呼ぶ。キャッシュにあればすぐ呼ぶ。"""
        key = (path, mtime_ns, side)
        hit, pm = self.cached(key)
        if hit:
            cb(pm)
            return
        if key in self._waiting:
            self._waiting[key].append(cb)
            return
        if self._closed:
            return
        self._waiting[key] = [cb]
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="twinsweep-thumb")
            fut: Future[Any] = self._pool.submit(self._loader, path, side)
        fut.add_done_callback(functools.partial(self._done, key))

    def _done(self, key: Key, fut: Future[Any]) -> None:
        try:
            qi = fut.result()
        except Exception:  # noqa: BLE001 - 取消・読み込み失敗は「読めない」
            qi = None
        if self._closed:
            return
        self._call_soon(lambda: self._deliver(key, qi))

    def _deliver(self, key: Key, qi: Any) -> None:
        from PySide6.QtGui import QPixmap

        pm = QPixmap.fromImage(qi) if qi is not None and not qi.isNull() else None
        self._cache[key] = pm
        self._cache.move_to_end(key)
        while len(self._cache) > self._max:
            self._cache.popitem(last=False)
        for cb in self._waiting.pop(key, []):
            try:
                cb(pm)
            except RuntimeError:
                pass  # 待っていた部品がもう消えている

    def load_once(self, path: str, side: int, cb: Callable[[Any], None]) -> None:
        """プレビュー用: キャッシュに入れずに1回だけ読む。"""
        if self._closed:
            return
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="twinsweep-thumb")
            fut: Future[Any] = self._pool.submit(self._loader, path, side)

        def done(f: Future[Any]) -> None:
            try:
                qi = f.result()
            except Exception:  # noqa: BLE001
                qi = None
            if self._closed:
                return

            def deliver() -> None:
                from PySide6.QtGui import QPixmap

                pm = QPixmap.fromImage(qi) if qi is not None and not qi.isNull() else None
                try:
                    cb(pm)
                except RuntimeError:
                    pass

            self._call_soon(deliver)

        fut.add_done_callback(done)

    def clear(self) -> None:
        self._cache.clear()

    def shutdown(self) -> None:
        self._closed = True
        self._waiting.clear()
        self._cache.clear()
        with self._lock:
            if self._pool is not None:
                self._pool.shutdown(wait=False, cancel_futures=True)
                self._pool = None
