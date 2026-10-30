"""План работ: приоритет, роль, оценка в часах и ожидаемый результат для каждой проверки.

Оценка — на исправление при статусе «Не пройдено» (для «Требует внимания» берётся доля PLAN_WARN_SHARE, по умолчанию 0,5).
Часы заданы на всю сеть как одну задачу (общая платформа); если сети правятся независимо — PLAN_PER_NETWORK=1 умножит на число сетей.
Свои значения: CSV в PLAN_FILE (колонки check_id;role;hours;result), строки перекрывают справочник ниже.
"""
from __future__ import annotations

import csv
import os
from pathlib import Path

PRIORITIES = ("Критично", "Высокий", "Средний", "Низкий")
WAVE = {"Критично": 1, "Высокий": 2, "Средний": 3, "Низкий": 4}
WAVE_LABEL = {1: "Волна 1", 2: "Волна 2", 3: "Волна 3", 4: "Волна 4"}
ROLES = {"DEV": "Разработчик", "DEVOPS": "DevOps / админ", "SEO": "SEO-специалист", "PM": "Менеджер", "CONTENT": "Контент"}

_RESULT_BY_ROLE = {
    "DEV": "ТЗ разработчику и проверка исправления", "DEVOPS": "Правка конфигурации сервера и перепроверка",
    "SEO": "Список правок и перепроверка", "PM": "Чек-лист правок", "CONTENT": "ТЗ на тексты и список правок",
}

