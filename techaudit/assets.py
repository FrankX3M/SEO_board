"""Ресурсы из <head>: favicon, apple-touch-icon, манифест (и его иконки), og:image / twitter:image.

Ссылки берутся из уже собранного HTML страниц, каждая запрашивается, проверяются статус,
тип содержимого и (для картинок) размеры.
"""
from __future__ import annotations

import asyncio
import json
import struct
from urllib.parse import urljoin, urlsplit

from .probes import ProbeContext


def image_size(data: bytes) -> tuple[int, int] | None:
    """Ширина и высота PNG/GIF/JPEG/WebP по заголовку, без сторонних библиотек."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", data[6:10])
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                size = struct.unpack(">H", data[i + 2:i + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + size
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            kind = data[12:16]
            if kind == b"VP8X":
                return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
            if kind == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if kind == b"VP8L":
                b = data[21:25]
                return 1 + (((b[1] & 0x3F) << 8) | b[0]), 1 + (((b[3] & 0xF) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
        if data[:4] == b"\x00\x00\x01\x00" and len(data) >= 8:  # ico: первая иконка
            return (data[6] or 256), (data[7] or 256)
    except Exception:  # noqa: BLE001
        return None
    return None


def extract_resources(html: bytes, page_url: str) -> list[dict]:
    """[{kind, rel, url, sizes}] из <link> и <meta> страницы."""
    import lxml.html

    try:
        doc = lxml.html.fromstring(html)
    except Exception:  # noqa: BLE001
        return []
    out = []

    def add(kind, rel, href, sizes=None):
        if not href or href.strip().lower().startswith(("data:", "javascript:")):
            return
        out.append({"kind": kind, "rel": rel, "url": urljoin(page_url, href.strip()), "sizes": sizes})

    for el in doc.iter("link"):
        rels = (el.get("rel") or "").lower().split()
        href = el.get("href")
        if "manifest" in rels:
            add("manifest", "manifest", href)
        elif any(r.startswith("apple-touch-icon") for r in rels):
            add("apple_icon", " ".join(rels), href, el.get("sizes"))
        elif "icon" in rels:
            add("favicon", " ".join(rels), href, el.get("sizes"))
    for el in doc.iter("meta"):
        key = (el.get("property") or el.get("name") or "").lower()
        if key in ("og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src"):
            add("og_image", key, el.get("content"))
    seen, uniq = set(), []
    for r in out:
        k = (r["kind"], r["url"])
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq


def _check_image(res, expect_ct_prefix="image/") -> list[str]:
    reasons = []
    if res.error:
        return ["fetch_error"]
    if res.status != 200:
        return [f"status_{res.status}"]
    ct = (res.headers.get("content-type") or "").split(";")[0].strip().lower()
    ok_ct = ct.startswith(expect_ct_prefix) or ct in {"application/octet-stream", "binary/octet-stream"}
    if not ok_ct:
        reasons.append(f"bad_content_type:{ct or 'none'}")
    if not res.body:
        reasons.append("empty_body")
    return reasons


async def probe_resources(ctx: ProbeContext, pages: list[tuple[str, str, str, bytes]], kinds) -> None:
    """pages: [(page_type, source_url, final_url, html_bytes)]. Каждый ресурс проверяется один раз на сеть."""
    wanted = set(kinds)
    todo: dict[tuple[str, str], dict] = {}
    for ptype, src, final_url, html in pages:
        for r in extract_resources(html, final_url):
            if r["kind"] in wanted:
                todo.setdefault((r["kind"], r["url"]), {**r, "page_type": ptype, "page": src})
    if "favicon" in wanted:  # адрес по умолчанию проверяем всегда
        d = ctx.access.base_url + "/favicon.ico"
        todo.setdefault(("favicon", d), {"kind": "favicon", "rel": "default /favicon.ico", "url": d, "sizes": None,
                                         "page_type": "", "page": ""})

    async def one(item: dict) -> None:
        res = await ctx.fetch(item["url"])
        meta = {"rel": item["rel"], "found_on": item["page"], "sizes": item["sizes"], "hops": len(res.chain)}
        if item["kind"] == "manifest":
            if res.error:
                reasons = ["fetch_error"]
            elif res.status != 200:
                reasons = [f"status_{res.status}"]
            else:
                try:
                    data = json.loads(res.body.decode("utf-8-sig"))
                    icons = [i.get("src") for i in (data.get("icons") or []) if isinstance(i, dict) and i.get("src")]
                    meta["icons"] = len(icons)
                    meta["name"] = data.get("name") or data.get("short_name")
                    for src in icons:
                        todo_icons.append({"kind": "manifest_icon", "rel": "manifest icon", "sizes": None,
                                           "url": urljoin(item["url"], src), "page_type": "", "page": item["url"]})
                except Exception:  # noqa: BLE001
                    reasons = ["invalid_json"]
                    data = None
                else:
                    reasons = [] if icons else ["manifest_without_icons"]
            ctx.record("manifest", item["url"], res, reasons, page_type=item["page_type"], source_url=item["page"],
                       meta=meta, save_ext="json")
            return
        reasons = _check_image(res)
        size = image_size(res.body) if res.body else None
        if size:
            meta["width"], meta["height"] = size
        elif res.status == 200 and not reasons:
            meta["size_unknown"] = True
        ctx.record(item["kind"], item["url"], res, reasons, page_type=item["page_type"], source_url=item["page"],
                   meta=meta)

    todo_icons: list[dict] = []
    await asyncio.gather(*(one(i) for i in todo.values()))
    if "manifest_icon" in wanted and todo_icons:
        seen = {u for (_, u) in todo}
        uniq = {i["url"]: i for i in todo_icons if i["url"] not in seen}
        await asyncio.gather(*(one(i) for i in uniq.values()))
