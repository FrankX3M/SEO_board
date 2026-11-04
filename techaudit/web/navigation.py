"""Shared navigation, without changing existing resource URLs."""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def site_link(path, site):
    url = urlsplit(path)
    query = dict(parse_qsl(url.query, keep_blank_values=True))
    query['site' if url.path == '/registry' else 'net'] = site or 'all'
    if url.path == '/registry' and not site:
        query['site'] = ''
    return urlunsplit(('', '', url.path, urlencode(query), url.fragment))


def navigation(nav, path, query):
    if nav in ('dashboard',):
        group = 'overview'
    elif nav in ('checks', 'ai', 'orphans'):
        group = 'audit'
    elif nav in ('registry', 'crawl'):
        group = 'settings'
    elif nav in ('runs', 'schedules'):
        group = 'runs'
    else:
        group = 'settings'
    if path == '/orphans' and query.get('view') == 'sources':
        group = 'settings'
    tabs = {
        'overview': [('/', 'Состояние сайта'), ('/explore', 'Динамика показателей')],
        'audit': [('/checks', 'Проверки'), ('/orphans', 'Структура и индексация'), ('/ai', 'ИИ-анализ')],
        'pages': [('/reference?tab=types', 'Типы страниц'), ('/registry', 'URL'), ('/crawl', 'Данные сканирования'), ('/orphans?view=sources', 'Данные поисковых систем')],
        'runs': [('/runs', 'История'), ('/runs?view=queue', 'Очередь'), ('/schedules', 'Расписание')],
        'settings': [],
    }
    if nav in ('registry', 'crawl') or path == '/orphans' and query.get('view') == 'sources' or nav == 'reference' and query.get('tab', 'types') not in ('sites', 'log'):
        return 'settings', tabs['pages']
    return group, tabs[group]
