# Windows.Media.Ocr で文字と位置を取り、個人情報らしい文字列の「候補」の枠を返す(FR-11・FR-13・S-7)。
# 読んだ文字列はこの関数の中だけで使い、候補には種類と枠しか持たせない。ファイル・ログ・設定には書かない(INV-4)。
# winrt は最初に使うときに import する(NFR-5)。候補の判定(正規表現)は文字列だけで単体テストできる形にする。
from __future__ import annotations

import re
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from PIL import Image

KIND_LABELS = {
    "email": "メールアドレス",
    "phone": "電話番号",
    "postal": "郵便番号",
    "user": "ユーザー名",
    "word": "登録した言葉",
}
PREFERRED_LANGS = ("ja", "en")


class OcrUnavailableError(Exception):
    pass


@dataclass(frozen=True)
class Word:
    text: str
    rect: tuple[float, float, float, float]  # x, y, w, h(画像の座標)


@dataclass(frozen=True)
class Line:
    words: tuple[Word, ...]


@dataclass(frozen=True)
class Candidate:
    kind: str
    rect: tuple[int, int, int, int]  # 文字は持たない(INV-4)

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)


# ------------------------------------------------------------------ 文字列の正規化(1文字→1文字で、位置がずれないようにする)
_DASHES = "ー－−―‐‑–—ｰ"


def normalize(text: str) -> str:
    out = []
    for ch in text:
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E:  # 全角の英数字・記号
            ch = chr(o - 0xFEE0)
        elif ch == "　":
            ch = " "
        elif ch in _DASHES:
            ch = "-"
        out.append(ch)
    return "".join(out)


_SEP = r"[-\s]\s?"
# 名前の部分には、文字認識が r / l を読み違えやすい「」| も入れる(実測: "user" が "use「" になる)
_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+\-「」|])[A-Za-z0-9._%+\-「」|]*[A-Za-z0-9][A-Za-z0-9._%+\-「」|]*@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_PHONES = [
    # +81(文字認識で「十」と読まれることがある)
    re.compile(r"(?<![\d])[+十]\s?81[-\s]?\s?(?:\(0\)\s?)?\d{1,4}" + _SEP + r"\d{1,4}" + _SEP + r"\d{3,4}(?![\d])"),
    re.compile(r"(?<![\d])[+十]\s?81\d{9,10}(?![\d])"),
    # 国内: 区切り2つ・かっこ・区切り無し
    re.compile(r"(?<![\d+])0\d{1,4}" + _SEP + r"\d{1,4}" + _SEP + r"\d{4}(?![\d])"),
    re.compile(r"(?<![\d+])\(0\d{1,4}\)\s?\d{1,4}[-\s]?\d{4}(?![\d])"),
    re.compile(r"(?<![\d+])0\d{1,4}\(\d{1,4}\)\d{4}(?![\d])"),
    re.compile(r"(?<![\d+])0\d{9,10}(?![\d])"),
]
_POSTAL = [
    re.compile(r"〒\s?\d{3}[-\s]?\s?\d{4}(?![\d])"),
    re.compile(r"(?<![\d\-])\d{3}-\d{4}(?![\d\-])"),
]
_USER = re.compile(r"(?<![A-Za-z0-9._%+\-@])@[A-Za-z0-9_](?:[A-Za-z0-9_.]{0,30}[A-Za-z0-9_])?(?![A-Za-z0-9_@])")


def _phone_ok(s: str) -> bool:
    digits = re.sub(r"\D", "", s)
    if s.lstrip()[:1] in "+十":
        rest = digits[2:]
        if rest.startswith("0"):
            rest = rest[1:]
        return 9 <= len(rest) <= 10
    return digits.startswith("0") and 10 <= len(digits) <= 11


def find_spans(text: str, my_words: Sequence[str] = ()) -> list[tuple[str, int, int]]:
    """文字列から (種類, 始まり, 終わり) の一覧。text は normalize 済みでなくてもよい(中で正規化する)。"""
    t = normalize(text)
    spans: list[tuple[str, int, int]] = []
    for m in _EMAIL.finditer(t):
        spans.append(("email", m.start(), m.end()))
    taken: list[tuple[int, int]] = []
    for rx in _PHONES:
        for m in rx.finditer(t):
            if any(m.start() < e and s < m.end() for s, e in taken) or not _phone_ok(m.group(0)):
                continue
            taken.append((m.start(), m.end()))
            spans.append(("phone", m.start(), m.end()))
    for rx in _POSTAL:
        for m in rx.finditer(t):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            taken.append((m.start(), m.end()))
            spans.append(("postal", m.start(), m.end()))
    for m in _USER.finditer(t):
        spans.append(("user", m.start(), m.end()))
    low = t.casefold()
    for w in my_words:
        nw = normalize(w).casefold().strip()
        if not nw:
            continue
        start = 0
        while True:
            i = low.find(nw, start)
            if i < 0:
                break
            spans.append(("word", i, i + len(nw)))
            start = i + max(1, len(nw))
    return spans


def _join(words: Sequence[Word], sep: str) -> tuple[str, list[int]]:
    """単語をつないだ文字列と、各文字がどの単語か(区切りは -1)。"""
    parts: list[str] = []
    owner: list[int] = []
    for i, w in enumerate(words):
        if i and sep:
            parts.append(sep)
            owner.extend([-1] * len(sep))
        parts.append(w.text)
        owner.extend([i] * len(w.text))
    return "".join(parts), owner