# check_id: (роль, часы, результат или "" — по роли)
PLAN: dict[str, tuple[str, float, str]] = {
    "crawl_overview": ("SEO", 0, "Декомпозиция импортированного краула и переход к отдельным проверкам"),
    # редиректы и коды ответа
    "redirect_slash": ("DEVOPS", 2, "Правило редиректа: один 301 на адрес без/со слешем по утверждённой схеме"),
    "redirect_upper": ("DEVOPS", 1.5, "Правило 301 с заглавных на строчные"),
    "redirect_www": ("DEVOPS", 1.5, "Один 301 на основной хост, сертификат на второй хост"),
    "redirect_http": ("DEVOPS", 1, "Один 301 с http на https"),
    "redirect_combined": ("DEVOPS", 2, "Единый редирект на конечный адрес за один шаг"),
    "redirect_index": ("DEVOPS", 1, "301 с index.php и index.html на корень раздела"),
    "status_200": ("DEV", 4, "Список URL с ошибками и исправление источников ссылок"),
    "status_404": ("DEV", 2, "Корректный код 404 для удалённых и несуществующих адресов"),
    "status_no5xx": ("DEV", 4, "Разбор причин 5xx и исправление"),
    "crawl_no_response": ("SEO", 1, "Уточнить причины отсутствия HTTP-ответа: robots.txt, сеть или таймаут; согласовать доступ краулера"),
    "status_no404_redirect": ("DEV", 1.5, "Отдавать 404 на месте, без редиректа на страницу ошибки"),
    "template_404": ("DEV", 3, "Шаблон 404: шапка, меню, поиск, ссылки на разделы, счётчики"),
    "ttfb": ("DEV", 8, "Профилирование и ускорение ответа сервера на типовых страницах"),
    # sitemap
    "sitemap_available": ("DEV", 1, "Карта сайта отдаёт 200 по адресу из robots.txt"),
    "sitemap_valid": ("DEV", 2, "Валидный XML, разбиение по лимитам 50 000 URL / 50 МБ"),
    "sitemap_canonical_200": ("DEV", 3, "В карте только канонические адреса с кодом 200"),
    "sitemap_complete": ("DEV", 3, "Добавить в карту недостающие индексируемые страницы"),
    "sitemap_cache": ("DEV", 1.5, "Сбросить кеш и настроить пересборку карт"),
    "sitemap_lastmod": ("DEV", 2, "Реальная дата изменения страницы в lastmod"),
    "sitemap_submitted": ("SEO", 0.5, "Карты добавлены в Вебмастер и Search Console"),
    "favicon": ("DEV", 1, "favicon.ico, png и apple-touch-icon отдают 200"),
    # рендеринг
    "content_no_js": ("DEV", 16, "Серверная отрисовка текста, заголовков и ссылок"),
    "dynamic_rendered": ("DEV", 12, "Динамические блоки (цены, отзывы) в отрендеренном DOM"),
    "text_descriptions": ("SEO", 3, "Текстовые описания видимы и не подгружаются по клику"),
    "gsc": ("SEO", 1, "Проверка URL в Search Console и разбор замечаний"),
    "yandex_webmaster": ("SEO", 1, "Проверка в Вебмастере и запрос переобхода"),
    "mobile_friendly": ("DEV", 6, "Исправление мобильной вёрстки по замечаниям Lighthouse"),
    "mobile_parity": ("DEV", 6, "Единые метатеги, текст и ссылки на мобильной и десктопной версиях"),
    "cwv": ("DEV", 12, "Оптимизация LCP, CLS, INP"),
    # внутренние ссылки
    "links_a_href": ("DEV", 6, "Ссылки тегом a с href вместо onclick"),
    "menu_footer": ("DEV", 4, "Меню и футер в исходном HTML"),
    "breadcrumbs": ("DEV", 3, "Хлебные крошки ссылками и BreadcrumbList"),
    "pagination_links": ("DEV", 8, "Пагинация ссылками с href"),
    "links_redirects": ("DEV", 4, "Внутренние ссылки сразу на конечные URL"),
    "links_404": ("DEV", 3, "Исправить или убрать ссылки на 404"),
    "images_broken": ("DEV", 2, "Исправить пути изображений и ресурсов"),
    "orphans": ("SEO", 4, "Схема перелинковки для страниц-сирот"),
    "session_params": ("DEV", 2, "Убрать параметры сессии из ссылок"),
    "alt": ("CONTENT", 3, "Заполнить alt у изображений карточек и листингов"),
    # robots / canonical
    "noindex_accidental": ("DEV", 2, "Снять случайный noindex"),
    "nofollow_accidental": ("DEV", 2, "Снять случайный nofollow"),
    "x_robots_tag": ("DEVOPS", 1, "Убрать noindex из X-Robots-Tag"),
    "noindex_where_needed": ("DEV", 2, "Поставить noindex на сортировки, поиск, корзину, ЛК"),
    "noindex_conflicts": ("SEO", 2, "Убрать конфликт noindex с canonical и sitemap"),
    "canonical_present": ("DEV", 3, "Один canonical в head на всех индексируемых"),
    "canonical_absolute": ("DEV", 2, "Абсолютный https-адрес основного домена"),
    "canonical_exact": ("DEV", 2, "Посимвольное совпадение с фактическим URL"),
    "canonical_self": ("DEV", 3, "Самоканоникал на обычных страницах"),
    "canonical_new_url": ("DEV", 3, "Canonical на новый URL без редиректов"),
    "canonical_target_ok": ("DEV", 3, "Canonical не на 404, редирект или noindex"),
    "canonical_params": ("DEV", 2, "Canonical на URL без параметров"),
    "canonical_pagination": ("DEV", 3, "Единая логика canonical для пагинации"),
    "canonical_filters": ("DEV", 3, "Canonical для фильтров по утверждённой логике"),
    # микроразметка и мета
    "schema_org_website": ("DEV", 3, "Organization и WebSite в JSON-LD"),
    "schema_breadcrumb": ("DEV", 3, "BreadcrumbList по фактическим крошкам"),
    "schema_product": ("DEV", 6, "Product и Offer с актуальными полями"),
    "schema_rating": ("DEV", 4, "AggregateRating и Review по видимым значениям"),
    "schema_itemlist": ("DEV", 3, "ItemList на листингах"),
    "schema_faq": ("DEV", 2, "FAQPage только на страницах с блоком вопросов"),
    "schema_valid": ("DEV", 4, "Исправить ошибки валидации разметки"),
    "schema_format_kept": ("DEV", 4, "Вернуть форматы разметки текущего прода"),
    "schema_duplicates": ("DEV", 3, "Убрать дубли сущностей JSON-LD и microdata"),
    "schema_urls": ("DEV", 2, "Актуальные url, image, sameAs в разметке"),
    "og_title_description": ("DEV", 2, "Заполнить og:title и og:description"),
    "og_url": ("DEV", 1.5, "og:url = canonical"),
    "og_image": ("DEV", 2, "og:image: абсолютный адрес, 200, нужный размер"),
    "og_misc": ("DEV", 1, "og:type, og:site_name, og:locale"),
    "twitter_card": ("DEV", 1, "Теги twitter:*"),
    "viewport": ("DEV", 0.5, "Точная строка meta viewport"),
    "charset": ("DEV", 0.5, "meta charset в первых 1024 байтах head"),
    "lang": ("DEV", 0.5, "lang=\"ru\" у html"),
    # --- дополнительные проверки
    "meta_placeholders": ("DEV", 2, "Убрать заглушки (title, metaDescription, undefined) из шаблонов"),
    "title_quality": ("SEO", 4, "Переписать title по кластеру, длина в пределах нормы"),
    "description_quality": ("SEO", 3, "Заполнить и сократить description"),
    "h1_single": ("DEV", 2, "Один H1 на странице в шаблонах"),
    "headings_structure": ("SEO", 2, "Структура H2–H3 без пропусков уровней"),
    "dup_title": ("SEO", 3, "Уникальные title на всех индексируемых страницах"),
    "dup_description": ("SEO", 3, "Уникальные description"),
    "dup_h1": ("SEO", 2, "Уникальные H1"),
    "duplicate_content": ("SEO", 4, "Склеить или переписать дубли контента"),
    "canonical_chain": ("DEV", 3, "Canonical сразу на конечный адрес, без цепочек"),
    "canonical_header_conflict": ("DEVOPS", 1.5, "Согласовать canonical в HTTP-заголовке Link и в HTML"),
    "sitemap_canonical_match": ("SEO", 2, "В карте только адреса, совпадающие с canonical"),
    "hreflang_valid": ("DEV", 2, "Корректные hreflang: коды, самоссылка, абсолютные URL"),
    "soft_404": ("DEV", 2, "404-код для страниц «не найдено»"),
    "robots_noindex_blocked": ("SEO", 1, "Открыть в robots.txt страницы с noindex или убрать их из индекса иначе"),
    "robots_txt_valid": ("DEVOPS", 1, "Исправленный robots.txt"),
    "robots_blocks_pages": ("DEV", 2, "Открыть в robots.txt нужные страницы и ресурсы"),
    "robots_vs_sitemap": ("SEO", 1.5, "Убрать из карты закрытые URL или открыть их в robots.txt"),
    "yandex_clean_param": ("SEO", 1, "Директива Clean-param для параметров"),
    "ai_bots_policy": ("SEO", 1, "Решение по доступу ИИ-ботов и правила в robots.txt"),
    "llms_txt": ("CONTENT", 1.5, "Файл llms.txt с описанием сайта и ключевых разделов"),
    "url_hygiene": ("DEV", 4, "Единый вид внутренних ссылок: регистр, слеш, без меток и //"),
    "mixed_content": ("DEV", 3, "Все ресурсы по https"),
    "sitemap_url_hygiene": ("DEV", 2, "Чистая карта: без дублей, undefined, чужих хостов и http"),
    "test_urls_exposed": ("DEV", 1, "Закрыть или удалить тестовые копии, убрать из карты"),
    "link_anchor_quality": ("SEO", 3, "Содержательные анкоры вместо «подробнее» и пустых ссылок"),
    "index_orphans": ("SEO", 4, "Внутренние ссылки на страницы-сироты из индекса или вывод ненужных из индекса"),
    "crawl_depth": ("SEO", 3, "Схема перелинковки для глубоких страниц"),
    "compression": ("DEVOPS", 1, "Включить gzip или brotli для HTML"),
    "cache_headers": ("DEVOPS", 1, "Корректный заголовок Cache-Control"),
    "vary_user_agent": ("DEVOPS", 1, "Vary: User-Agent там, где ответ зависит от UA"),
    "bot_head_parity": ("DEV", 4, "Согласованные метатеги сопоставимых отрисованных версий с учётом рендеринга для ботов"),
    "conditional_304": ("DEVOPS", 3, "ETag или Last-Modified и ответ 304 на условный запрос"),
    "security_headers": ("DEVOPS", 2, "HSTS, X-Content-Type-Options, Referrer-Policy, защита от фреймов"),
    "tls_cert": ("DEVOPS", 2, "Действующий сертификат на всех хостах сайта"),
    "html_weight": ("DEV", 3, "Сократить вес HTML и размер DOM"),
    "render_blocking": ("DEV", 4, "async/defer для скриптов, критичный CSS"),
    "images_perf": ("DEV", 3, "width/height, lazy-loading и современные форматы"),
}

