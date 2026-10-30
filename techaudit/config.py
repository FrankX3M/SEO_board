"""Настройки доступа. Единственное место, где собираются настройки проекта.

Источник правды — веб-интерфейс («Доступ и настройки»): сети, токены, User-Agent, пути к sitemap,
ключи и пороги хранятся в БД (см. appsettings.py) и подставляются в окружение при каждом load_settings().
.env — необязательный запасной вариант: из него берётся всё, что в интерфейсе не задано.

Все скрипты проекта получают токен и User-Agent только отсюда. Проверяется ПРОД.
Токен сайта обязателен (он обходит антифрод); ключи внешних сервисов (Google, Вебмастер, key.so)
необязательны: без них соответствующие проверки пропускаются, а скрипт не падает (см. integrations.py).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
from . import appsettings  # noqa: E402
from .appsettings import net_key, parse_list, resolve_sitemap_url  # noqa: E402,F401
from .keys import integrations_from_env  # noqa: E402

ENV = "prod"  # единственное окружение

# Профили User-Agent по умолчанию. Можно переопределить/добавить в .env: UA_<ИМЯ>=...
BUILTIN_UA = {
    "googlebot": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "googlebot-mobile": (
        "Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36 "
        "(compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
    ),
    "yandexbot": "Mozilla/5.0 (compatible; YandexBot/3.0; +http://yandex.com/bots)",
    "desktop": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "mobile": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
    ),
}

# Внешние источники: имя -> (переменная окружения, название для людей). Необязательные.
INTEGRATION_VARS = {
    "pagespeed": ("PAGESPEED_API_KEY", "PageSpeed / Lighthouse (мобилопригодность, Core Web Vitals)"),
    "gsc": ("GSC_SERVICE_ACCOUNT_FILE", "Google Search Console (URL Inspection, карты сайта)"),
    "yandex_webmaster": ("YANDEX_WEBMASTER_TOKEN", "Яндекс Вебмастер (проверка URL, карты, индекс)"),
    "keyso": ("KEYSO_TOKEN", "key.so (выгрузка проиндексированных страниц)"),
    "ai": ("AI_API_KEY", "ИИ-API, OpenAI-совместимый (сводка отчёта и черновики комментариев, run_checks.py --ai)"),
}


class ConfigError(Exception):
    pass


def _bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _opt(name: str) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else None


@dataclass(frozen=True)
class NetworkCfg:
    name: str
    url: str | None  # адрес прода, как его надо запрашивать (схема + хост, с www или без)
    token: str | None  # токен этой сети (NET_<СЕТЬ>_TOKEN); общий SP_TOKEN — только запасной вариант
    token_header: str | None = None  # имя заголовка этой сети, если отличается от SP_TOKEN_HEADER
    sitemaps: tuple[str, ...] = ()  # карты сайта, заданные вручную: полный URL или путь от адреса сети
    sitemaps_robots: bool = True  # искать карты ещё и в robots.txt
    sitemaps_fallback: bool = True  # если карт не нашлось — пробовать /sitemap.xml

    def base_url(self) -> str:
        if not self.url:
            raise ConfigError(f"Для сети '{self.name}' не задан адрес прода: укажите его в «Доступ и настройки → Сети»")
        return self.url.rstrip("/")

    def sitemap_urls(self) -> list[str]:
        """Заданные вручную карты в виде полных URL."""
        if not self.url:
            return []
        out = []
        for item in self.sitemaps:
            u = resolve_sitemap_url(self.base_url(), item)
            if u and u not in out:
                out.append(u)
        return out


@dataclass
class Settings:
    networks: dict[str, NetworkCfg]
    token_header: str
    default_token: str | None
    use_token: bool
    use_ua: bool
    ua_profiles: dict[str, str]
    ua_default: str
    plain_ua: str
    integrations: dict[str, str | None]  # имя -> значение (секрет) или None
    timeout: float
    concurrency: int
    render_concurrency: int
    delay: float
    verify_tls: bool
    probe_limit: int  # сколько URL каждого типа брать для проб
    index_skip_types: tuple[str, ...]  # типы, для которых не проверяем /index.php и /index.html
    data_dir: Path
    registry_dir: Path
    env_file: str | None = None  # какой файл .env прочитан (для вывода в план)
    from_db: bool = False  # настройки из БД (веб-интерфейс) подставлены


def find_env_file(explicit: str | os.PathLike | None = None) -> Path | None:
    """Где лежит .env. Порядок: явный путь → TECHAUDIT_ENV → .env уровнем выше проекта → .env в проекте.

    Файл выше проекта безопаснее: он не попадает в git и архивы проекта и общий для всех скриптов.
    """
    if explicit:
        return Path(explicit)
    var = os.environ.get("TECHAUDIT_ENV")
    if var:
        return Path(var).expanduser()
    for cand in (ROOT.parent / ".env", ROOT / ".env"):
        if cand.is_file():
            return cand
    return None


def load_settings(env_file: str | os.PathLike | None = None) -> Settings:
    """Собирает настройки: .env/окружение как запасной вариант, поверх — значения из БД (веб-интерфейс)."""
    found = find_env_file(env_file)
    if found is not None and (found.is_file() or str(found) == os.devnull):
        load_dotenv(found, override=False)
    else:
        found = None
    data_dir_boot = Path(os.environ.get("DATA_DIR") or "data")
    applied = appsettings.apply(data_dir_boot if data_dir_boot.is_absolute() else ROOT / data_dir_boot)

    names = [n.strip() for n in (os.environ.get("NETWORKS") or "").split(",") if n.strip()]
    networks = {}
    for n in names:
        k = net_key(n)
        networks[n] = NetworkCfg(
            name=n,
            url=_opt(f"NET_{k}_URL") or _opt(f"NET_{k}_PROD_URL"),  # старое имя тоже понимаем
            token=_opt(f"NET_{k}_TOKEN"),
            token_header=_opt(f"NET_{k}_TOKEN_HEADER"),
            sitemaps=tuple(parse_list(os.environ.get(f"NET_{k}_SITEMAPS"))),
            sitemaps_robots=_bool(os.environ.get(f"NET_{k}_SITEMAPS_ROBOTS"), True),
            sitemaps_fallback=_bool(os.environ.get(f"NET_{k}_SITEMAPS_FALLBACK"), True),
        )

    profiles = dict(BUILTIN_UA)
    for key, val in os.environ.items():
        if key.startswith("UA_") and key != "UA_PROFILE" and val.strip():
            profiles[key[3:].lower().replace("_", "-")] = val.strip()

    def _path(var: str, default: str) -> Path:
        p = Path(os.environ.get(var) or default)
        return p if p.is_absolute() else ROOT / p

    skip = tuple(t.strip() for t in (os.environ.get("PROBE_INDEX_SKIP") or "product").split(",") if t.strip())
    return Settings(
        networks=networks,
        token_header=_opt("SP_TOKEN_HEADER") or "X-SP-Token",
        default_token=_opt("SP_TOKEN"),
        use_token=_bool(os.environ.get("USE_TOKEN"), True),
        use_ua=_bool(os.environ.get("USE_UA"), True),
        ua_profiles=profiles,
        ua_default=(_opt("UA_PROFILE") or "googlebot").lower(),
        plain_ua=(_opt("PLAIN_UA_PROFILE") or "desktop").lower(),
        integrations=integrations_from_env(INTEGRATION_VARS, _opt),  # PageSpeed/GSC понимают списки (см. keys.py)
        timeout=float(os.environ.get("REQUEST_TIMEOUT") or 30),
        concurrency=int(os.environ.get("CONCURRENCY") or 4),
        render_concurrency=int(os.environ.get("RENDER_CONCURRENCY") or 2),
        delay=float(os.environ.get("REQUEST_DELAY") or 0),
        verify_tls=_bool(os.environ.get("VERIFY_TLS"), True),
        probe_limit=int(os.environ.get("PROBE_LIMIT") or 2),
        index_skip_types=skip,
        data_dir=_path("DATA_DIR", "data"),
        registry_dir=_path("REGISTRY_DIR", "registry"),
        env_file=str(found) if found and str(found) != os.devnull else None,
        from_db=bool(applied["db"] and (applied["settings"] or applied["networks"])),
    )


def _mask(s: str | None) -> str:
    if not s:
        return "—"
    return f"{s[:3]}…({len(s)} симв.)" if len(s) > 6 else f"…({len(s)} симв.)"


@dataclass(frozen=True)
class Access:
    """Итоговые параметры доступа для сети."""

    base_url: str
    host: str
    token_header: str | None
    token: str | None
    ua_name: str | None
    ua: str | None
    warnings: tuple[str, ...] = ()
    sitemaps: tuple[str, ...] = ()  # карты, заданные вручную (полные URL)
    sitemaps_robots: bool = True
    sitemaps_fallback: bool = True

    def auth_headers(self) -> dict[str, str]:
        """Заголовки, которые уходят только на хосты этого сайта."""
        if self.token_header and self.token:
            return {self.token_header: self.token}
        return {}

    def describe(self) -> dict:
        """Описание без секретов — для --dry-run и записи в БД."""
        return {
            "env": ENV,
            "base_url": self.base_url,
            "user_agent": self.ua_name or "(по умолчанию клиента)",
            "token": f"{self.token_header}: {_mask(self.token)}" if self.token else "ВЫКЛЮЧЕН",
            "sitemaps": list(self.sitemaps), "sitemaps_robots": self.sitemaps_robots,
            "sitemaps_fallback": self.sitemaps_fallback,
            "warnings": list(self.warnings),
        }


def resolve_access(
    settings: Settings,
    net: NetworkCfg,
    *,
    token: bool | None = None,
    ua: str | None = None,
) -> Access:
    """Собирает доступ. token/ua — переопределения из CLI (None = как в .env).

    Токен обязателен: без него сайт отвечает антифродом, и результаты бессмысленны.
    Выключить его можно только явно (--no-token / настройка «Отправлять токен») — для диагностики.
    """
    base = net.base_url()
    warnings: list[str] = []

    token_on = settings.use_token if token is None else token
    token_val = net.token or settings.default_token
    if token_on and not token_val:
        raise ConfigError(
            f"Для сети '{net.name}' не задан токен: укажите его в «Доступ и настройки → Сети» "
            f"(или NET_{net_key(net.name)}_TOKEN в .env). "
            "Токен обязателен для всех запросов к сайту: он обходит антифрод. "
            "Для диагностики без токена используйте --no-token."
        )
    if token_on and not net.token:
        warnings.append(f"для сети '{net.name}' нет своего токена — используется общий SP_TOKEN "
                        f"(задайте токен в карточке сети: у каждой сети он свой)")
    if not token_on:
        warnings.append("токен выключен вручную — только для диагностики, сайт может отвечать антифродом")

    if ua is None:
        ua_name = settings.ua_default if settings.use_ua else None
    elif ua.strip().lower() in {"none", "off", ""}:
        ua_name = None
    else:
        ua_name = ua.strip()
    ua_val = None
    if ua_name:
        key = ua_name.lower()
        if key in settings.ua_profiles:
            ua_name, ua_val = key, settings.ua_profiles[key]
        elif "/" in ua_name or " " in ua_name:  # передана целая строка UA
            ua_val, ua_name = ua_name, "custom"
        else:
            raise ConfigError(
                f"Неизвестный профиль UA '{ua_name}'. Доступны: {', '.join(sorted(settings.ua_profiles))}"
            )

    return Access(
        base_url=base,
        host=(urlsplit(base).hostname or "").lower(),
        token_header=(net.token_header or settings.token_header) if token_on else None,
        token=token_val if token_on else None,
        ua_name=ua_name,
        ua=ua_val,
        warnings=tuple(warnings),
        sitemaps=tuple(net.sitemap_urls()),
        sitemaps_robots=net.sitemaps_robots,
        sitemaps_fallback=net.sitemaps_fallback,
    )