def find_candidates(lines: Iterable[Line], my_words: Sequence[str] = (), pad: int = 3) -> list[Candidate]:
    """行ごとに、空白でつないだ文字列と空白なしでつないだ文字列の両方を調べ、当たった単語の枠を合わせて候補にする。"""
    found: list[tuple[str, frozenset[int], int]] = []  # (種類, 単語の集合, 行番号)
    words_by_line: list[tuple[Word, ...]] = []
    for li, line in enumerate(lines):
        words_by_line.append(line.words)
        if not line.words:
            continue
        seen: set[tuple[str, frozenset[int]]] = set()
        for sep in (" ", ""):
            text, owner = _join(line.words, sep)
            for kind, s, e in find_spans(text, my_words):
                idx = frozenset(o for o in owner[s:e] if o >= 0)
                if idx and (kind, idx) not in seen:
                    seen.add((kind, idx))
                    found.append((kind, idx, li))
    # 同じ行で、別の候補の単語に完全に含まれるものは出さない(郵便番号と電話番号の重なり・登録語とメールなど)
    keep: list[tuple[str, frozenset[int], int]] = []
    for i, (k, idx, li) in enumerate(found):
        dominated = any(j != i and lj == li and idx <= idx2 and (idx < idx2 or j < i) for j, (_k2, idx2, lj) in enumerate(found))
        if not dominated:
            keep.append((k, idx, li))
    out: list[Candidate] = []
    for k, idx, li in keep:
        ws = [words_by_line[li][i] for i in sorted(idx)]
        x0 = min(w.rect[0] for w in ws) - pad
        y0 = min(w.rect[1] for w in ws) - pad
        x1 = max(w.rect[0] + w.rect[2] for w in ws) + pad
        y1 = max(w.rect[1] + w.rect[3] for w in ws) + pad
        out.append(Candidate(k, (int(max(0, x0)), int(max(0, y0)), int(x1 - max(0, x0)) + 1, int(y1 - max(0, y0)) + 1)))
    return out


# ------------------------------------------------------------------ Windows の文字認識
_avail_lock = threading.Lock()
_avail: tuple[bool, str] | None = None


def _pick_language() -> Any:
    from winrt.windows.globalization import Language
    from winrt.windows.media.ocr import OcrEngine

    tags = [lang.language_tag for lang in OcrEngine.available_recognizer_languages]
    for pref in PREFERRED_LANGS:
        for tag in tags:
            if tag.lower() == pref or tag.lower().startswith(pref + "-"):
                return Language(tag)
    return None


def availability(refresh: bool = False) -> tuple[bool, str]:
    """(使えるか, 理由コード)。理由: ok / import_failed / no_language。結果は覚えておく。"""
    global _avail
    with _avail_lock:
        if _avail is not None and not refresh:
            return _avail
        try:
            lang = _pick_language()
            _avail = (True, "ok") if lang is not None else (False, "no_language")
        except Exception:  # noqa: BLE001 - winrt の読み込み失敗は「使えない」とだけ扱う(FR-13)
            _avail = (False, "import_failed")
        return _avail


def recognize(img: Image.Image) -> list[Line]:
    """画像の文字と位置を読む。使えないときは OcrUnavailableError。GUI スレッドの外から呼ぶ。"""
    ok, reason = availability()
    if not ok:
        raise OcrUnavailableError(reason)
    import asyncio

    from PIL import Image as PILImage

    try:
        from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import DataWriter

        lang = _pick_language()
        engine = OcrEngine.try_create_from_language(lang) if lang is not None else None
        if engine is None:
            raise OcrUnavailableError("no_language")
        src = img
        scale = 1.0
        limit = int(OcrEngine.max_image_dimension) or 4096
        if max(src.size) > limit:
            scale = limit / max(src.size)
            src = src.resize((max(1, int(src.width * scale)), max(1, int(src.height * scale))), PILImage.Resampling.BILINEAR)
        rgba = src.convert("RGBA")
        r, g, b, a = rgba.split()
        bgra = PILImage.merge("RGBA", (b, g, r, a)).tobytes()
        dw = DataWriter()
        dw.write_bytes(bgra)
        sb = SoftwareBitmap.create_copy_with_alpha_from_buffer(dw.detach_buffer(), BitmapPixelFormat.BGRA8,
                                                               rgba.width, rgba.height, BitmapAlphaMode.PREMULTIPLIED)

        async def _run() -> Any:
            return await engine.recognize_async(sb)

        result = asyncio.run(_run())
        inv = 1.0 / scale
        lines: list[Line] = []
        for ln in result.lines:
            ws = []
            for w in ln.words:
                br = w.bounding_rect
                ws.append(Word(str(w.text), (br.x * inv, br.y * inv, br.width * inv, br.height * inv)))
            lines.append(Line(tuple(ws)))
        return lines
    except OcrUnavailableError:
        raise
    except Exception as e:  # noqa: BLE001 - 認識の失敗は「使えない」と同じ扱いにする(手で囲める)
        raise OcrUnavailableError("recognize_failed") from e
