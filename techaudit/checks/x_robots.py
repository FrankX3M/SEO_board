"""Дополнительные проверки: robots.txt (доступ ботов, конфликты с картой и noindex), Clean-param для Яндекса, ИИ-боты, llms.txt."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from .core import FAIL, NA, PASS, SKIPPED, WARN, Item, Result, expects_noindex
from .extras import CAP, graded, short, xcheck

B_ROB = "Robots.txt и Яндекс"
B_IDX = "Индексация и дубли"
HINT = "пробы не собирались: python collect_html.py --probes discovery"
SEARCH_BOTS = (("Googlebot", "googlebot"), ("YandexBot", "yandex"))
ROBOTS_MAX = 500 * 1024
KNOWN = {"user-agent", "disallow", "allow", "sitemap", "crawl-delay", "clean-param", "host", "noindex"}

# (метка, токен user-agent, «ответный» бот: закрытие лишает сайт показа в ответах ИИ-поиска)
AI_BOTS = (("GPTBot", "gptbot", False), ("OAI-SearchBot", "oai-searchbot", True), ("ChatGPT-User", "chatgpt-user", True),
           ("ClaudeBot", "claudebot", False), ("Claude-SearchBot", "claude-searchbot", True), ("Claude-User", "claude-user", True),
           ("PerplexityBot", "perplexitybot", True), ("Google-Extended", "google-extended", False), ("CCBot", "ccbot", False),
           ("YandexAdditional", "yandexadditional", True), ("Bytespider", "bytespider", False), ("Applebot-Extended", "applebot-extended", False))


class Robots:
    """Разбор robots.txt по правилам Google/Яндекса: группы по user-agent, самое длинное совпадение побеждает, при равенстве — Allow;
    поддерживаются * и $ в шаблонах."""

    def __init__(self, text: str):
        self.groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
        self.sitemaps: list[str] = []
        self.clean_params: list[str] = []
        self.hosts: list[str] = []
        self.unknown: list[str] = []
        self.stray: int = 0
        agents: list[str] = []
        rules: list[tuple[bool, str]] = []
        in_rules = False
        for raw in text.lstrip("﻿").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if ":" not in line:
                self.unknown.append(line[:60])
                continue
            k, v = line.split(":", 1)
            k, v = k.strip().lower(), v.strip()
            if k == "user-agent":
                if in_rules:
                    self.groups.append((agents, rules))
                    agents, rules, in_rules = [], [], False
                agents.append(v.lower())
            elif k in ("allow", "disallow"):
                if not agents:
                    self.stray += 1
                    continue
                in_rules = True
                if v:
                    rules.append((k == "allow", v))
            elif k == "sitemap":
                self.sitemaps.append(v)
            elif k == "clean-param":
                self.clean_params.append(v)
            elif k == "host":
                self.hosts.append(v)
            elif k not in KNOWN:
                self.unknown.append(line[:60])
        if agents:
            self.groups.append((agents, rules))

    @staticmethod
    def _rx(pat: str):
        end = pat.endswith("$")
        body = re.escape(pat[:-1] if end else pat).replace(r"\*", ".*")
        return re.compile("^" + body + ("$" if end else ""))

    def _rules_for(self, ua: str) -> list[tuple[bool, str]]:
        ua = ua.lower()
        best, best_len = None, -1
        for agents, rules in self.groups:
            for a in agents:
                if a != "*" and a in ua and len(a) > best_len:
                    best, best_len = rules, len(a)
        if best is not None:
            return best
        out: list[tuple[bool, str]] = []
        for agents, rules in self.groups:
            if "*" in agents:
                out += rules
        return out

    def check(self, ua: str, url: str) -> tuple[bool, str | None]:
        """(разрешено, сработавшее правило)."""
        u = urlsplit(url)
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        winner: tuple[int, bool, str] | None = None
        for allow, pat in self._rules_for(ua):
            if self._rx(pat).match(path):
                cand = (len(pat), allow, pat)
                if winner is None or cand[0] > winner[0] or (cand[0] == winner[0] and allow and not winner[1]):
                    winner = cand
        if winner is None:
            return True, None
        return winner[1], (("Allow: " if winner[1] else "Disallow: ") + winner[2])

    def explicit(self, token: str) -> bool:
        return any(token in a for agents, _ in self.groups for a in agents if a != "*")


def robots_of(ctx):
    """(строка пробы | None, Robots | None). Нет проб — (None, None)."""
    if not hasattr(ctx, "_robots"):
        rows = ctx.probes("robots")
        row = rows[0] if rows else None
        rb = None
        if row and row.get("status") == 200:
            body = ctx.probe_body(row)
            if body:
                rb = Robots(body.decode("utf-8", "replace"))
                rb.size = len(body)
        ctx._robots = (row, rb)
    return ctx._robots


def _no_robots(row) -> Result | None:
    if row is None:
        return Result(SKIPPED, HINT, source="probe")
    return None


@xcheck("robots_txt_valid", B_ROB, "robots.txt доступен, корректен и не закрывает сайт",
        "Код 200, text/plain, до 500 КБ; нет Disallow: / для всех ботов; есть Sitemap с абсолютным https-адресом", "Критично", source="probe")
def robots_txt_valid(ctx):
    row, rb = robots_of(ctx)
    if (r := _no_robots(row)):
        return r
    url = row["url"]
    items: list[tuple[Item, str]] = []
    if row["status"] != 200 or rb is None:
        return Result(FAIL, f"robots.txt отвечает {row['status'] or 'ошибкой'}", [Item(url, False, f"код {row['status']}", "robots")], "probe",
                      {"total": 1, "bad": 1})
    ct = (row.get("content_type") or "").lower()
    items.append((Item(url, ct.startswith("text/plain"), f"Content-Type: {ct or 'нет'}", "robots"), "ok" if ct.startswith("text/plain") else "warn"))
    size = getattr(rb, "size", 0)
    items.append((Item(url, size <= ROBOTS_MAX, f"размер {size // 1024} КБ", "robots"), "ok" if size <= ROBOTS_MAX else "fail"))
    closed = [ua for ua, rules in ((a, r) for ags, r in rb.groups for a in ags)
              if ua == "*" and any(not allow and pat in ("/", "/*") for allow, pat in rules)
              and not any(allow and pat in ("/", "/*") for allow, pat in rules)]
    items.append((Item(url, not closed, "сайт закрыт для всех ботов: Disallow: /" if closed else "сайт не закрыт целиком", "robots"),
                  "fail" if closed else "ok"))
    star = any("*" in ags for ags, _ in rb.groups)
    items.append((Item(url, star, "есть группа User-agent: *" if star else "нет группы User-agent: *", "robots"), "ok" if star else "warn"))
    sm_ok = bool(rb.sitemaps) and all(s.startswith("https://") for s in rb.sitemaps)
    items.append((Item(url, sm_ok, ("Sitemap: " + ", ".join(short(s, 50) for s in rb.sitemaps[:3])) if rb.sitemaps else "нет директивы Sitemap",
                       "robots"), "ok" if sm_ok else "warn"))
    probs = []
    if rb.hosts:
        probs.append("директива Host устарела — Яндекс её не учитывает")
    if rb.unknown:
        probs.append("неизвестные строки: " + "; ".join(rb.unknown[:3]))
    if rb.stray:
        probs.append(f"{rb.stray} правил вне группы User-agent")
    items.append((Item(url, not probs, "; ".join(probs) or "синтаксис без замечаний", "robots"), "warn" if probs else "ok"))
    res = graded(items, what="пунктов robots.txt", source="probe")
    return res


def _sample_urls(ctx, limit: int = 100) -> list[tuple[str, str]]:
    """[(url, откуда)] — страницы выборки и URL из карты сайта."""
    out, seen = [], set()
    for p in ctx.primary():
        if p.final_url not in seen:
            seen.add(p.final_url)
            out.append((p.final_url, "выборка"))
    sm = list(ctx.sitemap["urls"])
    step = max(1, len(sm) // limit) if sm else 1
    for u in sm[::step][:limit]:
        if u not in seen:
            seen.add(u)
            out.append((u, "sitemap"))
    return out


@xcheck("robots_blocks_pages", B_ROB, "robots.txt не закрывает страницы и ресурсы от Googlebot и YandexBot",
        "Страницы выборки, URL из карты, CSS и JS открыты для основных ботов", "Критично", source="probe")
def robots_blocks_pages(ctx):
    row, rb = robots_of(ctx)
    if (r := _no_robots(row)):
        return r
    if rb is None:
        return Result(NA, "robots.txt недоступен — доступ не ограничен правилами", source="probe")
    items: list[tuple[Item, str]] = []
    for url, src in _sample_urls(ctx):
        blocked = []
        for label, tok in SEARCH_BOTS:
            ok, rule = rb.check(tok, url)
            if not ok:
                blocked.append(f"{label}: {rule}")
        items.append((Item(url, not blocked, "; ".join(blocked) or "открыт", src), "fail" if blocked else "ok"))
    assets, seen = [], set()
    for p in ctx.primary():
        for el in p.xp("//link[@rel='stylesheet']/@href|//script/@src"):
            u = urljoin(p.final_url, str(el))
            if u not in seen and p.internal(u):
                seen.add(u)
                assets.append(u)
    for u in assets[:200]:
        blocked = [f"{l}: {rb.check(t, u)[1]}" for l, t in SEARCH_BOTS if not rb.check(t, u)[0]]
        if blocked:
            items.append((Item(u, False, "ресурс закрыт: " + "; ".join(blocked), "css/js"), "warn"))
    if not items:
        return Result(NA, "нет URL для проверки", source="probe")
    return graded(items, what="URL", source="probe", ok_text=f"{len(items)} URL и ресурсов открыты для Googlebot и YandexBot")


@xcheck("robots_vs_sitemap", B_ROB, "URL из sitemap не закрыты в robots.txt",
        "Ни один адрес из карты сайта не попадает под Disallow для Googlebot и YandexBot", "Высокий", source="probe")
def robots_vs_sitemap(ctx):
    row, rb = robots_of(ctx)
    if (r := _no_robots(row)):
        return r
    sm = list(ctx.sitemap["urls"])
    if not sm:
        return Result(SKIPPED, "карта сайта не собрана: python collect_html.py --probes discovery", source="probe")
    if rb is None:
        return Result(NA, "robots.txt недоступен", source="probe")
    bad = []
    for u in sm:
        blocked = [f"{l}: {rb.check(t, u)[1]}" for l, t in SEARCH_BOTS if not rb.check(t, u)[0]]
        if blocked:
            bad.append(Item(u, False, "; ".join(blocked), "sitemap"))
    total = len(sm)
    stats = {"total": total, "bad": len(bad)}
    if bad:
        return Result(FAIL, f"{len(bad)} из {total} URL карты закрыты в robots.txt", bad[:CAP], "probe", stats)
    return Result(PASS, f"{total} URL карты открыты для Googlebot и YandexBot", [Item(u, True, "открыт", "sitemap") for u in sm[:8]], "probe", stats)


@xcheck("robots_noindex_blocked", B_IDX, "Страницы с noindex не закрыты в robots.txt",
        "Бот должен иметь доступ к странице, чтобы увидеть noindex; закрытая в robots.txt страница может остаться в индексе", "Высокий",
        source="probe")
def robots_noindex_blocked(ctx):
    row, rb = robots_of(ctx)
    if (r := _no_robots(row)):
        return r
    if rb is None:
        return Result(NA, "robots.txt недоступен", source="probe")
    items = []
    for p in ctx.primary():
        d = " ".join(v for k in ("robots", "googlebot", "yandex", "yandexbot") for v in p.meta.get(k, [])).lower() + " " + (p.headers.get("x-robots-tag") or "").lower()
        if "noindex" not in d:
            continue
        blocked = [f"{l}: {rb.check(t, p.final_url)[1]}" for l, t in SEARCH_BOTS if not rb.check(t, p.final_url)[0]]
        items.append((Item(p.url, not blocked, "; ".join(blocked) or "доступ боту открыт, noindex будет прочитан", p.page_type),
                      "fail" if blocked else "ok"))
    if not items:
        return Result(PASS, "страниц с noindex в выборке нет", source="probe")
    return graded(items, what="страниц с noindex", source="probe")


@xcheck("yandex_clean_param", B_ROB, "Для Яндекса задан Clean-param, если есть URL с параметрами",
        "Метки и служебные параметры (utm_*, yclid, sort) перечислены в Clean-param robots.txt", "Средний", source="probe")
def yandex_clean_param(ctx):
    row, rb = robots_of(ctx)
    if (r := _no_robots(row)):
        return r
    pagin = ctx.cs.pagination_param
    param_urls = []
    for p in ctx.primary():
        for h in dict.fromkeys(p.links):
            q = urlsplit(h).query
            if q and p.internal(h) and re.sub(rf"(^|&){re.escape(pagin)}=\d+", "", q).strip("&"):
                param_urls.append(h)
    param_urls += [u for u in ctx.sitemap["urls"] if "?" in u]
    if not param_urls:
        return Result(PASS, "внутренних ссылок и адресов карты с параметрами в выборке нет — Clean-param не требуется", source="probe")
    if rb is None:
        return Result(NA, "robots.txt недоступен", source="probe")
    if rb.clean_params:
        return Result(PASS, f"Clean-param задан ({len(rb.clean_params)} правил), URL с параметрами: {len(param_urls)}",
                      [Item(u, True, "Clean-param: " + short(x, 60), "robots") for u, x in zip(param_urls[:3], rb.clean_params)], "probe",
                      {"total": len(param_urls), "bad": 0})
    return Result(WARN, f"найдено URL с параметрами: {len(param_urls)}, а Clean-param в robots.txt нет",
                  [Item(u, False, "URL с параметрами без Clean-param", "robots") for u in param_urls[:CAP]], "probe",
                  {"total": len(param_urls), "bad": len(param_urls)})


@xcheck("ai_bots_policy", B_ROB, "Осознанная политика для ИИ-ботов в robots.txt",
        "Боты ИИ-поиска (OAI-SearchBot, PerplexityBot, YandexAdditional и др.) не закрыты случайно", "Низкий", source="probe")
def ai_bots_policy(ctx):
    row, rb = robots_of(ctx)
    if (r := _no_robots(row)):
        return r
    if rb is None:
        return Result(NA, "robots.txt недоступен", source="probe")
    url = (ctx.base_url or "https://example.test") + "/"
    items, answer_blocked = [], 0
    for label, tok, answer in AI_BOTS:
        ok, rule = rb.check(tok, url)
        kind = "ответы ИИ" if answer else "обучение"
        msg = ("открыт" if ok else f"закрыт ({rule})") + (", правило задано явно" if rb.explicit(tok) else ", по группе *")
        if not ok and answer:
            answer_blocked += 1
        items.append(Item(url, ok or not answer, f"{label}: {msg}", kind))
    stats = {"total": len(items), "bad": answer_blocked}
    if answer_blocked:
        return Result(WARN, f"закрыты боты ИИ-поиска: {answer_blocked} — сайт не попадёт в ответы ИИ; убедитесь, что это решение, а не случайность",
                      items, "probe", stats)
    return Result(PASS, "боты ИИ-поиска не закрыты; закрытие ботов обучения — вопрос политики сайта", items, "probe", stats)


@xcheck("llms_txt", B_ROB, "Файл llms.txt", "/llms.txt отдаёт 200 и начинается с H1 (Markdown): описание сайта и ссылки на главные разделы",
        "Низкий", source="live")
def llms_txt(ctx):
    rows = ctx.live("llms")
    if not rows:
        return Result(SKIPPED, "нужна живая выборка: python run_checks.py --live", source="live")
    r = rows[0]
    m = r["meta_d"]
    url = r["url"]
    if r["status"] != 200:
        return Result(WARN, f"llms.txt отвечает {r['status']} — файл необязателен, но помогает ИИ-агентам понять структуру сайта",
                      [Item(url, False, f"код {r['status']}", "llms")], "live", {"total": 1, "bad": 1})
    head = m.get("head") or ""
    ok = head.lstrip().startswith("# ")
    return Result(PASS if ok else WARN, "llms.txt есть" + ("" if ok else ", но первая строка не H1 (# Название)"),
                  [Item(url, ok, f"{m.get('bytes', 0)} байт; начало: {short(head, 60)}", "llms")], "live", {"total": 1, "bad": 0 if ok else 1})
