"""Дополнительные проверки: гигиена внутренних ссылок и карты сайта, смешанный контент, тестовые адреса, анкоры, глубина."""
from __future__ import annotations

import re
from collections import Counter
from urllib.parse import urljoin, urlsplit

from .core import FAIL, NA, PASS, SKIPPED, WARN, Item, Result
from .extras import CAP, crawl_import, graded, short, ws, xcheck

B_URL = "URL и ссылки"
TRACK = re.compile(r"(^|&)(utm_[a-z]+|yclid|gclid|fbclid|_openstat|from|ysclid)=", re.I)
GARBAGE = re.compile(r"(/|=)(undefined|null|nan|none)(/|$|&)|\[object|%7b|\{\{|%20", re.I)
TEST_TOKENS = {"test", "tests", "testing", "dev", "staging", "stage", "preprod", "tmp", "temp", "backup", "copy", "draft", "demo", "old"}
PREFIX_TOKENS = ("test", "dev", "staging", "tmp")
GENERIC_ANCHORS = {"подробнее", "читать далее", "читать", "здесь", "тут", "кликните", "кликните здесь", "click here", "more", "read more",
                   "ссылка", "далее", "перейти", "узнать больше", "смотреть", "смотреть все", "ещё", "еще", "подробности", "→"}
FILE_EXT = re.compile(r"\.[a-z0-9]{2,5}$", re.I)


def _internal_links(p, cap: int = 500) -> list[str]:
    out = []
    for h in dict.fromkeys(p.links):
        if p.internal(h):
            out.append(h.split("#")[0])
            if len(out) >= cap:
                break
    return out


def _dominant_slash(links: list[str]) -> str | None:
    """'/' | '' — преобладающий вид адресов без расширения (не менее 80 %); None — единого стиля нет."""
    paths = [urlsplit(u).path for u in links if urlsplit(u).path not in ("", "/") and not FILE_EXT.search(urlsplit(u).path)]
    if len(paths) < 5:
        return None
    share = sum(1 for x in paths if x.endswith("/")) / len(paths)
    return "/" if share >= 0.8 else "" if share <= 0.2 else None


@xcheck("url_hygiene", B_URL, "Единый вид внутренних ссылок",
        "Во внутренних ссылках: нет заглавных букв, двойных слешей, пробелов, index.php, меток utm/yclid, http; единый стиль слеша", "Высокий")
def url_hygiene(ctx):
    pages = ctx.primary()
    if not pages:
        return Result(NA, "нет страниц")
    all_links = [u for p in pages for u in _internal_links(p)]
    dom = _dominant_slash(all_links)
    items = []
    for p in pages:
        hard, soft = Counter(), Counter()
        ex: dict[str, str] = {}
        for u in _internal_links(p):
            s = urlsplit(u)
            path = re.sub(r"%[0-9a-fA-F]{2}", "", s.path)
            probs = []
            if s.scheme == "http":
                probs.append("http")
            if re.search(r"[A-Z]", path):
                probs.append("заглавные буквы")
            if "//" in s.path:
                probs.append("двойной слеш")
            if " " in s.path or "%20" in s.path:
                probs.append("пробел")
            if re.search(r"/index\.(php|html?)$", s.path, re.I):
                probs.append("index.php/html")
            if TRACK.search(s.query):
                probs.append("метки utm/yclid")
            for x in probs:
                hard[x] += 1
                ex.setdefault(x, u)
            if dom is not None and s.path not in ("", "/") and not FILE_EXT.search(s.path) and s.path.endswith("/") != (dom == "/"):
                soft["стиль слеша отличается (справочно; редирект этим не доказан)"] += 1
                ex.setdefault("стиль слеша отличается (справочно; редирект этим не доказан)", u)
        msg = "; ".join(f"{k} ×{v} (напр. {short(ex[k], 70)})" for k, v in (hard + soft).items())
        sev = "fail" if hard else "ok"
        items.append((Item(p.url, sev == "ok", msg or "ссылки в едином виде", p.page_type), sev))
    return graded(items, note=f" · преобладающий стиль: {'со слешем' if dom == '/' else 'без слеша' if dom == '' else 'не определён'}")