DEFAULT_PRIORITY = {"redirect_www": "Критично"}  # в чек-листе вместо приоритета остался шаблон {{USERNAME}}

# если проверки нет в PLAN: роль и часы по блоку чек-листа
BLOCK_DEFAULT = {
    "Редиректы": ("DEVOPS", 2), "Коды ответа": ("DEV", 3), "Рендеринг": ("DEV", 6), "Внутренние ссылки": ("DEV", 4),
    "Meta robots и X-Robots-Tag": ("DEV", 2), "Canonical": ("DEV", 3), "Sitemap.xml": ("DEV", 2), "Микроразметка": ("DEV", 3),
    "Favicon": ("DEV", 1), "Open Graph": ("DEV", 2), "Twitter Card": ("DEV", 1), "Viewport": ("DEV", 0.5), "Прочие мета": ("DEV", 1),
    "Итоговый краул": ("SEO", 3),
}

_overrides: dict = {"key": None, "data": {}}


def _load_overrides() -> dict[str, tuple[str, float, str]]:
    path = (os.environ.get("PLAN_FILE") or "").strip()
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    key = (str(p), p.stat().st_mtime)
    if _overrides["key"] == key:
        return _overrides["data"]
    out: dict[str, tuple[str, float, str]] = {}
    try:
        text = p.read_text(encoding="utf-8-sig")
        delim = ";" if text.splitlines()[0].count(";") >= text.splitlines()[0].count(",") else ","
        for r in csv.DictReader(text.splitlines(), delimiter=delim):
            cid = (r.get("check_id") or "").strip()
            if not cid:
                continue
            base = PLAN.get(cid, ("DEV", 2, ""))
            role = (r.get("role") or base[0]).strip().upper()
            try:
                hours = float((r.get("hours") or "").replace(",", ".")) if (r.get("hours") or "").strip() else base[1]
            except ValueError:
                hours = base[1]
            out[cid] = (role, hours, (r.get("result") or base[2]).strip())
    except (OSError, UnicodeDecodeError, IndexError):
        return {}
    _overrides.update(key=key, data=out)
    return out


