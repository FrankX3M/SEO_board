"""Настройки, которые меняются в веб-интерфейсе («Доступ и настройки»), и их хранение в БД.

Единый источник правды — таблицы app_settings и app_networks в data/techaudit.db. При каждом load_settings()
значения из БД подставляются в os.environ (поверх .env и переменных процесса), поэтому весь остальной код,
который читает окружение, и консольные скрипты работают без изменений и видят те же настройки, что и интерфейс.
.env остаётся необязательным запасным вариантом: всё, что в интерфейсе не задано, берётся из него.

Секреты (токены, ключи) хранятся в БД, в интерфейсе, логах и записях запусков не показываются.
"""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_networks (
  name TEXT PRIMARY KEY,
  position INTEGER NOT NULL DEFAULT 0,
  url TEXT NOT NULL DEFAULT '',            -- адрес прода: схема + хост (с www или без — как отдаётся правильно)
  token TEXT NOT NULL DEFAULT '',          -- секрет
  token_header TEXT NOT NULL DEFAULT '',   -- пусто = общий заголовок
  sitemaps TEXT NOT NULL DEFAULT '',       -- по одной карте в строке: полный URL или путь от адреса сети
  sitemaps_robots INTEGER NOT NULL DEFAULT 1,   -- искать карты ещё и в robots.txt
  sitemaps_fallback INTEGER NOT NULL DEFAULT 1, -- если ничего не найдено, пробовать /sitemap.xml
  gsc_files TEXT NOT NULL DEFAULT '',
  gsc_properties TEXT NOT NULL DEFAULT ''
);
"""

GROUPS = [
    ("access", "Доступ к сайту", "Токен и User-Agent общие для всех скриптов: сбор, проверки, краул, веб-интерфейс."),
    ("requests", "Запросы к прод-сайту", "Нагрузка и поведение сборщика."),
    ("external", "Внешние сервисы", "Нет ключа — связанные проверки получают статус «пропущено», прогон не падает."),
    ("ai", "ИИ-анализ", "OpenAI-совместимый API. ИИ не меняет статусы проверок, только пишет сводку и черновики комментариев."),
    ("thresholds", "Пороги проверок", "Значения, с которыми сравниваются метрики страниц."),
    ("misc", "Прочее", ""),
]

# kind: text | secret | secret_area | bool | int | float | area | ua
FIELDS: list[dict] = [
    dict(key="USE_TOKEN", group="access", kind="bool", default="1", label="Отправлять токен",
         help="Токен обходит антифрод; выключать только для диагностики."),
    dict(key="SP_TOKEN_HEADER", group="access", kind="text", default="X-SP-Token", label="Имя заголовка токена"),
    dict(key="SP_TOKEN", group="access", kind="secret", label="Общий токен (запасной)",
         help="Используется, только если у сети нет своего токена. Токен каждой сети задаётся в её карточке."),
    dict(key="USE_UA", group="access", kind="bool", default="1", label="Подменять User-Agent"),
    dict(key="UA_PROFILE", group="access", kind="ua", default="googlebot", label="UA для бота",
         help="С UA поисковика сервер отдаёт отрендеренную страницу (варианты bot и browser)."),
    dict(key="PLAIN_UA_PROFILE", group="access", kind="ua", default="desktop", label="UA обычного клиента",
         help="Вариант plain и тесты адресов (редиректы, 404, robots, sitemap)."),

    dict(key="REQUEST_TIMEOUT", group="requests", kind="float", default="30", label="Таймаут запроса, с"),
    dict(key="CONCURRENCY", group="requests", kind="int", default="4", label="Параллельных запросов"),
    dict(key="RENDER_CONCURRENCY", group="requests", kind="int", default="2", label="Параллельных рендеров"),
    dict(key="REQUEST_DELAY", group="requests", kind="float", default="0", label="Пауза между стартами запросов, с"),
    dict(key="VERIFY_TLS", group="requests", kind="bool", default="1", label="Проверять сертификат TLS"),
    dict(key="PROBE_LIMIT", group="requests", kind="int", default="2", label="URL каждого типа для тестов адресов"),
    dict(key="PROBE_INDEX_SKIP", group="requests", kind="text", default="product",
         label="Типы без тестов /index.php и /index.html", help="Через запятую."),

    dict(key="EXTERNAL_CHECKS", group="external", kind="bool", default="0", label="Внешние запросы включены",
         help="PageSpeed и Search Console тратят квоту ключей; результат кешируется."),
    dict(key="PAGESPEED_API_KEYS", group="external", kind="secret_area", label="Ключи PageSpeed",
         help="По одному в строке или через запятую; лучше из разных проектов Google Cloud — квота считается на проект. Используются по кругу."),
    dict(key="GSC_SERVICE_ACCOUNT_FILES", group="external", kind="text", label="Сервисные аккаунты GSC (общие)",
         help="Имена json-файлов через запятую: из папки service_accaunt/ или абсолютные пути. Для отдельной сети — в её карточке."),
    dict(key="YANDEX_WEBMASTER_TOKEN", group="external", kind="secret", label="Токен Яндекс Вебмастера"),
    dict(key="KEYSO_TOKEN", group="external", kind="secret", label="Токен key.so"),
    dict(key="EXT_CACHE_HOURS", group="external", kind="float", default="24", label="Кеш внешних ответов, ч"),

    dict(key="AI_API_KEY", group="ai", kind="secret", label="Ключ API"),
    dict(key="AI_API_URL", group="ai", kind="text", default="https://api-copilot.x5.ru/v1", label="Адрес API"),
    dict(key="AI_MODEL", group="ai", kind="text", default="x5-airun-medium", label="Модель"),
    dict(key="AI_MAX_REQUESTS", group="ai", kind="int", default="20", label="Лимит запросов за запуск"),
    dict(key="AI_TIMEOUT", group="ai", kind="float", default="180", label="Таймаут, с"),
    dict(key="AI_CONCURRENCY", group="ai", kind="int", default="2", label="Параллельных запросов"),

    dict(key="TTFB_MAX_MS", group="thresholds", kind="int", default="600", label="TTFB, мс (максимум)"),
    dict(key="MIN_TEXT_CHARS", group="thresholds", kind="int", default="500", label="Текст без JS, симв. (минимум)"),
    dict(key="MIN_LINKS", group="thresholds", kind="int", default="10", label="Ссылок на странице (минимум)"),
    dict(key="ALT_WARN_SHARE", group="thresholds", kind="float", default="0.05", label="Картинки без alt: допуск (доля)"),
    dict(key="ALT_FAIL_SHARE", group="thresholds", kind="float", default="0.20", label="Картинки без alt: ошибка (доля)"),
    dict(key="TITLE_MIN", group="thresholds", kind="int", default="10", label="Title: минимум знаков"),
    dict(key="TITLE_MAX", group="thresholds", kind="int", default="70", label="Title: максимум знаков"),
    dict(key="DESC_MIN", group="thresholds", kind="int", default="50", label="Description: минимум знаков"),
    dict(key="DESC_MAX", group="thresholds", kind="int", default="200", label="Description: максимум знаков"),
    dict(key="MAX_DEPTH", group="thresholds", kind="int", default="4", label="Глубина по краулу, кликов"),
    dict(key="HTML_WARN_KB", group="thresholds", kind="int", default="500", label="Вес HTML: предупреждение, КБ"),
    dict(key="HTML_FAIL_KB", group="thresholds", kind="int", default="1500", label="Вес HTML: ошибка, КБ"),
    dict(key="DOM_WARN", group="thresholds", kind="int", default="1500", label="DOM: предупреждение, элементов"),
    dict(key="DOM_FAIL", group="thresholds", kind="int", default="3000", label="DOM: ошибка, элементов"),
    dict(key="BLOCKING_SCRIPTS_MAX", group="thresholds", kind="int", default="2", label="Блокирующих скриптов в head"),
    dict(key="CERT_MIN_DAYS", group="thresholds", kind="int", default="30", label="Сертификат: минимум дней до конца"),
    dict(key="HSTS_MIN_AGE", group="thresholds", kind="int", default="15552000", label="HSTS max-age, с (минимум)"),
    dict(key="DUP_TEXT_SIM", group="thresholds", kind="float", default="0.9", label="Дубли текста: доля совпадения"),
    dict(key="SITEMAP_MAX_AGE_DAYS", group="thresholds", kind="int", default="14", label="Свежесть sitemap, дней"),
    dict(key="OG_MIN_WIDTH", group="thresholds", kind="int", default="600", label="og:image: ширина, px"),
    dict(key="OG_MIN_HEIGHT", group="thresholds", kind="int", default="315", label="og:image: высота, px"),

    dict(key="LIVE_LIMIT", group="misc", kind="int", default="60", label="URL на каждый вид живой выборки"),
    dict(key="PAGINATION_PARAM", group="misc", kind="text", default="page", label="Параметр пагинации"),
    dict(key="OLD_HOSTS", group="misc", kind="text", label="Старые/тестовые домены",
         help="Через запятую: их не должно быть в canonical и разметке."),
    dict(key="NOINDEX_TYPES", group="misc", kind="text", label="Типы, где noindex — норма", help="Через запятую."),
    dict(key="ROLE_OVERRIDES", group="misc", kind="text", label="Роли нестандартных типов", help="тип:роль, через запятую."),
    dict(key="NAV_TYPES", group="misc", kind="text", label="Разводящие типы без товаров", help="Через запятую."),
    dict(key="CHECKLIST_XLSX", group="misc", kind="text", label="Чек-лист Excel", help="Путь; пусто — *тсх*.xlsx рядом с проектом."),
    dict(key="PLAN_WARN_SHARE", group="misc", kind="float", default="0.5", label="План работ: доля часов для «Требует внимания»"),
    dict(key="PLAN_PER_NETWORK", group="misc", kind="bool", default="0", label="План работ: часы на каждую сеть отдельно"),
    dict(key="PLAN_FILE", group="misc", kind="text", label="План работ: свой CSV"),
    dict(key="GSC_PAGES_DAYS", group="misc", kind="int", default="90", label="Сироты: глубина выгрузки страниц GSC, дней",
         help="За какой период брать страницы с показами в Google (Search Analytics)."),
    dict(key="ORPHAN_PROBE_LIMIT", group="misc", kind="int", default="300", label="Сироты: живая проверка, URL за сравнение",
         help="Сколько адресов «нет в краулe» запросить с сайта, чтобы отличить живых сирот от устаревших записей индекса. 0 — не проверять."),
    dict(key="CRAWL_MAX_MB", group="misc", kind="int", default="1500", label="Краул: максимальный размер CSV, МБ"),
]
FIELD = {f["key"]: f for f in FIELDS}
SECRET_KEYS = {f["key"] for f in FIELDS if f["kind"].startswith("secret")}

# Переменные, которые приложение подставляет в окружение само (сети). Нужны, чтобы корректно откатывать подстановку.
NET_SUFFIXES = ("URL", "TOKEN", "TOKEN_HEADER", "SITEMAPS", "SITEMAPS_ROBOTS", "SITEMAPS_FALLBACK", "GSC_FILES", "GSC_PROPERTIES")

_applied: dict[str, str | None] = {}  # что мы переопределили -> исходное значение (None = не было)


def net_key(name: str) -> str:
    """5ka -> 5KA, my-net -> MY_NET (для имён переменных NET_<KEY>_*)."""
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ карты сайта

def parse_list(text: str | None) -> list[str]:
    """Список из текста: по строкам, через запятую или точку с запятой; без пустых и повторов."""
    out: list[str] = []
    for part in re.split(r"[\n\r,;]+", text or ""):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def resolve_sitemap_url(base: str, item: str) -> str | None:
    """Полный URL карты: относительный путь достраивается от адреса сети. None — значение недопустимо."""
    item = (item or "").strip()
    if not item:
        return None
    u = urljoin(base.rstrip("/") + "/", item)
    p = urlsplit(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    return u


def validate_sitemaps(items: list[str], base: str = "https://example.invalid") -> list[str]:
    """Ошибки в списке карт (для показа пользователю)."""
    return [f"«{i}» — не похоже на адрес карты (нужен http(s)://… или путь вида /sitemap.xml)"
            for i in items if resolve_sitemap_url(base, i) is None]


# ------------------------------------------------------------------ подключение

def db_file(data_dir: Path) -> Path:
    return Path(data_dir) / "techaudit.db"


def _open(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    return db


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def _restore_env() -> None:
    for k, orig in _applied.items():
        if orig is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = orig
    _applied.clear()


def _put(key: str, value: str) -> None:
    if key not in _applied:
        _applied[key] = os.environ.get(key)
    os.environ[key] = value


def original_env() -> dict[str, str]:
    """Окружение процесса и .env БЕЗ подстановок из БД (для импорта из .env)."""
    env = dict(os.environ)
    for k, orig in _applied.items():
        if orig is None:
            env.pop(k, None)
        else:
            env[k] = orig
    return env


def apply(data_dir: Path) -> dict:
    """Подставляет настройки из БД в os.environ. Предыдущая подстановка сначала откатывается.

    Возвращает {'db': путь|None, 'settings': n, 'networks': n}. Нет БД — ничего не делает (работает .env).
    """
    _restore_env()
    path = db_file(data_dir)
    info = {"db": None, "settings": 0, "networks": 0}
    if not path.is_file():
        return info
    db = _open(path)
    try:
        ensure_schema(db)
        for r in db.execute("SELECT key, value FROM app_settings"):
            _put(r["key"], r["value"])
            info["settings"] += 1
        nets = db.execute("SELECT * FROM app_networks ORDER BY position, name").fetchall()
        if nets:
            _put("NETWORKS", ",".join(n["name"] for n in nets))
            for n in nets:
                k = net_key(n["name"])
                _put(f"NET_{k}_URL", n["url"])
                _put(f"NET_{k}_PROD_URL", "")
                _put(f"NET_{k}_TOKEN", n["token"])
                _put(f"NET_{k}_TOKEN_HEADER", n["token_header"])
                _put(f"NET_{k}_SITEMAPS", "\n".join(parse_list(n["sitemaps"])))
                _put(f"NET_{k}_SITEMAPS_ROBOTS", "1" if n["sitemaps_robots"] else "0")
                _put(f"NET_{k}_SITEMAPS_FALLBACK", "1" if n["sitemaps_fallback"] else "0")
                _put(f"NET_{k}_GSC_FILES", n["gsc_files"])
                _put(f"NET_{k}_GSC_FILE", "")
                _put(f"NET_{k}_GSC_PROPERTIES", n["gsc_properties"])
                _put(f"NET_{k}_GSC_PROPERTY", "")
            info["networks"] = len(nets)
        info["db"] = str(path)
    finally:
        db.close()
    return info


# ------------------------------------------------------------------ общие настройки

def get_settings(db) -> dict[str, str]:
    return {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM app_settings")}


def _clean_value(field: dict, raw: str) -> str:
    v = (raw or "").strip()
    kind = field["kind"]
    if kind == "bool":
        if v not in ("", "0", "1"):
            raise ValueError(f"{field['label']}: допустимо «по умолчанию», «да» или «нет»")
    elif kind == "int":
        if v:
            try:
                int(v)
            except ValueError:
                raise ValueError(f"{field['label']}: нужно целое число") from None
    elif kind == "float":
        if v:
            try:
                float(v.replace(",", "."))
            except ValueError:
                raise ValueError(f"{field['label']}: нужно число") from None
            v = v.replace(",", ".")
    elif kind == "secret_area":
        v = "\n".join(parse_list(v))
    return v


def save_group(db, group: str, form: dict) -> list[str]:
    """Сохраняет поля одной группы из формы. Пустое значение = убрать из БД (вернуться к .env/умолчанию).
    Секрет: пустое поле не меняет значение, флажок clear__<КЛЮЧ> удаляет его. Возвращает названия изменённых полей."""
    changed: list[str] = []
    errors: list[str] = []
    cur = get_settings(db)
    plan: list[tuple[str, str | None]] = []
    for f in FIELDS:
        if f["group"] != group:
            continue
        k = f["key"]
        if k in SECRET_KEYS:
            if form.get(f"clear__{k}"):
                plan.append((k, None))
            elif (form.get(k) or "").strip():
                try:
                    plan.append((k, _clean_value(f, form[k])))
                except ValueError as e:
                    errors.append(str(e))
            continue
        if k not in form:
            continue
        try:
            v = _clean_value(f, form[k])
        except ValueError as e:
            errors.append(str(e))
            continue
        plan.append((k, v or None))
    if errors:
        raise ValueError("; ".join(errors))
    ts = now_iso()
    for k, v in plan:
        if v is None:
            if k in cur:
                db.execute("DELETE FROM app_settings WHERE key=?", (k,))
                changed.append(FIELD[k]["label"])
        elif cur.get(k) != v:
            db.execute("INSERT INTO app_settings(key,value,updated_at) VALUES (?,?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (k, v, ts))
            changed.append(FIELD[k]["label"])
    return changed


# ------------------------------------------------------------------ профили User-Agent

UA_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


def add_ua(db, name: str, value: str, builtin: set[str]) -> str:
    name = (name or "").strip().lower().replace("_", "-")
    value = (value or "").strip()
    if not UA_RE.match(name):
        raise ValueError("имя профиля: латиница, цифры и дефис, до 31 символа")
    if name in builtin:
        raise ValueError(f"«{name}» — встроенный профиль, возьмите другое имя")
    if len(value) < 8 or "\n" in value:
        raise ValueError("укажите строку User-Agent целиком, в одну строку")
    db.execute("INSERT INTO app_settings(key,value,updated_at) VALUES (?,?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
               ("UA_" + name.replace("-", "_").upper(), value, now_iso()))
    return name


def delete_ua(db, name: str) -> None:
    db.execute("DELETE FROM app_settings WHERE key=?", ("UA_" + name.replace("-", "_").upper(),))


# ------------------------------------------------------------------ сети

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,30}$")


def list_networks(db) -> list[dict]:
    return [dict(r) for r in db.execute("SELECT * FROM app_networks ORDER BY position, name")]


def get_network(db, name: str) -> dict | None:
    r = db.execute("SELECT * FROM app_networks WHERE name=?", (name,)).fetchone()
    return dict(r) if r else None


def norm_url(raw: str) -> str:
    u = (raw or "").strip().rstrip("/")
    if not u:
        raise ValueError("укажите адрес прода, например https://www.example.ru")
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    p = urlsplit(u)
    if not p.hostname:
        raise ValueError("адрес сети некорректен")
    return f"{p.scheme.lower()}://{p.netloc}"  # только схема + хост: путь в адресе сети не нужен


def save_network(db, form: dict, *, original: str | None = None) -> str:
    """Создаёт или обновляет сеть. Токен: пусто — не менять, clear_token — удалить. Имя после создания не меняется."""
    name = (original or form.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise ValueError("ключ сети: латиница, цифры, дефис и подчёркивание (например 5ka)")
    url = norm_url(form.get("url", ""))
    sm = parse_list(form.get("sitemaps"))
    bad = validate_sitemaps(sm, url)
    if bad:
        raise ValueError("; ".join(bad))
    existing = get_network(db, name)
    if original is None and existing:
        raise ValueError(f"сеть «{name}» уже есть")
    if original is not None and not existing:
        raise ValueError(f"сеть «{name}» не найдена")
    token = existing["token"] if existing else ""
    if form.get("clear_token"):
        token = ""
    elif (form.get("token") or "").strip():
        token = form["token"].strip()
    hdr = (form.get("token_header") or "").strip()
    if hdr and not re.match(r"^[A-Za-z0-9-]+$", hdr):
        raise ValueError("имя заголовка: латиница, цифры и дефис")
    vals = dict(url=url, token=token, token_header=hdr, sitemaps="\n".join(sm),
                sitemaps_robots=1 if form.get("sitemaps_robots") else 0,
                sitemaps_fallback=1 if form.get("sitemaps_fallback") else 0,
                gsc_files=", ".join(parse_list(form.get("gsc_files"))),
                gsc_properties=", ".join(parse_list(form.get("gsc_properties"))))
    if existing:
        sets = ",".join(f"{k}=?" for k in vals)
        db.execute(f"UPDATE app_networks SET {sets} WHERE name=?", (*vals.values(), name))
    else:
        pos = db.execute("SELECT COALESCE(MAX(position),0)+1 FROM app_networks").fetchone()[0]
        cols = ["name", "position", *vals]
        db.execute(f"INSERT INTO app_networks({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", (name, pos, *vals.values()))
    return name


def delete_network(db, name: str) -> None:
    db.execute("DELETE FROM app_networks WHERE name=?", (name,))


# ------------------------------------------------------------------ импорт из .env

def env_candidates() -> dict:
    """Что есть в .env/окружении и чего ещё нет в БД (значения секретов не возвращаются)."""
    env = original_env()
    names = [n.strip() for n in (env.get("NETWORKS") or "").split(",") if n.strip()]
    keys = [f["key"] for f in FIELDS if (env.get(f["key"]) or "").strip()]
    ua = [k for k in env if k.startswith("UA_") and k not in ("UA_PROFILE",) and env[k].strip()]
    return {"networks": names, "settings": keys, "ua": ua}


def import_from_env(db, *, with_settings: bool = True) -> dict:
    """Переносит сети (и, если with_settings, остальные настройки) из .env/окружения в БД.
    Уже заданное в БД не перезаписывается."""
    env = original_env()
    cur = get_settings(db)
    rep = {"networks": 0, "settings": 0}
    ts = now_iso()
    for f in FIELDS if with_settings else ():
        v = (env.get(f["key"]) or "").strip()
        if v and f["key"] not in cur:
            db.execute("INSERT INTO app_settings(key,value,updated_at) VALUES (?,?,?)", (f["key"], v, ts))
            rep["settings"] += 1
    for k, v in env.items() if with_settings else ():
        if k.startswith("UA_") and k != "UA_PROFILE" and v.strip() and k not in cur:
            db.execute("INSERT INTO app_settings(key,value,updated_at) VALUES (?,?,?)", (k, v.strip(), ts))
            rep["settings"] += 1
    have = {n["name"] for n in list_networks(db)}
    for i, name in enumerate(n.strip() for n in (env.get("NETWORKS") or "").split(",") if n.strip()):
        if name in have:
            continue
        k = net_key(name)
        g = lambda s: (env.get(f"NET_{k}_{s}") or "").strip()  # noqa: E731
        url = g("URL") or g("PROD_URL")
        try:
            url = norm_url(url) if url else ""
        except ValueError:
            url = ""
        pos = db.execute("SELECT COALESCE(MAX(position),0)+1 FROM app_networks").fetchone()[0]
        db.execute(
            "INSERT INTO app_networks(name,position,url,token,token_header,sitemaps,sitemaps_robots,sitemaps_fallback,gsc_files,gsc_properties)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name, pos, url, g("TOKEN"), g("TOKEN_HEADER"), "\n".join(parse_list(g("SITEMAPS"))), 1, 1,
             ", ".join(parse_list(g("GSC_FILES") or g("GSC_FILE"))), ", ".join(parse_list(g("GSC_PROPERTIES") or g("GSC_PROPERTY")))))
        rep["networks"] += 1
    return rep
