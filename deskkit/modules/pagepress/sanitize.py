# 持ち込まない物の除去(P-5)と、書いたあとの検査(FR-19)。出力はいつも新しい PdfWriter に append で組んだ物(P-4)。
# 名前付きの行き先(/Names /Dests)は捨てるので、先にリンクの行き先をページの参照に直す(§8 の実測で必要と分かった)。
# 抜いたページを指すリンクは注釈ごと外す。検査はページ・注釈・しおり・入力欄の木を辿り、中身(/Resources・/Contents)は読まない。
from __future__ import annotations

from typing import Any

FORBIDDEN_ACTIONS = frozenset({"/JavaScript", "/Launch", "/SubmitForm", "/ImportData", "/GoToR", "/GoToE", "/Rendition"})
FORBIDDEN_ANNOTS = frozenset({"/FileAttachment", "/Sound", "/Movie", "/Screen", "/RichMedia", "/3D"})
ROOT_DROP = ("/Names", "/OpenAction", "/Metadata", "/PageLabels", "/AA", "/Dests")
PAGE_DROP = ("/AA", "/PieceInfo", "/Metadata")
_WALK_SKIP = frozenset({"/Parent", "/P", "/Resources", "/Contents", "/AP", "/Thumb", "/B", "/StructTreeRoot", "/Pg",
                        "/ParentTree", "/Font", "/XObject", "/ColorSpace", "/Pattern", "/Shading", "/ExtGState"})
MAX_WALK = 5_000_000


def _obj(v: Any) -> Any:
    try:
        return v.get_object()
    except AttributeError:
        return v


def _is_dict(v: Any) -> bool:
    from pypdf.generic import DictionaryObject

    return isinstance(v, DictionaryObject)


def _is_named(v: Any) -> bool:
    """行き先が名前(文字列か名前)か。"""
    from pypdf.generic import ArrayObject

    v = _obj(v)
    return v is not None and not isinstance(v, ArrayObject) and isinstance(v, (str, bytes))


# ------------------------------------------------------------------ 名前付きの行き先 → ページの参照
def _explicit(writer_page: Any, dest: Any) -> Any:
    from pypdf.generic import ArrayObject, FloatObject, NameObject, NullObject

    arr = ArrayObject([writer_page.indirect_reference])
    try:
        rest = list(dest.dest_array[1:])
    except Exception:  # noqa: BLE001 - 形の崩れた行き先は「ページ全体」にする
        rest = [NameObject("/Fit")]
    for v in rest:
        v = _obj(v)
        if isinstance(v, NameObject):
            arr.append(NameObject(str(v)))
        elif v is None or isinstance(v, NullObject):
            arr.append(NullObject())
        else:
            try:
                arr.append(FloatObject(float(v)))
            except (TypeError, ValueError):
                arr.append(NullObject())
    if len(arr) == 1:
        arr.append(NameObject("/Fit"))
    return arr


def resolve_named(writer: Any, reader: Any, offset: int, picked: list[int]) -> None:
    """いま足したページ(writer.pages[offset:offset+len(picked)])のリンクの名前付きの行き先を、ページの参照に直す。
    行き先のページが出力に無ければ、そのリンクを外す。"""
    from pypdf.generic import ArrayObject, NameObject

    try:
        named = reader.named_destinations
    except Exception:  # noqa: BLE001
        named = {}
    pos: dict[int, int] = {}
    for i, src in enumerate(picked):
        pos.setdefault(src, i)

    def target(name: Any) -> Any | None:
        key = str(_obj(name))
        d = named.get(key) or named.get(key.lstrip("/")) or named.get("/" + key)
        if d is None:
            return None
        try:
            src = reader.get_destination_page_number(d)
        except Exception:  # noqa: BLE001
            return None
        if src is None or src not in pos:
            return None
        return _explicit(writer.pages[offset + pos[src]], d)

    for k in range(len(picked)):
        page = writer.pages[offset + k]
        annots = _obj(page.get("/Annots"))
        if not isinstance(annots, ArrayObject):
            continue
        keep = ArrayObject()
        changed = False
        for a in annots:
            ao = _obj(a)
            if _is_dict(ao) and ao.get("/Subtype") == "/Link":
                if "/Dest" in ao and _is_named(ao["/Dest"]):
                    t = target(ao["/Dest"])
                    changed = True
                    if t is None:
                        continue
                    ao[NameObject("/Dest")] = t
                act = _obj(ao.get("/A"))
                if _is_dict(act) and act.get("/S") == "/GoTo" and _is_named(act.get("/D")):
                    t = target(act["/D"])
                    changed = True
                    if t is None:
                        continue
                    act[NameObject("/D")] = t
            keep.append(a)
        if changed:
            page[NameObject("/Annots")] = keep