@xcheck("mixed_content", B_URL, "Нет смешанного контента (http на https-страницах)",
        "Скрипты, стили, фреймы и изображения грузятся по https", "Высокий")
def mixed_content(ctx):
    pages = [p for p in ctx.primary() if p.final_url.startswith("https://")]
    if not pages:
        return Result(NA, "нет https-страниц")
    items = []
    for p in pages:
        active = [str(x) for x in p.xp("//script/@src|//link[@rel='stylesheet']/@href|//iframe/@src|//embed/@src|//object/@data")
                  if str(x).strip().lower().startswith("http://")]
        passive = [str(x) for x in p.xp("//img/@src|//source/@src|//video/@src|//audio/@src|//img/@srcset")
                   if str(x).strip().lower().startswith("http://")]
        if active:
            items.append((Item(p.url, False, f"активный: {len(active)} (напр. {short(active[0], 70)})" + (f"; пассивный: {len(passive)}" if passive else ""),
                               p.page_type), "fail"))
        elif passive:
            items.append((Item(p.url, False, f"пассивный: {len(passive)} (напр. {short(passive[0], 70)})", p.page_type), "warn"))
        else:
            items.append((Item(p.url, True, "всё по https", p.page_type), "ok"))
    return graded(items)


@xcheck("sitemap_url_hygiene", B_URL, "Чистая карта сайта: без дублей, мусора и чужих хостов",
        "В sitemap нет повторов, http, чужих хостов, undefined/null, меток и параметров; единый стиль слеша", "Высокий", source="probe")
