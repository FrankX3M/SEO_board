"""Shared presentation rules for web, dashboard and exported checklists."""
META_BLOCK = "Мета-теги и микроразметка"
META_BLOCKS = {"Микроразметка", "Open Graph", "Twitter Card", "Viewport", "Прочие мета", "Мета-теги и заголовки", "Meta robots и X-Robots-Tag"}
TITLES = {
    "status_200": "HTTP-статусы HTML-страниц: ошибки, редиректы и ответы 200",
    "bot_head_parity": "Мета-теги в сопоставимых версиях страницы с учётом рендеринга",
    "vary_user_agent": "Разделение кеша для версий бота и пользователя (Vary)",
    "cwv": "Скорость загрузки — Google PageSpeed Insights API (Lighthouse, mobile)",
    "alt": "Изображения без alt на страницах справочника",
    "redirect_www": "Редирект с альтернативного хоста на основной (www / без www)",
}
CRITERIA = {
    "noindex_where_needed": "На страницах, отмеченных noindex в справочнике, и типах из NOINDEX_TYPES ожидается директива noindex в HTML или X-Robots-Tag. Если таких страниц нет в выборке, результат — нет данных.",
    "status_200": "По справочнику проверяем конечные ответы plain/bot: ожидается 200. При наличии краула проверяем HTML-URL: 4xx/5xx — ошибки, 3xx — замечания по ссылкам, код 0 — отсутствие HTTP-ответа. Источник указан в результате.",
    "bot_head_parity": "Для 5ka рендеринг только для ботов: различия сырого plain и bot ожидаемы. Сравниваем bot с browser и, если собран, browser-plain. Ожидаем совпадение title, description, H1, canonical и robots в сопоставимых версиях. BOT_RENDER_ONLY=0 включает строгую сверку plain/bot.",
    "vary_user_agent": "Если HTML зависит от User-Agent, в обоих ответах ожидается Vary: User-Agent или Vary: *. Это позволяет кешу различать версии. Отсутствие заголовка — повод проверить настройки CDN, а не доказанная ошибка индексации. В «Как проверить» сравните Vary у plain и bot.",
    "canonical_pagination": "При ответе 200 допустим canonical на текущую страницу пагинации или на основную страницу без параметров. Это согласованная политика, дополнительного подтверждения не требуется.",
    "url_hygiene": "Ожидаем внутренние ссылки без http, пробелов, двойных слешей, index.php/html и меток. Отличие завершающего слеша от большинства — справочная информация: само по себе оно не доказывает дубль или ошибку. Адрес после «напр.» — найденная ссылка на указанной странице.",
    "alt": "Проверяется HTML справочника (главная, листинги, товары), а не вся выгрузка краула. Ожидается доля изображений без alt не выше ALT_WARN_SHARE; ALT_FAIL_SHARE задаёт порог ошибки. Два проблемных URL здесь не означают две ошибки во всём крауле.",
    "cwv": "Отдельный замер через Google PageSpeed Insights API, а не Search Console API: лабораторные LCP ≤ CWV_LCP_MS, CLS ≤ CWV_CLS, TBT ≤ CWV_TBT_MS. TBT не заменяет полевой INP. Нужны включённые внешние сервисы и доступ к PageSpeed; отсутствие замера означает пропуск, а не успешную проверку.",
    "crawl_overview": "Декомпозиция всей импортированной выгрузки Screaming Frog: HTTP-ответы, индексируемость, ссылки, canonical, мета и глубина. Числа отражают доступные колонки CSV. Canonical на другую страницу и noindex сами по себе не являются ошибками. Alt в этом импорте не измеряется; см. отдельную проверку выборки.",
}


def block_name(name):
    return META_BLOCK if name in META_BLOCKS else name


def normalize_rows(rows):
    for row in rows:
        cid = row.get("check_id")
        row["block"] = block_name(row.get("block") or "Прочее")
        if cid in TITLES:
            row["title"] = TITLES[cid]
        if cid in CRITERIA:
            row["criterion"] = CRITERIA[cid]
        if cid == "alt" and row["block"] == "Итоговый краул":
            row["block"] = "Внутренние ссылки"
        if cid == "cwv":
            row["block"] = "Скорость загрузки"
        if cid == "bot_head_parity":
            row["block"] = META_BLOCK
        if cid and cid.startswith("redirect_"):
            row["criterion"] = "Ожидается один постоянный редирект 301 или 308 на конечный URL с ответом 200. Форма слеша определяется по фактическому ответу исходного адреса. В результате показана полная цепочка; дополнительные переходы сохраняются как замечание."
    # Keep every group contiguous, preserving its original order.
    order = list(dict.fromkeys(r["block"] for r in rows))
    rows.sort(key=lambda r: order.index(r["block"]))
    return rows