# ------------------------------------------------------------------ 掃除(P-5)
def clean_action(a: Any) -> Any | None:
    """許す動作ならその動作(/Next の禁止分は外す)。禁止なら None。"""
    from pypdf.generic import ArrayObject, NameObject

    ao = _obj(a)
    if not _is_dict(ao):
        return None
    if ao.get("/S") in FORBIDDEN_ACTIONS or "/JS" in ao:
        return None
    nxt = _obj(ao.get("/Next"))
    if nxt is not None:
        if isinstance(nxt, ArrayObject):
            kept = ArrayObject([x for x in nxt if clean_action(x) is not None])
            if kept:
                ao[NameObject("/Next")] = kept
            else:
                del ao["/Next"]
        elif clean_action(nxt) is None:
            del ao["/Next"]
    return a


def _clean_holder(d: Any) -> None:
    """/AA を外し、/A の禁止の動作を外す(注釈・入力欄・しおり)。"""
    if "/AA" in d:
        del d["/AA"]
    if "/A" in d and clean_action(d["/A"]) is None:
        del d["/A"]


def _page_refs(writer: Any) -> set[int]:
    out: set[int] = set()
    for p in writer.pages:
        ref = p.indirect_reference
        if ref is not None:
            out.add(ref.idnum)
    return out


def _dest_ok(dest: Any, pages: set[int]) -> bool:
    from pypdf.generic import ArrayObject, IndirectObject

    d = _obj(dest)
    if not isinstance(d, ArrayObject) or not d:
        return False
    first = d[0]
    if isinstance(first, IndirectObject):
        return first.idnum in pages
    return not _is_dict(_obj(first)) and _obj(first) is not None  # 数のページ番号などは残す


def _clean_annots(page: Any, pages: set[int]) -> None:
    from pypdf.generic import ArrayObject, NameObject

    annots = _obj(page.get("/Annots"))
    if annots is None:
        return
    if not isinstance(annots, ArrayObject):
        del page["/Annots"]
        return
    keep = ArrayObject()
    for a in annots:
        ao = _obj(a)
        if not _is_dict(ao):
            continue
        if ao.get("/Subtype") in FORBIDDEN_ANNOTS:
            continue
        _clean_holder(ao)
        if ao.get("/Subtype") == "/Link":
            act = _obj(ao.get("/A"))
            if "/Dest" in ao:
                if not _dest_ok(ao["/Dest"], pages):
                    continue  # 抜いたページ・名前だけの行き先を指すリンク
            elif _is_dict(act):
                if act.get("/S") == "/GoTo" and not _dest_ok(act.get("/D"), pages):
                    continue
            else:
                continue  # 行き先も動作も無いリンク
        keep.append(a)
    if keep:
        page[NameObject("/Annots")] = keep
    else:
        del page["/Annots"]


def _walk_outline(first: Any, seen: set[int]) -> None:
    node = _obj(first)
    while _is_dict(node) and id(node) not in seen:
        seen.add(id(node))
        _clean_holder(node)
        if "/First" in node:
            _walk_outline(node["/First"], seen)
        node = _obj(node.get("/Next"))


