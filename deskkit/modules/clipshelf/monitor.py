# クリップボード更新の処理本体(デバウンス後に1回呼ばれる)。OpenClipboard を再試行し、
# ① 形式一覧と所有者だけで policy.decide() → ② 記録可のときだけ CF_UNICODETEXT を読み、即 CloseClipboard(D-2 / INV-3)。
# 閉じた後に empty / too_large / duplicate を判定して暗号化保存する。ログは理由コード・形式名・所有者 exe・ID・件数だけ。
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PureWindowsPath

from deskkit.modules.clipshelf import policy
from deskkit.modules.clipshelf._win32 import FMT_ORIGIN, Win32Api
from deskkit.modules.clipshelf.config import Config
from deskkit.modules.clipshelf.crypto import CryptoError
from deskkit.modules.clipshelf.ops import OpsLog
from deskkit.modules.clipshelf.store import Store, StoreError
from deskkit.modules.clipshelf.writer import decode_origin


@dataclass(frozen=True)
class Decision:
    """画面に出してよい判定記録(本文・文字数を持たない)。"""

    ts: datetime
    reason: str
    owner_exe: str | None
    formats: tuple[str, ...]
    item_id: int | None = None


class ClipMonitor:
    def __init__(self, api: Win32Api, *, owner_hwnd: Callable[[], int], config: Callable[[], Config],
                 store: Callable[[], Store | None], paused: Callable[[], bool], log: logging.Logger, ops: OpsLog,
                 now: Callable[[], datetime], sleep: Callable[[float], None] = time.sleep,
                 on_decision: Callable[[Decision], None] | None = None) -> None:
        self._api = api
        self._hwnd = owner_hwnd
        self._config = config
        self._store = store
        self._paused = paused
        self._log = log
        self._ops = ops
        self._now = now
        self._sleep = sleep
        self._on_decision = on_decision
        self._startup_seq: int | None = None
        self._last_seq: int | None = None
        self._fmt_names: dict[int, str] = {}
        self._origin_fmt = 0

    # ------------------------------------------------------------ 起動時(D-15)
    def mark_startup(self) -> None:
        """起動時点の内容は記録しない。その時点のシーケンス番号を覚え、同じ番号の更新は無視する。"""
        self._startup_seq = self._api.sequence_number()
        self._last_seq = self._startup_seq
        self._origin_fmt = self._api.register_format(FMT_ORIGIN)

    # ------------------------------------------------------------ 本体
    def process(self) -> Decision | None:
        seq = self._api.sequence_number()
        if self._startup_seq is not None and seq == self._startup_seq:
            self._log.debug("clip update ignored: startup content (D-15)")
            return None
        if seq == self._last_seq and seq != 0:
            return None  # 同じ内容の重複通知
        self._last_seq = seq
        cfg = self._config()

        if not self._open(cfg.open_retry):
            return self._finish(policy.CLIPBOARD_BUSY, None, ())

        text: str | None = None
        origin_id: int | None = None
        try:
            ids = self._api.enum_formats()
            names = tuple(self._name(f) for f in ids)
            owner = self._owner_exe()
            facts = policy.ClipFacts(frozenset(names), owner)
            pcfg = policy.PolicyConfig(cfg.exclude_exes, cfg.unknown_owner_policy)
            reason = policy.decide(facts, pcfg, self._paused())
            if reason == policy.SELF_ORIGIN and self._origin_fmt:
                origin_id = decode_origin(self._api.get_data_bytes(self._origin_fmt))
            elif reason == policy.RECORD and cfg.mode == "record":
                # ② 記録可のときだけ本文を取得(INV-3)。max_chars+1 文字まで読めば超過を判定できる
                text = self._api.get_unicode_text(cfg.max_chars)
        finally:
            self._api.close_clipboard()  # 取得直後に閉じる(FR-4)

        if reason == policy.SELF_ORIGIN:
            touched = None
            st = self._store()
            if cfg.mode == "record" and origin_id is not None and st is not None and st.is_open:
                try:
                    if st.touch(origin_id):
                        touched = origin_id
                except StoreError as e:
                    self._log.error("store error on touch: %s", e)
            return self._finish(policy.SELF_ORIGIN, owner, names, touched)
        if reason != policy.RECORD:
            return self._finish(reason, owner, names)
        if cfg.mode != "record":
            return self._finish(policy.OBSERVE_ONLY, owner, names)
        return self._record(text, owner, names, cfg)

    def _record(self, text: str | None, owner: str | None, names: tuple[str, ...], cfg: Config) -> Decision:
        if text is None or not text.strip():
            return self._finish(policy.EMPTY, owner, names)
        if len(text) > cfg.max_chars:
            text = None  # 切り詰めて記録しない(FR-5)
            return self._finish(policy.TOO_LARGE, owner, names)
        st = self._store()
        if st is None or not st.is_open:
            self._log.warning("clip not recorded: store unavailable")
            return self._finish_unrecorded(owner, names)
        latest = st.latest_history()
        try:
            if latest is not None and latest.text == text:
                st.touch(latest.id)  # 直前と同じなら新規行を作らない(FR-6)
                return self._finish(policy.DUPLICATE, owner, names, latest.id)
            # C3: 短命記録の対象アプリ(exe 名だけで判定。内容は見ない)なら期限を payload に入れる
            expires = (self._now() + timedelta(minutes=cfg.short_lived_minutes)
                       if owner is not None and owner in cfg.short_lived_exes else None)
            item = st.add_history(text, owner, expires)
        except CryptoError as e:
            self._log.error("encrypt failed (not recorded): win32 error %d", e.code)
            return self._finish_unrecorded(owner, names)
        except StoreError as e:
            self._log.error("store error (not recorded): %s", e)
            return self._finish_unrecorded(owner, names)
        finally:
            text = None
        dec = self._finish(policy.RECORDED, owner, names, item.id)
        self.trim(cfg)
        return dec

    def trim(self, cfg: Config) -> int:
        st = self._store()
        if st is None or not st.is_open:
            return 0
        try:
            n = st.trim(cfg.max_items, cfg.max_days)
        except StoreError as e:
            self._log.error("retention trim failed: %s", e)
            return 0
        if n:
            self._ops.write("retention_trim", deleted=n)
            self._log.info("retention_trim deleted=%d", n)
        return n

    # ------------------------------------------------------------ 補助
    def _open(self, retry: int) -> bool:
        n = max(1, retry)
        for attempt in range(n):
            if self._api.open_clipboard(self._hwnd()):
                return True
            if attempt + 1 < n:
                self._sleep(0.015 * (attempt + 1))
        return False

    def _name(self, fmt: int) -> str:
        name = self._fmt_names.get(fmt)
        if name is None:
            name = self._api.format_name(fmt)
            if fmt >= 0xC000:  # 登録形式の ID はセッション中は変わらない
                self._fmt_names[fmt] = name
        return name

    def _owner_exe(self) -> str | None:
        hwnd = self._api.clipboard_owner()
        if not hwnd:
            return None
        pid = self._api.window_pid(hwnd)
        if not pid:
            return None
        path = self._api.process_image_path(pid)
        if not path:
            return None  # 昇格プロセス等で取れない。推測しない(§10)
        return PureWindowsPath(path).name.lower()

    def _finish(self, reason: str, owner: str | None, names: tuple[str, ...], item_id: int | None = None) -> Decision:
        dec = Decision(self._now(), reason, owner, names, item_id)
        if reason in policy.EXCLUSION_REASONS or reason == policy.OBSERVE_ONLY:
            self._ops.write("clip", reason=reason)  # 利用状況(日ごとの除外件数)用。理由コードだけ
        self._log.info("clip decision reason=%s owner=%s formats=[%s]%s", reason, owner or "(不明)", ", ".join(names),
                       f" item={item_id}" if item_id is not None else "")
        if self._on_decision is not None:
            self._on_decision(dec)
        return dec

    def _finish_unrecorded(self, owner: str | None, names: tuple[str, ...]) -> Decision:
        # 理由コードの語彙(§9.4)を増やさないため、記録失敗は判定履歴に載せずログ(呼び出し元で出力済み)だけにする
        return Decision(self._now(), "", owner, names, None)