def plan_for(check_id: str | None, block: str = "") -> dict:
    """{role, role_label, hours, result}."""
    ov = _load_overrides()
    if check_id and check_id in ov:
        role, hours, result = ov[check_id]
    elif check_id and check_id in PLAN:
        role, hours, result = PLAN[check_id]
    else:
        role, hours = BLOCK_DEFAULT.get(block, ("DEV", 2))
        result = ""
    return {"role": role, "role_label": ROLES.get(role, role), "hours": float(hours), "result": result or _RESULT_BY_ROLE.get(role, "")}


def normalize_priority(raw, check_id: str | None = None) -> str:
    """Приоритет из чек-листа к одному из PRIORITIES; шаблон/пусто → значение по умолчанию для проверки."""
    t = str(raw or "").strip().lower()
    for stem, name in (("критич", "Критично"), ("высок", "Высокий"), ("средн", "Средний"), ("низк", "Низкий")):
        if t.startswith(stem):
            return name
    return DEFAULT_PRIORITY.get(check_id or "", "Средний")


def enrich(rows: list[dict]) -> list[dict]:
    """Добавляет к строкам чек-листа: нормализованный priority (исходный — priority_raw), role, hours, result, wave."""
    from .guidance import normalize_rows
    normalize_rows(rows)
    for r in rows:
        if "priority_raw" not in r:
            r["priority_raw"] = r.get("priority")
        r["priority"] = normalize_priority(r.get("priority_raw"), r.get("check_id"))
        r.update(plan_for(r.get("check_id"), r.get("block") or ""))
        r["wave"] = WAVE[r["priority"]]
    return rows


def warn_share() -> float:
    try:
        return float(os.environ.get("PLAN_WARN_SHARE") or 0.5)
    except ValueError:
        return 0.5


def hours_for(hours: float, status: str, n_nets: int = 1) -> float:
    """Часы по статусу: «Не пройдено» — полностью, «Требует внимания» — доля, остальное — 0."""
    k = 1.0 if status == "fail" else warn_share() if status == "warn" else 0.0
    mult = max(1, n_nets) if os.environ.get("PLAN_PER_NETWORK") == "1" else 1
    return round(hours * k * mult, 2)


def build_tasks(rows: list[dict], results: dict[str, dict[str, dict]]) -> list[dict]:
    """Задачи плана работ: по одной на пункт, у которого в какой-либо сети статус «Не пройдено» или «Требует внимания»."""
    nets = sorted(results)
    tasks = []
    for r in rows:
        cid = r.get("check_id")
        if not cid:
            continue
        bad = {n: results[n][cid] for n in nets if cid in results[n] and results[n][cid]["status"] in ("fail", "warn")}
        if not bad:
            continue
        status = "fail" if any(x["status"] == "fail" for x in bad.values()) else "warn"
        first = next(iter(bad.values()))
        tasks.append({
            "check_id": cid, "block": r.get("block") or "", "title": r["title"], "priority": r["priority"], "wave": r["wave"],
            "status": status, "nets": list(bad), "summary": first["summary"], "role": r["role"], "role_label": r["role_label"],
            "hours": hours_for(r["hours"], status, len(bad)), "result": r["result"],
        })
    tasks.sort(key=lambda t: (t["wave"], t["status"] != "fail", -t["hours"], t["block"], t["title"]))
    return tasks


def totals_by_role(tasks: list[dict]) -> list[dict]:
    agg: dict[str, dict] = {}
    for t in tasks:
        a = agg.setdefault(t["role"], {"role": t["role"], "role_label": t["role_label"], "tasks": 0, "hours": 0.0})
        a["tasks"] += 1
        a["hours"] = round(a["hours"] + t["hours"], 2)
    return sorted(agg.values(), key=lambda a: -a["hours"])


def fmt_hours(h: float) -> str:
    return (f"{h:g}").replace(".", ",")