def _walk_fields(fields: Any, seen: set[int]) -> None:
    from pypdf.generic import ArrayObject

    arr = _obj(fields)
    if not isinstance(arr, ArrayObject):
        return
    for f in arr:
        fo = _obj(f)
        if not _is_dict(fo) or id(fo) in seen:
            continue
        seen.add(id(fo))
        _clean_holder(fo)
        if "/Kids" in fo:
            _walk_fields(fo["/Kids"], seen)


def clean(writer: Any) -> None:
    """書く前の掃除(P-4・P-5)。"""
    root = writer.root_object
    for k in ROOT_DROP:
        if k in root:
            del root[k]
    af = _obj(root.get("/AcroForm"))
    if _is_dict(af):
        if "/XFA" in af:
            del af["/XFA"]
        _walk_fields(af.get("/Fields"), set())
    pages = _page_refs(writer)
    for page in writer.pages:
        for k in PAGE_DROP:
            if k in page:
                del page[k]
        _clean_annots(page, pages)
    ol = _obj(root.get("/Outlines"))
    if _is_dict(ol) and "/First" in ol:
        _walk_outline(ol["/First"], set())
    writer.metadata = None


# ------------------------------------------------------------------ 検査(FR-19)
def _violation(d: Any) -> str | None:
    if "/AA" in d:
        return "aa"
    if "/JS" in d:
        return "js"
    if d.get("/S") in FORBIDDEN_ACTIONS:
        return "action"
    if d.get("/Subtype") in FORBIDDEN_ANNOTS:
        return "annot"
    if d.get("/Type") == "/EmbeddedFile" or "/EF" in d:
        return "embedded"
    if d.get("/Type") == "/Page" and ("/PieceInfo" in d or "/Metadata" in d):
        return "page_info"
    return None


def find_violation(reader: Any) -> str | None:
    """P-5 の持ち込まない物が1つでもあれば理由のコード。無ければ None。"""
    from pypdf.generic import ArrayObject, IndirectObject

    tr = reader.trailer
    if "/Info" in tr:
        return "info"
    if "/Encrypt" in tr:
        return "encrypted"
    root = _obj(tr["/Root"])
    for k in ("/Names", "/OpenAction", "/Metadata", "/PageLabels", "/AA"):
        if k in root:
            return "root" + k.replace("/", "_").lower()
    af = _obj(root.get("/AcroForm"))
    if _is_dict(af) and "/XFA" in af:
        return "xfa"
    seen_ind: set[int] = set()
    seen_dir: set[int] = set()
    stack: list[Any] = [root]
    n = 0
    while stack:
        v = stack.pop()
        if isinstance(v, IndirectObject):
            if v.idnum in seen_ind:
                continue
            seen_ind.add(v.idnum)
            v = _obj(v)
        n += 1
        if n > MAX_WALK:
            return "too_deep"
        if _is_dict(v):
            if id(v) in seen_dir:
                continue
            seen_dir.add(id(v))
            bad = _violation(v)
            if bad:
                return bad
            for k in list(v.keys()):
                if k not in _WALK_SKIP:
                    stack.append(v.raw_get(k))
        elif isinstance(v, ArrayObject):
            stack.extend(list(v))
    return None


def verify(f: Any, expected_pages: int) -> str | None:
    """書いた一時ファイル(開いたファイル)を読み直して確かめる。合格なら None、落ちたら理由のコード。"""
    from deskkit.modules.pagepress.reader import quiet_pypdf_logging

    quiet_pypdf_logging()
    from pypdf import PdfReader

    try:
        r = PdfReader(f, strict=False)
        if r.is_encrypted:
            return "encrypted"
        if len(r.pages) != expected_pages:
            return "pages"
        return find_violation(r)
    except Exception:  # noqa: BLE001 - 読めない出力は不合格
        return "unreadable"
