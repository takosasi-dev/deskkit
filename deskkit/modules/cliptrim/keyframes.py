# T の「まわりの読み取り」(T-3・P-1): `-ss T -copyts -i <入力> -map 0:V:0 -c copy -f framecrc pipe:1` を1行ずつ読み、
# T の直前の切れ目(キーフレーム)から、T より後の最初の切れ目(の分のコマが出そろうまで)か T+60 秒で止める。全体は読まない。
# 画質そのままの実際の開始(T-5)もここ。位置はすべて「pts × 時間の単位 − 開始時刻」の秒。
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from deskkit.modules.cliptrim import runner

HORIZON_S = 60.0     # T の後、ここまでに切れ目が無ければ止める(FR-4)
FAR_S = 120.0        # 直前の切れ目が T よりこれ以上前なら、T まで読まずに止める(§10)
TIMEOUT_S = 30.0     # まわりの読み取りの時間切れ(FR-13)
EPS = 1e-6
NOPTS = -9223372036854775808

_TB = re.compile(r"^#tb 0:\s*(\d+)/(\d+)")
_FLAGS = re.compile(r"F=0x([0-9a-fA-F]+)")


@dataclass(frozen=True)
class Packet:
    dts: int
    pts: int
    dur: int
    key: bool


def parse_tb(line: str) -> Fraction | None:
    m = _TB.match(line.strip())
    if not m or int(m.group(2)) == 0:
        return None
    return Fraction(int(m.group(1)), int(m.group(2)))


def parse_line(line: str) -> Packet | None:
    """framecrc の1行(`ストリーム, dts, pts, 長さ, 大きさ, crc[, F=0x..][, 付加情報…]`)。ストリーム 0 以外と見出しは None。
    切れ目は F= が無いか、F の最下位ビットが 1(AV_PKT_FLAG_KEY)。"""
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    parts = [p.strip() for p in s.split(",")]
    if len(parts) < 6 or parts[0] != "0":
        return None
    try:
        dts, pts, dur = int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        return None
    key = True
    for p in parts[6:]:
        m = _FLAGS.fullmatch(p)
        if m:
            key = bool(int(m.group(1), 16) & 1)
            break
    if pts == NOPTS:
        pts = dts
    if dts == NOPTS:
        dts = pts
    return Packet(dts, pts, dur, key)


@dataclass(frozen=True)
class Key:
    pos: float     # 切れ目の位置(秒)
    lead: float    # pts − dts(秒)。MKV の試し直しに使う(T-5)


@dataclass(frozen=True)
class Around:
    t: float                                  # 読みたかった位置
    first: Key | None                         # 読み始めの切れ目(-ss で着いた所)
    keys: tuple[Key, ...]                     # 読んだ範囲の切れ目(位置の順)
    frames: tuple[tuple[float, float], ...]   # (位置, 長さ) の位置の順。first から next_key の手前までは欠けない
    next_key: float | None                    # t より後の最初の切れ目
    far: bool = False                         # 直前の切れ目が遠すぎて T まで読まなかった
    horizon: bool = False                     # T+60 秒まで切れ目が無かった
    eof: bool = False                         # 終わりまで読んだ

    def covers(self, pos: float) -> bool:
        """pos の前後のコマをこの読み取りだけで決められるか。"""
        if self.far or self.first is None or pos < self.first.pos - EPS:
            return False
        if self.next_key is not None:
            return pos < self.next_key - EPS
        return self.eof or (bool(self.frames) and pos < self.frames[-1][0] - EPS)

    def frame_at(self, t: float) -> tuple[float, float] | None:
        """t に表示されるコマ(pts が t 以上の最初のコマ)。"""
        for f in self.frames:
            if f[0] >= t - EPS:
                return f
        return None

    def next_frame(self, pos: float) -> tuple[float, float] | None:
        for f in self.frames:
            if f[0] > pos + EPS:
                return f
        return None

    def prev_frame(self, pos: float) -> tuple[float, float] | None:
        best = None
        for f in self.frames:
            if f[0] < pos - EPS:
                best = f
            else:
                break
        return best

    def prev_key(self, pos: float) -> Key | None:
        """pos より前の切れ目(pos ちょうどの切れ目は含まない)。"""
        best = None
        for k in self.keys:
            if k.pos < pos - EPS:
                best = k
        return best

    def key_at_or_before(self, pos: float) -> Key | None:
        best = None
        for k in self.keys:
            if k.pos <= pos + EPS:
                best = k
        return best

    def duration_of(self, pos: float) -> float | None:
        for p, d in self.frames:
            if abs(p - pos) <= EPS:
                return d if d > 0 else None
        return None


