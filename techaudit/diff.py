"""Сравнение двух вариантов одной страницы: какие блоки появились/исчезли.

Блок определяется по стабильным маркерам, а не по хэшированным CSS-классам:
data-qa / data-testid, id, семантические теги (header, nav, main, footer…),
role и itemtype. Числа в маркерах схлопываются (product-card-4280098 →
product-card-*), поэтому карточки товаров считаются как один тип блока
с N экземплярами.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin

from lxml import html as LH

SKIP_TAGS = {"script", "style", "noscript", "template", "head", "svg", "iframe", "meta", "link"}
BLOCK_TAGS = {"header", "nav", "main", "aside", "footer", "section", "article", "form", "dialog"}
ROLE_BLOCKS = {"navigation", "banner", "contentinfo", "main", "complementary", "search"}
# служебные узлы фреймворков: есть только в отрендеренном DOM и не относятся к контенту
IGNORE_KEY = re.compile(r"next-route-announcer|chakra-portal|chakra-toast|^#__", re.I)
LINK_SKIP = re.compile(r"^(javascript:|mailto:|tel:|#|data:)", re.I)


def _norm_key(s: str) -> str:
    s = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "*", s, flags=re.I)
    return re.sub(r"\d{3,}", "*", s.strip())


def marker(el) -> str | None:
    for attr in ("data-qa", "data-testid", "data-kit-qa"):
        v = el.get(attr)
        if v:
            return "qa:" + _norm_key(v)
    i = el.get("id")
    if i and not i.startswith("__") and ":" not in i:
        return "#" + _norm_key(i)
    if el.tag in BLOCK_TAGS:
        return el.tag
    if el.get("role") in ROLE_BLOCKS:
        return "role:" + el.get("role")
    it = el.get("itemtype")
    if it:
        return "itemtype:" + it.rstrip("/").rsplit("/", 1)[-1]
    return None


@dataclass
class BlockStat:
    instances: int = 0
    chars: int = 0  # собственный текст блока (без вложенных маркированных блоков)
    links: int = 0
    imgs: int = 0
    parent: str | None = None
    samples: list[str] = field(default_factory=list)

    @property
    def weight(self) -> int:
        return self.chars + 20 * self.links + 20 * self.imgs


@dataclass
class Doc:
    head: dict = field(default_factory=dict)
    blocks: dict[str, BlockStat] = field(default_factory=dict)
    links: set[str] = field(default_factory=set)
    text: Counter = field(default_factory=Counter)  # нормализованные фрагменты текста
    structured: dict = field(default_factory=dict)

    @property
    def text_chars(self) -> int:
        return sum(len(t) * c for t, c in self.text.items())


def decode_html(body: bytes | str, content_type: str | None = None) -> str:
    if isinstance(body, str):
        return body
    enc = None
    if content_type:
        m = re.search(r"charset=([\w-]+)", content_type, re.I)
        enc = m.group(1) if m else None
    if not enc:
        m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.I)
        enc = m.group(1).decode() if m else "utf-8"
    try:
        return body.decode(enc, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _ws(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _jsonld_types(node, out: Counter) -> None:
    if isinstance(node, list):
        for n in node:
            _jsonld_types(n, out)
    elif isinstance(node, dict):
        t = node.get("@type")
        for x in ([t] if isinstance(t, str) else t or []):
            out[str(x)] += 1
        for v in node.values():
            if isinstance(v, (dict, list)):
                _jsonld_types(v, out)


def parse_doc(body: bytes | str, base_url: str, content_type: str | None = None) -> Doc:
    text = decode_html(body, content_type)
    # убираем комментарии-шапку выгрузки (SOURCE_URL…) — они не часть страницы
    text = re.sub(r"^(\s*<!--.*?-->\s*)+", "", text, flags=re.S)
    doc = Doc()
    if not text.strip():
        return doc
    root = LH.document_fromstring(text)
    xp = root.xpath

    def first(q):
        r = xp(q)
        return _ws(r[0] if isinstance(r[0], str) else r[0].text_content()) if r else None

    doc.head = {
        "title": first("//head/title"),
        "description": first("//meta[@name='description']/@content"),
        "canonical": first("//link[@rel='canonical']/@href"),
        "robots": first("//meta[@name='robots']/@content"),
        "viewport": first("//meta[@name='viewport']/@content"),
        "lang": first("/html/@lang"),
        "h1": [_ws(h.text_content()) for h in xp("//body//h1")],
        "og": sorted(f"{m.get('property')}={_ws(m.get('content'))}" for m in xp("//meta[starts-with(@property,'og:')]")),
        "twitter": sorted(f"{m.get('name')}={_ws(m.get('content'))}" for m in xp("//meta[starts-with(@name,'twitter:')]")),
        "hreflang": len(xp("//link[@rel='alternate'][@hreflang]")),
    }

    jl, bad = Counter(), 0
    for s in xp("//script[@type='application/ld+json']"):
        try:
            _jsonld_types(json.loads(s.text or ""), jl)
        except ValueError:
            bad += 1
    md = Counter(i.rstrip("/").rsplit("/", 1)[-1] for i in xp("//@itemtype"))
    doc.structured = {"jsonld": dict(jl), "jsonld_invalid": bad, "microdata": dict(md)}

    body_el = root.find("body")
    if body_el is None:
        body_el = root
    blocks = doc.blocks
    stack = [(body_el, None)]
    while stack:
        el, ctx = stack.pop()
        m = marker(el)
        key = m or ctx
        if m:
            st = blocks.setdefault(m, BlockStat())
            st.instances += 1
            if st.parent is None:
                st.parent = ctx
        if key is not None and key not in blocks:
            blocks[key] = BlockStat()
        st = blocks.get(key)

        def add_text(raw: str | None, st=st) -> None:
            t = _ws(raw)
            if len(t) < 2:
                return
            doc.text[t] += 1
            if st is not None:
                st.chars += len(t)
                if len(st.samples) < 3:
                    st.samples.append(t[:80])

        add_text(el.text)
        if el.tag == "a" and el.get("href") and not LINK_SKIP.match(el.get("href").strip()):
            doc.links.add(urldefrag(urljoin(base_url, el.get("href").strip()))[0])
            if st is not None:
                st.links += 1
        elif el.tag == "img" and st is not None:
            st.imgs += 1
        for child in el:
            add_text(child.tail)
            if isinstance(child.tag, str) and child.tag not in SKIP_TAGS:
                stack.append((child, key))
    for k in [k for k in blocks if IGNORE_KEY.search(k)]:
        del blocks[k]
    return doc


def _bs(b: BlockStat | None) -> dict | None:
    return None if b is None else {"instances": b.instances, "chars": b.chars, "links": b.links,
                                   "imgs": b.imgs, "parent": b.parent, "sample": b.samples}


def compare(a: Doc, b: Doc, limit: int = 40) -> dict:
    """Что есть в B, чего нет в A (и наоборот)."""
    appeared, disappeared, changed = [], [], []
    for k in sorted(set(a.blocks) | set(b.blocks)):
        ba, bb = a.blocks.get(k), b.blocks.get(k)
        ia, ib = (ba.instances if ba else 0), (bb.instances if bb else 0)
        wa, wb = (ba.weight if ba else 0), (bb.weight if bb else 0)
        if ia == 0 and ib > 0:
            appeared.append({"key": k, **_bs(bb)})
        elif ib == 0 and ia > 0:
            disappeared.append({"key": k, **_bs(ba)})
        elif ia and ib:
            kind = None
            if wa == 0 and wb > 0:
                kind = "filled"  # контейнер был пустым
            elif wb == 0 and wa > 0:
                kind = "emptied"
            elif ia != ib:
                kind = "count"
            elif abs(wb - wa) > max(40, 0.25 * max(wa, wb)):
                kind = "grew" if wb > wa else "shrunk"
            if kind:
                changed.append({"key": k, "kind": kind, "a": _bs(ba), "b": _bs(bb)})
    by_w = lambda x: -(x.get("chars", 0) + 20 * x.get("links", 0) + 20 * x.get("imgs", 0))  # noqa: E731
    appeared.sort(key=by_w)
    disappeared.sort(key=by_w)

    only_b, only_a = b.text - a.text, a.text - b.text
    ob = sum(len(t) * c for t, c in only_b.items())
    oa = sum(len(t) * c for t, c in only_a.items())
    tb = b.text_chars
    coverage = round(1 - ob / tb, 4) if tb else None

    head = {}
    for f in sorted(set(a.head) | set(b.head)):
        if a.head.get(f) != b.head.get(f):
            head[f] = {"a": a.head.get(f), "b": b.head.get(f)}

    links_new, links_gone = sorted(b.links - a.links), sorted(a.links - b.links)
    structured = {}
    for f in ("jsonld", "microdata"):
        sa, sb = a.structured.get(f, {}), b.structured.get(f, {})
        if sa != sb:
            structured[f] = {"a": sa, "b": sb}

    if tb == 0:
        verdict = "empty_b"
    elif not appeared and not disappeared and not changed and (coverage or 0) >= 0.98 and not links_new:
        verdict = "same"
    elif (coverage or 0) < 0.6 or len(appeared) >= 5:
        verdict = "major"
    else:
        verdict = "partial"

    return {
        "verdict": verdict,
        "summary": {
            "blocks_appeared": len(appeared), "blocks_disappeared": len(disappeared), "blocks_changed": len(changed),
            "links_appeared": len(links_new), "links_disappeared": len(links_gone),
            "text_chars_a": a.text_chars, "text_chars_b": tb,
            "text_only_b_chars": ob, "text_only_a_chars": oa, "coverage": coverage,
        },
        "blocks_appeared": appeared[:limit], "blocks_disappeared": disappeared[:limit], "blocks_changed": changed[:limit],
        "links_appeared_sample": links_new[:limit], "links_disappeared_sample": links_gone[:limit],
        "text_only_b_sample": [t[:120] for t, _ in only_b.most_common(15)],
        "head": head, "structured": structured,
    }
