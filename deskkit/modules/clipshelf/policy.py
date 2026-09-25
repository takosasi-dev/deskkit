# 記録可否の判定(純関数)。入力は形式名の集合・所有者 exe・設定・一時停止状態だけで、本文は受け取らない(FR-3)。
# 戻り値は §9.4 の理由コード。本文取得後に決まる duplicate / empty / too_large は monitor が判定する。
# 除外形式は §8.6 に従い、値の形式が実測で確定するまで「存在すれば除外」とする(安全側)。
from __future__ import annotations

from dataclasses import dataclass

from deskkit.modules.clipshelf._win32 import FMT_CAN_INCLUDE_HISTORY, FMT_EXCLUDE_MONITOR, FMT_ORIGIN

# §9.4 の語彙(これ以外を作らない)
RECORDED = "recorded"
OBSERVE_ONLY = "observe_only"
SELF_ORIGIN = "self_origin"
DUPLICATE = "duplicate"
EXCLUDE_FORMAT = "exclude_format"
HISTORY_DISALLOWED = "history_disallowed"
EXCLUDED_APP = "excluded_app"
OWNER_UNKNOWN = "owner_unknown"
NOT_TEXT = "not_text"
EMPTY = "empty"
TOO_LARGE = "too_large"
PAUSED = "paused"
CLIPBOARD_BUSY = "clipboard_busy"
RECORD = "record"  # decide() が返す「記録可」(ログには recorded / observe_only として出る)

REASON_CODES: tuple[str, ...] = (
    RECORDED, OBSERVE_ONLY, SELF_ORIGIN, DUPLICATE, EXCLUDE_FORMAT, HISTORY_DISALLOWED, EXCLUDED_APP,
    OWNER_UNKNOWN, NOT_TEXT, EMPTY, TOO_LARGE, PAUSED, CLIPBOARD_BUSY,
)
# 「除外」として数える理由(画面の今日の除外件数)
EXCLUSION_REASONS: frozenset[str] = frozenset({
    EXCLUDE_FORMAT, HISTORY_DISALLOWED, EXCLUDED_APP, OWNER_UNKNOWN, NOT_TEXT, EMPTY, TOO_LARGE, PAUSED, CLIPBOARD_BUSY,
})
TEXT_FORMAT_NAME = "CF_UNICODETEXT"


@dataclass(frozen=True)
class ClipFacts:
    """本文を含まない、判定の材料。"""

    formats: frozenset[str]  # 形式名(標準形式は CF_* 名、登録形式は登録名)
    owner_exe: str | None    # 小文字の exe ファイル名。取れなければ None(推測しない)


@dataclass(frozen=True)
class PolicyConfig:
    exclude_exes: frozenset[str]
    unknown_owner_policy: str  # "skip" | "record"


def decide(facts: ClipFacts, config: PolicyConfig, paused: bool) -> str:
    """RECORD か除外理由コードを返す。本文は引数にとらない(FR-3 / D-2)。"""
    if paused:
        return PAUSED
    if FMT_ORIGIN in facts.formats:
        return SELF_ORIGIN
    if FMT_EXCLUDE_MONITOR in facts.formats:
        return EXCLUDE_FORMAT
    if FMT_CAN_INCLUDE_HISTORY in facts.formats:
        # §8.6: 値の形式(DWORD 等)が実測で確定するまで、存在すれば除外する。
        return HISTORY_DISALLOWED
    if TEXT_FORMAT_NAME not in facts.formats:
        return NOT_TEXT
    if facts.owner_exe is None:
        return OWNER_UNKNOWN if config.unknown_owner_policy != "record" else RECORD
    if facts.owner_exe.lower() in config.exclude_exes:
        return EXCLUDED_APP
    return RECORD