@dataclass
class Scanner:
    """framecrc の行を順に受け取り、止めてよいかを返す(feed が False で止める)。"""

    t: float
    start: float
    first_only: bool = False
    horizon_s: float = HORIZON_S
    far_s: float = FAR_S
    tb: Fraction | None = None
    first: Key | None = None
    keys: list[Key] = field(default_factory=list)
    frames: list[tuple[float, float]] = field(default_factory=list)
    next_key: float | None = None
    far: bool = False
    horizon: bool = False
    done: bool = False

    def _sec(self, v: int) -> float:
        assert self.tb is not None
        return float(v * self.tb) - self.start

    def feed(self, line: str) -> bool:
        if self.done:
            return False
        if self.tb is None:
            tb = parse_tb(line)
            if tb is not None:
                self.tb = tb
            return True
        pk = parse_line(line)
        if pk is None:
            return True
        pos = self._sec(pk.pts)
        dpos = self._sec(pk.dts)
        dur = float(pk.dur * self.tb) if pk.dur > 0 else 0.0
        if self.first is None:
            if not pk.key:
                return True  # 読み始めは切れ目のはず(-c copy は最初の切れ目の前を落とす)。念のため飛ばす
            self.first = Key(pos, float((pk.pts - pk.dts) * self.tb))
            if self.first_only:
                self.keys.append(self.first)
                self.frames.append((pos, dur))
                self.done = True
                return False
            if pos < self.t - self.far_s:
                self.far = True
                self.keys.append(self.first)
                self.done = True
                return False
        if self.next_key is not None and dpos >= self.next_key - EPS:
            # これより後の包みは pts ≥ dts ≥ 次の切れ目なので、次の切れ目より前のコマは出そろった
            self.done = True
            return False
        if dpos > self.t + self.horizon_s:
            self.horizon = True
            self.done = True
            return False
        self.frames.append((pos, dur))
        if pk.key:
            self.keys.append(Key(pos, float((pk.pts - pk.dts) * self.tb)))
            if self.next_key is None and pos > self.t + EPS:
                self.next_key = pos
        return True

    def result(self, eof: bool) -> Around:
        frames = sorted(set(self.frames))
        keys = sorted(set(self.keys), key=lambda k: k.pos)
        return Around(self.t, self.first, tuple(keys), tuple(frames), self.next_key, self.far, self.horizon,
                      eof and not self.done)


def fmt_ss(x: float) -> str:
    """-ss に渡す文字列(マイクロ秒まで。値そのものの丸めは呼ぶ側で決める)。"""
    return f"{max(0.0, x):.6f}"


def floor_us(x: float) -> float:
    """マイクロ秒で切り捨て(T-4。切り上げると狙ったコマを飛ばす)。"""
    return math.floor(x * 1_000_000 + 1e-7) / 1_000_000


def round_us(x: float) -> float:
    """マイクロ秒で四捨五入(T-5)。"""
    return round(x * 1_000_000) / 1_000_000


def around_args(exe: Path, src: Path, ss: float) -> list[str]:
    return [str(exe), "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", fmt_ss(ss), "-copyts",
            "-protocol_whitelist", "file,pipe", "-i", runner.arg_path(src), "-map", "0:V:0", "-c", "copy",
            "-f", "framecrc", "pipe:1"]


class ReadError(Exception):
    """読み取れなかった(時間切れ・ffmpeg の失敗)。中止は Cancelled。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ReadCancelledError(Exception):
    pass


def read(exe: Path, src: Path, t: float, start: float, *, ss: float | None = None, first_only: bool = False,
         token: runner.Token | None = None, timeout: float = TIMEOUT_S, popen: runner.Popen | None = None) -> Around:
    """位置 t のまわりを読む。ss を渡せばその値を -ss に使う(省けば t)。"""
    sc = Scanner(t, start, first_only=first_only)
    cap = runner.stream(around_args(exe, src, t if ss is None else ss), sc.feed, timeout=timeout, token=token,
                        popen=popen)
    if cap.cancelled:
        raise ReadCancelledError()
    if cap.timed_out:
        raise ReadError("timeout")
    if cap.returncode is None:
        raise ReadError("spawn")
    res = sc.result(eof=True)
    if not sc.done and cap.returncode != 0 and not res.frames:
        raise ReadError(f"exit_{cap.returncode}")
    return res


# ------------------------------------------------------------------ 画質そのままの実際の開始(T-5)
@dataclass(frozen=True)
class CopyStart:
    ss: float       # -ss に渡す値
    actual: float   # 出力が実際に始まる位置(見え始めるコマ)
    retried: bool = False
    hidden_from: float | None = None  # MP4・MOV で K より前の切れ目から入り、編集リストで隠れる部分の始まり(CT-3)


def resolve_copy_start(exe: Path, src: Path, s: float, start: float, *, matroska: bool,
                       token: runner.Token | None = None, popen: runner.Popen | None = None) -> CopyStart:
    """区間の開始 s に対し、s 以前の最後の切れ目 K から切るときの -ss と実際の開始を決める(T-5)。
    同じ -ss で読み直した最初の切れ目が K でなく、入れ物が MKV・WebM なら K+(K の pts−dts) で1回だけ試し直し、
    それでも K でなければ着いた切れ目を実際の開始にする。MP4・MOV は試し直さない。-ss が K ちょうどなので、
    K より前の切れ目に着いても K の前は編集リストで隠れ、見え始めは K になる(CT-3。docs/v0.4/cliptrim.md)。"""
    a = read(exe, src, s, start, token=token, popen=popen)
    k = a.key_at_or_before(s) or a.first
    if k is None:
        raise ReadError("no_key")
    ss = round_us(k.pos)
    b = read(exe, src, ss, start, ss=ss, first_only=True, token=token, popen=popen)
    if b.first is None:
        raise ReadError("no_key")
    if abs(b.first.pos - k.pos) <= 1e-4:
        return CopyStart(ss, k.pos)
    if not matroska:
        return CopyStart(ss, k.pos, hidden_from=b.first.pos)
    if 0 < k.lead <= 1.0:
        ss2 = round_us(k.pos + k.lead)
        c = read(exe, src, ss2, start, ss=ss2, first_only=True, token=token, popen=popen)
        if c.first is not None:
            return CopyStart(ss2, k.pos if abs(c.first.pos - k.pos) <= 1e-4 else c.first.pos, True)
    return CopyStart(ss, b.first.pos)
