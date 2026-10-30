"""Списки ключей и доступов внешних сервисов (PageSpeed, Google Search Console) из env.

Несколько ключей нужны, чтобы не упираться в суточные лимиты: клиент берёт их по кругу и пропускает исчерпанные.

  PAGESPEED_API_KEYS=key1,key2,key3          # список (через запятую, ; или перенос строки)
  PAGESPEED_API_KEY=key                      # одиночный ключ тоже работает (добавится в список)
  PAGESPEED_API_KEY_1 … PAGESPEED_API_KEY_9  # нумерованные тоже подхватываются

  GSC_SERVICE_ACCOUNT_FILES=a.json,b.json    # общие сервисные аккаунты
  GSC_SERVICE_ACCOUNT_FILE=a.json            # одиночный (как раньше)
  NET_<СЕТЬ>_GSC_FILES=chizhik.json          # аккаунты только для этой сети (приоритетнее общих)
  NET_<СЕТЬ>_GSC_PROPERTIES=sc-domain:5ka.ru,https://5ka.ru/   # ресурсы Search Console этой сети

Пути к файлам — абсолютные, от корня проекта или из папки service_accaunt/. Значения ключей нигде не печатаются.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Mapping

ROOT = Path(__file__).resolve().parent.parent
SA_DIRS = (ROOT, ROOT / "service_accaunt", ROOT.parent / "service_accaunt")


def _net_key(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()


def split_list(*values: str | None) -> list[str]:
    out: list[str] = []
    for v in values:
        for part in re.split(r"[,;\n]+", v or ""):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def pagespeed_keys(env: Mapping[str, str] | None = None) -> list[str]:
    env = env if env is not None else os.environ
    numbered = [env.get(f"PAGESPEED_API_KEY_{i}") for i in range(1, 10)]
    return split_list(env.get("PAGESPEED_API_KEYS"), env.get("PAGESPEED_API_KEY"), *numbered)


def resolve_file(name: str) -> Path | None:
    p = Path(name).expanduser()
    if p.is_absolute():
        return p if p.is_file() else None
    for d in SA_DIRS:
        if (d / p).is_file():
            return d / p
    return None


def gsc_accounts(network: str | None = None, env: Mapping[str, str] | None = None) -> tuple[list[Path], list[str]]:
    """(найденные файлы, имена не найденных файлов). Для сети сначала её собственные аккаунты, затем общие."""
    env = env if env is not None else os.environ
    names: list[str] = []
    if network:
        names += split_list(env.get(f"NET_{_net_key(network)}_GSC_FILES"), env.get(f"NET_{_net_key(network)}_GSC_FILE"))
    if not names:
        names = split_list(env.get("GSC_SERVICE_ACCOUNT_FILES"), env.get("GSC_SERVICE_ACCOUNT_FILE"))
    found, missing = [], []
    for n in names:
        p = resolve_file(n)
        (found if p else missing).append(p or n)  # type: ignore[arg-type]
    return found, missing  # type: ignore[return-value]


def gsc_properties(network: str, base_url: str | None, env: Mapping[str, str] | None = None) -> list[str]:
    """Ресурсы Search Console сети. Не заданы — пробуем и домен, и префикс URL от NET_<СЕТЬ>_URL."""
    env = env if env is not None else os.environ
    k = _net_key(network)
    props = split_list(env.get(f"NET_{k}_GSC_PROPERTIES"), env.get(f"NET_{k}_GSC_PROPERTY"))
    if props or not base_url:
        return props
    host = re.sub(r"^https?://", "", base_url).strip("/").split("/")[0].lower()
    bare = host[4:] if host.startswith("www.") else host
    return [f"sc-domain:{bare}", base_url.rstrip("/") + "/"]


def integrations_from_env(variables: Mapping[str, tuple[str, str]], opt: Callable[[str], str | None]) -> dict[str, str | None]:
    """Как раньше {имя: значение|None}, но PageSpeed и GSC понимают списки."""
    out = {name: opt(var) for name, (var, _label) in variables.items()}
    ps = pagespeed_keys()
    out["pagespeed"] = ps[0] if ps else None
    files = split_list(os.environ.get("GSC_SERVICE_ACCOUNT_FILES"), os.environ.get("GSC_SERVICE_ACCOUNT_FILE"))
    per_net = any(k.startswith("NET_") and k.endswith(("_GSC_FILES", "_GSC_FILE")) and v.strip() for k, v in os.environ.items())
    out["gsc"] = (files[0] if files else None) or ("по сетям" if per_net else None)
    return out


def describe(network: str | None = None, base_url: str | None = None) -> dict:
    """Сводка без секретов: сколько ключей и аккаунтов найдено (для страницы «Доступ»)."""
    files, missing = gsc_accounts(network)
    return {"pagespeed_keys": len(pagespeed_keys()), "gsc_accounts": len(files), "gsc_missing": missing,
            "gsc_properties": gsc_properties(network, base_url) if network else []}
