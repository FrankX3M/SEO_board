"""Дополнительные проверки, которых нет в Excel-чек-листе (модули x_*.py).

Каждая регистрируется как обычная проверка (@check) и дополнительно описывает «строку чек-листа»: блок, пункт, критерий, приоритет.
Такие строки добавляются в конец отчёта (Excel, HTML, интерфейс), исходный файл чек-листа не меняется.
"""
from __future__ import annotations

from dataclasses import dataclass

from .core import FAIL, NA, PASS, WARN, Item, Result, check

BLOCK_ORDER = ["Индексация и дубли", "Мета-теги и заголовки", "Robots.txt и Яндекс", "URL и ссылки", "Сервер и заголовки",
               "Производительность HTML"]
CAP = 500  # сколько проблемных URL сохраняем в результат


@dataclass(frozen=True)
class ExtraMeta:
    block: str
    title: str
    criterion: str
    priority: str


EXTRA: dict[str, ExtraMeta] = {}


def xcheck(check_id: str, block: str, title: str, criterion: str, priority: str, source: str = "html"):
    """Регистрирует дополнительную проверку и её строку для отчёта."""
    def deco(fn):
        EXTRA[check_id] = ExtraMeta(block, title, criterion, priority)
        return check(check_id, title, source=source)(fn)
    return deco


def extra_rows(matched: set[str] | None = None) -> list[dict]:
    """Строки для дополнительных проверок, которых ещё нет среди строк чек-листа (matched — их check_id)."""
    matched = matched or set()
    order = {b: i for i, b in enumerate(BLOCK_ORDER)}
    items = [(cid, m) for cid, m in EXTRA.items() if cid not in matched]
    items.sort(key=lambda x: order.get(x[1].block, 99))
    return [{"row": None, "block": m.block, "title": m.title, "criterion": m.criterion, "priority": m.priority,
             "check_id": cid, "extra": True} for cid, m in items]


def fallback_rows() -> list[dict]:
    """Нет файла чек-листа: все проверки реестра; дополнительные — со своими блоками и приоритетами."""
    from .core import REGISTRY
    from .runner import load_all
    load_all()
    base = [{"row": 0, "block": "Проверки", "title": c.titles[0], "criterion": "", "priority": "", "check_id": c.id}
            for c in REGISTRY.values() if c.id not in EXTRA]
    return base + extra_rows(set())


def with_extras(rows: list[dict]) -> list[dict]:
    from .runner import load_all
    load_all()
    return list(rows) + extra_rows({r["check_id"] for r in rows if r.get("check_id")})


# ---------------------------------------------------------------- общие помощники для проверок

def graded(items: list[tuple[Item, str]], *, what: str = "страниц", ok_text: str = "", source: str = "html",
           note: str = "") -> Result:
    """items: [(Item, 'ok'|'warn'|'fail')] → Result: любой fail — «Не пройдено», иначе любой warn — «Требует внимания»."""
    if not items:
        return Result(NA, f"нет {what} для проверки", source=source)
    fails = sum(1 for _, s in items if s == "fail")
    warns = sum(1 for _, s in items if s == "warn")
    total = len(items)
    shown = [i for i, s in items if s != "ok"][:CAP] + [i for i, s in items if s == "ok"][:8]
    for it, s in items:
        it.ok = s == "ok"
    stats = {"total": total, "bad": fails + warns}
    if fails:
        head = f"{fails} из {total} {what} не соответствуют" + (f", ещё {warns} с замечаниями" if warns else "")
        return Result(FAIL, head + note, shown, source, stats)
    if warns:
        return Result(WARN, f"{warns} из {total} {what} с замечаниями" + note, shown, source, stats)
    return Result(PASS, (ok_text or f"{total} из {total} {what} соответствуют") + note, shown, source, stats)


def ws(s) -> str:
    import re
    return re.sub(r"\s+", " ", s or "").strip()


def indexable(ctx):
    from .c_seo import _indexable_pages
    return _indexable_pages(ctx)


def content_pages(ctx):
    """Все страницы выборки, включая noindex: для проверок содержимого (H1, title, description, заголовки).
    Исключены только поисковые (динамические по определению)."""
    from .core import role_of
    return [p for p in ctx.primary() if role_of(p.page_type, ctx.cs) != "search"]


def crawl_import(ctx):
    """Последняя выгрузка краула Screaming Frog для сети или None."""
    from .c_crawl import _imp
    return _imp(ctx)


def short(s: str, n: int = 70) -> str:
    s = ws(s)
    return s if len(s) <= n else s[: n - 1] + "…"