def sitemap_url_hygiene(ctx):
    sm = ctx.sitemap["urls"]
    if not sm:
        return Result(SKIPPED, "карта сайта не собрана: python collect_html.py --probes discovery", source="probe")
    base_host = (urlsplit(ctx.base_url).hostname or "").lower()
    declared = sum(int((r["meta_d"] or {}).get("urls") or 0) for r in ctx.sitemap["files"])
    probs: dict[str, list[str]] = {}

    def add(k, u):
        probs.setdefault(k, []).append(u)

    if declared > len(sm):
        add("повторы URL", f"{declared - len(sm)} повторов (в файлах {declared}, уникальных {len(sm)})")
    for u in sm:
        s = urlsplit(u)
        if s.scheme != "https":
            add("не https", u)
        if base_host and (s.hostname or "").lower() != base_host:
            add("чужой хост", u)
        if GARBAGE.search(u):
            add("мусор в адресе (undefined, null, пробел)", u)
        if s.query:
            add("параметры в адресе", u)
        if re.search(r"[A-Z]", re.sub(r"%[0-9a-fA-F]{2}", "", s.path)):
            add("заглавные буквы", u)
        if u != u.strip() or "  " in u:
            add("пробелы в адресе", u)
    dom = _dominant_slash(list(sm))
    if dom is not None:
        for u in sm:
            p = urlsplit(u).path
            if p not in ("", "/") and not FILE_EXT.search(p) and p.endswith("/") != (dom == "/"):
                add("слеш не как у большинства", u)
    severe = {"повторы URL", "не https", "чужой хост", "мусор в адресе (undefined, null, пробел)"}
    items, fails, warns = [], 0, 0
    for k, urls in probs.items():
        sev = "fail" if k in severe else "warn"
        fails += sev == "fail"
        warns += sev == "warn"
        for u in urls[:max(1, CAP // len(probs))]:
            items.append(Item(u, False, k, "sitemap"))
    stats = {"total": len(sm), "bad": len({u for k, v in probs.items() for u in v})}
    ok = [Item(u, True, "в порядке", "sitemap") for u in list(sm)[:5]]
    if fails:
        return Result(FAIL, f"в карте ({len(sm)} URL) найдено: " + ", ".join(f"{k} — {len(v)}" for k, v in probs.items()), items + ok, "probe", stats)
    if warns:
        return Result(WARN, f"в карте ({len(sm)} URL) замечания: " + ", ".join(f"{k} — {len(v)}" for k, v in probs.items()), items + ok, "probe", stats)
    return Result(PASS, f"карта чистая: {len(sm)} URL без повторов, мусора и чужих хостов", ok, "probe", stats)


@xcheck("test_urls_exposed", B_URL, "Тестовые и служебные копии не открыты для индексации",
        "В sitemap, canonical и внутренних ссылках нет адресов вида /crm-test/, /dev/, /old/, /copy/", "Критично")
def test_urls_exposed(ctx):
    def seg_hit(seg: str) -> str | None:
        if seg in TEST_TOKENS:
            return seg
        for t in TEST_TOKENS:
            if re.search(rf"[-_.]{t}$", seg):
                return t
        for t in PREFIX_TOKENS:
            if re.match(rf"^{t}[-_.]", seg):
                return t
        return None

    def is_test(u: str) -> str | None:
        hit = [h for seg in urlsplit(u).path.lower().split("/") if seg and (h := seg_hit(seg))]
        host = (urlsplit(u).hostname or "").lower()
        label = host.split(".")[0]
        if label in TEST_TOKENS or label.startswith(("preprod", "stage", "staging", "test")):
            hit.append("хост " + host)
        return ", ".join(sorted(set(hit))) or None

    found: dict[str, tuple[str, str]] = {}  # url -> (где, токены)
    for u in ctx.sitemap["urls"]:
        if (t := is_test(u)):
            found.setdefault(u, ("sitemap", t))
    pages = ctx.primary()
    for p in pages:
        c = p.canonicals[0].get("href", "").strip() if p.canonicals else ""
        if c and (t := is_test(c)):
            found.setdefault(c, ("canonical на " + p.url, t))
        if (t := is_test(p.final_url)):
            found.setdefault(p.final_url, ("страница выборки", t))
        for h in _internal_links(p):
            if (t := is_test(h)):
                found.setdefault(h, ("ссылка на " + p.url, t))
    if not pages and not ctx.sitemap["urls"]:
        return Result(NA, "нет страниц и карты сайта")
    if not found:
        return Result(PASS, "тестовых адресов в карте, canonical и внутренних ссылках не найдено")
    items = []
    for u, (where, t) in found.items():
        sev = "fail" if where.startswith(("sitemap", "canonical", "страница")) else "warn"
        items.append((Item(u, False, f"{where}: токены «{t}»", "тест"), sev))
    return graded(items, what="адресов")


@xcheck("link_anchor_quality", B_URL, "Содержательные анкоры внутренних ссылок",
        "Не больше 10 % ссылок на странице с анкором «подробнее», «здесь» или без текста", "Средний")
def link_anchor_quality(ctx):
    pages = ctx.primary()
    if not pages:
        return Result(NA, "нет страниц")
    items = []
    for p in pages:
        total = generic = empty = 0
        ex = ""
        for a in p.xp("//a[@href]"):
            h = (a.get("href") or "").strip()
            if not h or h.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            if not p.internal(urljoin(p.final_url, h)):
                continue
            total += 1
            text = ws(a.text_content()).lower()
            if not text:
                has_img_alt = any((i.get("alt") or "").strip() for i in a.xpath(".//img")) or (a.get("aria-label") or "").strip() or (a.get("title") or "").strip()
                if not has_img_alt:
                    empty += 1
                    ex = ex or h
            elif text in GENERIC_ANCHORS:
                generic += 1
                ex = ex or f"«{text}» → {h}"
        if not total:
            continue
        share = (generic + empty) / total
        msg = f"пустых: {empty}, общих: {generic} из {total} ссылок ({share:.0%})" + (f", напр. {short(ex, 70)}" if ex else "")
        items.append((Item(p.url, share <= 0.10, msg, p.page_type), "ok" if share <= 0.10 else "warn"))
    return graded(items, ok_text="анкоры содержательные")


@xcheck("crawl_depth", B_URL, "Важные страницы не глубже MAX_DEPTH кликов",
        "Индексируемые страницы доступны не более чем в MAX_DEPTH переходах от главной (по краулу Screaming Frog)", "Средний", source="crawl")
def crawl_depth(ctx):
    imp = crawl_import(ctx)
    if not imp:
        return Result(SKIPPED, "нужна выгрузка Screaming Frog: раздел «Краул» или import_crawl.py", source="crawl")
    md = ctx.cs.max_depth
    base = "FROM crawl_pages WHERE import_id=? AND content_type LIKE 'text/html%' AND status=200 AND indexability='Indexable' AND depth IS NOT NULL"
    total = ctx.db.execute(f"SELECT COUNT(*) {base}", (imp["id"],)).fetchone()[0]
    if not total:
        return Result(NA, "в краулe нет данных о глубине", source="crawl")
    n = ctx.db.execute(f"SELECT COUNT(*) {base} AND depth>?", (imp["id"], md)).fetchone()[0]
    rows = ctx.db.execute(f"SELECT address, depth, inlinks {base} AND depth>? ORDER BY depth DESC, address LIMIT ?", (imp["id"], md, CAP)).fetchall()
    items = [Item(r["address"], False, f"глубина {r['depth']} > {md}, входящих ссылок: {r['inlinks'] or 0}", "crawl") for r in rows]
    stats = {"total": total, "bad": n}
    note = f" · краул от {(imp['taken_at'] or '')[:10]}, {imp['rows']} URL"
    if not n:
        return Result(PASS, f"все {total} индексируемых страниц не глубже {md} кликов{note}", [], "crawl", stats)
    return Result(FAIL if n / total > 0.2 else WARN, f"{n} из {total} индексируемых страниц глубже {md} кликов ({n / total:.0%}){note}", items, "crawl", stats)


@xcheck("index_orphans", B_URL, "Страницы из индекса поисковиков достижимы по ссылкам (нет сирот)",
        "Каждая страница из Яндекс Вебмастера / Search Console есть в краулe, на неё ведут внутренние ссылки и она не глубже MAX_DEPTH кликов",
        "Высокий", source="crawl")
def index_orphans(ctx):
    from .. import indexed as ix
    from .. import orphans as orph
    imp = crawl_import(ctx)
    if not imp:
        return Result(SKIPPED, "нужна выгрузка Screaming Frog: раздел «Краул» или import_crawl.py", source="crawl")
    ids = {}
    for src in ("ywm", "gsc"):
        last = ix.latest(ctx.db, ctx.network, src)
        if last:
            ids[src] = last["id"]
    if not ids:
        return Result(SKIPPED, "нет выгрузок индекса: раздел «Сироты» (Вебмастер / Search Console, API или файл)", source="crawl")
    res = orph.compare(ctx.db, imp["id"], ids, max_depth=ctx.cs.max_depth, probes=orph.saved_probes(ctx.db, ctx.network))
    s = res["summary"]
    bad = [r for r in res["rows"] if r["category"] in orph.ORPHAN_CATS]
    bad.sort(key=lambda r: (-(r["impressions"] or 0), r["url"]))
    labels = {"not_in_crawl": "нет в краулe", "no_inlinks": "нет входящих ссылок", "deep": "глубина %s > %s"}
    items = []
    for r in bad[:CAP]:
        msg = labels[r["category"]] % (r["depth"], ctx.cs.max_depth) if r["category"] == "deep" else labels[r["category"]]
        items.append(Item(r["url"], False, msg + (" · " + ", ".join(n for n, f in (("Яндекс", r["in_ywm"]), ("Google", r["in_gsc"])) if f)), "crawl"))
    total = s["index_total"]
    note = f" · индекс: {', '.join(ix.SOURCES[k] for k in ids)}; краул от {(imp['taken_at'] or '')[:10]}"
    stats = {"total": total, "bad": len(bad)}
    if not bad:
        return Result(PASS, f"все {total} страниц из индекса достижимы краулом{note}", [], "crawl", stats)
    share = len(bad) / total if total else 0
    warn = ("; краул нашёл менее половины адресов — сначала проверьте его полноту" if (s["coverage"] or 100) < 50 else "")
    return Result(FAIL if share > 0.05 else WARN,
                  f"{len(bad)} из {total} страниц индекса — сироты ({share:.0%}): нет в краулe {s['by_category']['not_in_crawl']}, "
                  f"без ссылок {s['by_category']['no_inlinks']}, глубже {ctx.cs.max_depth} кликов {s['by_category']['deep']}{warn}{note}",
                  items, "crawl", stats)
