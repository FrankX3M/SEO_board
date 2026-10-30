"""Сборщик HTML по справочнику страниц ПРОДА: несколько вариантов страницы + пробы.

Варианты (--variants):
  plain          сырой HTML, обычный UA            — «без рендеринга»
  bot            сырой HTML, UA поисковика         — «с рендерингом» (сервер рендерит для ботов)
  browser        Playwright, UA поисковика         — DOM после JS
  browser-plain  Playwright, обычный UA            — то, что видит живой пользователь

Пробы (probes.py, assets.py): искажённые адреса (слеш, регистр, www, http, index.php), несуществующие
страницы, robots.txt, карты сайта, favicon/манифест/og:image. Токен сайта обязателен.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import re
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime

from .comparer import compare_run, default_pairs, parse_pairs, print_diff_summary, summarize_diffs
from . import integrations
from .assets import probe_resources
from .config import ENV, Access, ConfigError, Settings, load_settings, resolve_access
from .diffreport import write_report
from .netutil import host_of, same_site
from .probes import (ALL_KINDS, KIND_LABELS, RESOURCE_KINDS, ProbeContext, ProbeSpec, parse_kinds, plan_specs,
                     print_probe_summary, summarize_probes)
from .rawfetch import fetch_raw, make_client
from .registry import RegistryEntry, build_url, load_registry, path_qs
from .db_registry import entries_for
from .storage import Store, now_iso

VARIANTS = {  # имя -> (способ получения, роль UA)
    "plain": ("raw", "plain"),
    "bot": ("raw", "bot"),
    "browser": ("rendered", "bot"),
    "browser-plain": ("rendered", "plain"),
}
DEFAULT_VARIANTS = "plain,bot,browser"

CAPTCHA_MARKERS = (b"captcha", b"smartcaptcha", b"cf-chl", b"access denied", b"attention required",
                   "доступ ограничен".encode(), "подтвердите, что вы не робот".encode())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Сбор HTML и пробы по справочнику страниц (прод)")
    p.add_argument("--network", default="all", help="ключ сети или список через запятую (по умолчанию все)")
    p.add_argument("--variants", default=DEFAULT_VARIANTS,
                   help=f"какие варианты собирать, через запятую: {', '.join(VARIANTS)} (по умолчанию {DEFAULT_VARIANTS})")
    p.add_argument("--pairs", help="какие пары сравнивать, например plain:bot,bot:browser "
                                   "(по умолчанию соседние варианты из --variants)")
    p.add_argument("--no-diff", action="store_true", help="только собрать, не сравнивать")
    p.add_argument("--types", help="только эти типы страниц, через запятую")
    p.add_argument("--limit", type=int, help="не более N URL на каждый тип в каждой сети")
    p.add_argument("--url", action="append", help="разовый путь/URL вместо справочника (можно несколько)")
    p.add_argument("--probes", default="all",
                   help="какие пробы делать: all | none | группы redirects,not_found,discovery,resources | отдельные "
                        f"виды ({', '.join(ALL_KINDS)})")
    p.add_argument("--no-probes", action="store_true", help="не делать проб (то же, что --probes none)")
    p.add_argument("--probes-only", action="store_true", help="только пробы, без сбора вариантов страниц")
    p.add_argument("--probe-limit", type=int, help="сколько URL каждого типа брать для проб (по умолчанию PROBE_LIMIT=2)")
    p.add_argument("--asset-pages", type=int, default=1, help="с скольких страниц каждого типа брать favicon/og:image")
    p.add_argument("--ua", help="UA для вариантов bot/browser: профиль (googlebot, yandexbot…) или 'none'")
    p.add_argument("--plain-ua", help="UA для вариантов plain/browser-plain и проб (по умолчанию PLAIN_UA_PROFILE=desktop)")
    p.add_argument("--token", action=argparse.BooleanOptionalAction, default=None,
                   help="слать токен сайта (по умолчанию да; --no-token — только для диагностики)")
    p.add_argument("--viewport", choices=["desktop", "mobile"], default="desktop", help="для browser-вариантов")
    p.add_argument("--concurrency", type=int)
    p.add_argument("--render-concurrency", type=int)
    p.add_argument("--delay", type=float, help="пауза между стартами запросов, сек")
    p.add_argument("--timeout", type=float, help="таймаут запроса, сек")
    p.add_argument("--settle-ms", type=int, default=0, help="доп. ожидание после загрузки (browser)")
    p.add_argument("--scroll", action="store_true", help="прокрутить страницу до конца (ленивая подгрузка)")
    p.add_argument("--no-block-assets", action="store_true", help="не блокировать картинки/шрифты/видео в browser")
    p.add_argument("--show-browser", action="store_true", help="показать окно браузера")
    p.add_argument("--tag", help="метка в идентификаторе запуска")
    p.add_argument("--env-file", help="путь к .env (по умолчанию: TECHAUDIT_ENV, затем .env уровнем выше проекта, затем в проекте)")
    p.add_argument("--debug", action="store_true", help="режим отладки: в запуск пишутся условия доступа (UA, заголовок токена) для ручной перепроверки")
    p.add_argument("--dry-run", action="store_true", help="показать план и настройки доступа, ничего не запрашивать")
    return p


def parse_variants(spec: str) -> list[str]:
    names = [v.strip() for v in spec.split(",") if v.strip()]
    bad = [v for v in names if v not in VARIANTS]
    if bad or not names:
        raise ConfigError(f"Неизвестный вариант {bad}. Доступны: {', '.join(VARIANTS)}")
    return list(dict.fromkeys(names))


def suspect_reasons(status, body: bytes, final_url: str, access, error) -> str:
    r = []
    if error:
        r.append("fetch_error")
    if status in (401, 403, 429):
        r.append("blocked_status")
    elif status and status >= 500:
        r.append("server_error")
    if final_url and not same_site(host_of(final_url), access.host):
        r.append("left_site")
    if status == 200 and len(body) < 2000:
        r.append("tiny_body")
    low = body[:150_000].lower()
    if any(m in low for m in CAPTCHA_MARKERS):
        r.append("captcha_marker")
    return ",".join(r)


def _flags(body: bytes) -> tuple[int, int]:
    # <script>/<style> вырезаем: строки внутри JS не должны считаться тегами страницы
    clean = re.sub(rb"<(script|style)\b.*?</\1\s*>", b"", body, flags=re.I | re.S)
    return (int(bool(re.search(rb"<title[\s>]", clean[:300_000], re.I))),
            int(bool(re.search(rb"<h1[\s>]", clean, re.I))))


class RateLimiter:
    def __init__(self, delay: float):
        self.delay, self._next, self._lock = delay, 0.0, asyncio.Lock()

    async def wait(self) -> None:
        if not self.delay:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._next - now
            self._next = max(now, self._next) + self.delay
        if wait > 0:
            await asyncio.sleep(wait)


def _probe_kinds(args) -> tuple[str, ...]:
    if args.no_probes:
        return ()
    try:
        return parse_kinds(args.probes)
    except ValueError as e:
        raise ConfigError(str(e)) from e


def _plan(args, settings: Settings, variants: list[str], probe_kinds):
    names = list(settings.networks) if args.network == "all" else [n.strip() for n in args.network.split(",")]
    types = {t.strip() for t in args.types.split(",")} if args.types else None
    roles = {VARIANTS[v][1] for v in variants}
    if probe_kinds:
        roles.add("plain")  # пробы идут обычным UA
    plan = []
    for name in names:
        net = settings.networks.get(name)
        if not net:
            raise ConfigError(f"Сеть '{name}' не заведена: добавьте её в веб-интерфейсе («Доступ → Сети»)")
        accesses: dict[str, Access] = {}
        if "bot" in roles:
            accesses["bot"] = resolve_access(settings, net, token=args.token, ua=args.ua)
        if "plain" in roles:
            accesses["plain"] = resolve_access(settings, net, token=args.token, ua=args.plain_ua or settings.plain_ua)
        if args.url:
            entries = [RegistryEntry(name, "adhoc", u, path_qs(u)) for u in args.url]
        else:
            entries = entries_for(settings, name)  # справочник из БД (веб-интерфейс), иначе registry/<сеть>.csv
        if types:
            entries = [e for e in entries if e.type in types]
        if args.limit:
            per_type, limited = defaultdict(int), []
            for e in entries:
                per_type[e.type] += 1
                if per_type[e.type] <= args.limit:
                    limited.append(e)
            entries = limited
        plan.append((net, accesses, entries))
    return plan


def _print_plan(plan, variants, probe_kinds, args, settings) -> None:
    print(f"Настройки: {'БД веб-интерфейса' if settings.from_db else 'БД пока пуста'}; "
          f".env: {settings.env_file or '— (не используется)'}")
    print(f"Окружение: {ENV}   варианты: {', '.join(variants) or '— (только пробы)'}")
    limit = args.probe_limit if args.probe_limit is not None else settings.probe_limit
    for net, accesses, entries in plan:
        any_access = next(iter(accesses.values()))
        print(f"\n[{net.name}] {any_access.base_url}")
        for role, access in accesses.items():
            d = access.describe()
            used = [v for v in variants if VARIANTS[v][1] == role] + (["пробы"] if role == "plain" and probe_kinds else [])
            print(f"  UA «{role}» ({', '.join(used)}): {d['user_agent']}")
        d = any_access.describe()
        print(f"  токен: {d['token']}")
        for w in sorted({w for a in accesses.values() for w in a.describe()["warnings"]}):
            print(f"  ! {w}")
        by_type = defaultdict(int)
        for e in entries:
            by_type[e.type] += 1
        if not entries:
            print("  ! справочник пуст или не найден")
        for t, n in by_type.items():
            print(f"  {t}: {n} URL")
        print(f"  запросов страниц: {len(entries) * len(variants)}")
        if probe_kinds:
            specs = plan_specs(any_access.base_url, entries, probe_kinds, limit, settings.index_skip_types)
            cnt = defaultdict(int)
            for sp in specs:
                cnt[sp.kind] += 1
            extra = [k for k in ("robots", "sitemap") if k in probe_kinds]
            res = [k for k in RESOURCE_KINDS if k in probe_kinds]
            line = ", ".join(f"{KIND_LABELS[k]} ×{n}" for k, n in cnt.items())
            print(f"  проб: {len(specs)}" + (f" ({line})" if line else "")
                  + (f" + {', '.join(KIND_LABELS[k] for k in extra)}" if extra else "")
                  + (f" + ресурсы страниц ({', '.join(KIND_LABELS[k] for k in res)})" if res else ""))
    print("\nВнешние источники (необязательны, без ключа проверки пропускаются):")
    for line in integrations.status_lines(settings):
        print(f"  {line}")


async def run_collection(args, settings: Settings) -> dict | None:
    probe_kinds = _probe_kinds(args)
    if args.probes_only and not probe_kinds:
        raise ConfigError("--probes-only без проб ничего не делает")
    variants = [] if args.probes_only else parse_variants(args.variants)
    pairs = parse_pairs(args.pairs) if (args.pairs and variants) else default_pairs(variants)
    plan = _plan(args, settings, variants, probe_kinds)
    _print_plan(plan, variants, probe_kinds, args, settings)
    limit = args.probe_limit if args.probe_limit is not None else settings.probe_limit
    total = sum(len(e) for _, _, e in plan) * len(variants)
    n_probes = sum(len(plan_specs(next(iter(a.values())).base_url, e, probe_kinds, limit, settings.index_skip_types))
                   for _, a, e in plan) if probe_kinds else 0
    if args.dry_run or (total == 0 and n_probes == 0 and not probe_kinds):
        if pairs and not args.no_diff:
            print("\nСравнение: " + ", ".join(f"{a} → {b}" for a, b in pairs))
        return None

    sem = asyncio.Semaphore(args.concurrency or settings.concurrency)
    rsem = asyncio.Semaphore(args.render_concurrency or settings.render_concurrency)
    limiter = RateLimiter(args.delay if args.delay is not None else settings.delay)
    timeout = args.timeout or settings.timeout

    store = Store(settings.data_dir)
    run_id = store.unique_run_id(
        datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{ENV}" + (f"-{args.tag}" if args.tag else ""))
    store.start_run(run_id, ENV, {
        "variants": variants, "pairs": pairs, "viewport": args.viewport, "limit": args.limit, "types": args.types,
        "probes": list(probe_kinds), "probe_limit": limit,
        "integrations": {n: bool(v) for n, v in settings.integrations.items()},
        "networks": {n.name: {r: a.describe() for r, a in acc.items()} for n, acc, _ in plan},
        **({"debug": True, "ua_strings": {r: a.ua for r, a in next(iter(plan))[1].items()}} if args.debug else {}),
    })
    rows: list[dict] = []
    probe_rows: list[dict] = []
    html_by_entry: dict[tuple, tuple] = {}  # (сеть, тип, исходный url) -> (final_url, байты) для ресурсов страниц
    done = 0

    async def record(net, access, entry, url, variant, *, status, final_url, chain, headers, ttfb, total_ms, body, error):
        nonlocal done
        mode = VARIANTS[variant][0]
        title, h1 = _flags(body)
        file_path = None
        if body or mode == "rendered":
            if mode == "rendered":
                head = (f"<!-- SOURCE_URL: {url} -->\n<!-- SAVED_AT: {now_iso()} -->\n"
                        f"<!-- FINAL_URL: {final_url} | HTTP_STATUS: {status} | ENV: {ENV} | "
                        f"VARIANT: {variant} | UA: {access.ua_name} -->\n")
                content = head.encode() + body
            else:
                content = body  # сырые байты как есть (нужны для проверки charset в первых 1024 байтах)
            file_path = store.save_html(run_id, net.name, entry.type, url, variant, content)
        row = dict(
            run_id=run_id, network=net.name, page_type=entry.type, variant=variant, ua_name=access.ua_name, mode=mode,
            source_url=entry.source_url, url=url, status=status, final_url=final_url,
            chain=json.dumps(chain, ensure_ascii=False),
            headers=json.dumps(headers, ensure_ascii=False) if headers else None,
            ttfb_ms=ttfb, total_ms=total_ms, bytes=len(body), sha256=hashlib.sha256(body).hexdigest() if body else None,
            file_path=file_path, has_title=title, has_h1=h1,
            suspect=suspect_reasons(status, body, final_url, access, error), error=error, fetched_at=now_iso(),
        )
        store.add_page(row)
        rows.append(row)
        if status == 200 and mode == "raw" and body:
            html_by_entry.setdefault((net.name, entry.type, entry.source_url), (final_url, body))
        done += 1
        flag = f"  ⚠ {row['suspect']}" if row["suspect"] else ""
        print(f"[{done}/{total}] {net.name:<10} {entry.type:<12} {variant:<13} {status or 'ERR'}  {url}{flag}", flush=True)

    async def do_raw(client, net, access, entry, variant):
        url = build_url(access.base_url, entry)
        async with sem:
            await limiter.wait()
            r = await fetch_raw(client, url, access)
        await record(net, access, entry, url, variant, status=r.status, final_url=r.final_url, chain=r.chain,
                     headers=r.headers, ttfb=r.ttfb_ms, total_ms=r.total_ms, body=r.body, error=r.error)

    async def do_render(renderer, net, access, entry, variant):
        url = build_url(access.base_url, entry)
        async with rsem:
            await limiter.wait()
            r = await renderer.render(url, timeout_ms=int(timeout * 1000), settle_ms=args.settle_ms, scroll=args.scroll)
        await record(net, access, entry, url, variant, status=r.status, final_url=r.final_url, chain=r.chain,
                     headers=None, ttfb=None, total_ms=r.total_ms, body=r.html.encode("utf-8"), error=r.error)

    async def do_probes(client, net, access, entries):
        ctx = ProbeContext(store, run_id, net.name, client, access, sem, limiter)
        print(f"\n--- пробы: {net.name} ---", flush=True)
        specs = plan_specs(access.base_url, entries, probe_kinds, limit, settings.index_skip_types)
        await asyncio.gather(*(ctx.run_spec(sp) for sp in specs))
        await ctx.run_discovery(probe_kinds)
        wanted = [k for k in RESOURCE_KINDS if k in probe_kinds]
        if wanted:
            pages, per_type = [], defaultdict(int)
            for e in entries:  # HTML страниц: из этого запуска, а если не собирали — запрашиваем сами
                if per_type[e.type] >= max(1, args.asset_pages):
                    continue
                per_type[e.type] += 1
                got = html_by_entry.get((net.name, e.type, e.source_url))
                if not got:
                    r = await ctx.fetch(build_url(access.base_url, e))
                    got = (r.final_url, r.body) if r.status == 200 and r.body else None
                if got:
                    pages.append((e.type, e.source_url, got[0], got[1]))
            await probe_resources(ctx, pages, wanted)
        probe_rows.extend(ctx.rows)

    diff_results: list[dict] = []
    try:
        async with contextlib.AsyncExitStack() as stack:
            browser = None
            if any(VARIANTS[v][0] == "rendered" for v in variants):
                from .render import BrowserSession
                browser = await stack.enter_async_context(BrowserSession(headless=not args.show_browser))
            tasks, clients = [], {}
            for net, accesses, entries in plan:
                client = make_client(timeout, settings.verify_tls)
                stack.push_async_callback(client.aclose)
                clients[net.name] = client
                renderers = {}
                for v in variants:
                    mode, role = VARIANTS[v]
                    access = accesses[role]
                    if mode == "rendered" and role not in renderers:
                        renderers[role] = await browser.new_renderer(
                            access, mobile=args.viewport == "mobile",
                            block_assets=not args.no_block_assets, verify_tls=settings.verify_tls)
                    for e in entries:
                        tasks.append(do_raw(client, net, access, e, v) if mode == "raw"
                                     else do_render(renderers[role], net, access, e, v))
            await asyncio.gather(*tasks)
            if probe_kinds:  # пробы после страниц: ресурсам нужен HTML
                await asyncio.gather(*(do_probes(clients[net.name], net, accesses["plain"], entries)
                                       for net, accesses, entries in plan))
    finally:
        summary = summarize(rows)
        summary["probes"] = summarize_probes(probe_rows)
        if pairs and not args.no_diff and rows:
            diff_results = compare_run(store, run_id, pairs)
            summary["diffs"] = summarize_diffs(diff_results)
        store.finish_run(run_id, summary)
        store.close()
    if rows:
        print_summary(summary)
    print_probe_summary(summary["probes"])
    print_diff_summary(summary.get("diffs", []))
    print(f"\nЗапуск: {run_id}\nHTML: {settings.data_dir / 'html' / run_id}\nБД:   {settings.data_dir / 'techaudit.db'}")
    if diff_results:
        print(f"Отчёт сравнения: {write_report(settings.data_dir, run_id, diff_results)}")
    return summary


def summarize(rows: list[dict]) -> dict:
    groups = defaultdict(list)
    for r in rows:
        groups[(r["network"], r["page_type"], r["variant"])].append(r)
    out = []
    for (net, ptype, variant), rs in sorted(groups.items()):
        codes = defaultdict(int)
        for r in rs:
            codes[str(r["status"] or "ERR")] += 1
        ttfb = [r["ttfb_ms"] for r in rs if r["ttfb_ms"] is not None]
        out.append({
            "network": net, "type": ptype, "variant": variant, "total": len(rs),
            "ok": sum(1 for r in rs if r["status"] == 200 and not r["suspect"]),
            "statuses": dict(codes),
            "redirected": sum(1 for r in rs if len(json.loads(r["chain"] or "[]")) > 1),
            "suspects": sorted({s for r in rs for s in (r["suspect"] or "").split(",") if s}),
            "ttfb_p50_ms": round(statistics.median(ttfb)) if ttfb else None,
            "median_kb": round(statistics.median(r["bytes"] for r in rs) / 1024, 1),
            "has_h1": sum(r["has_h1"] for r in rs if r["status"] == 200),
            "ok_200": sum(1 for r in rs if r["status"] == 200),
        })
    return {"groups": out}


def print_summary(summary: dict) -> None:
    print("\n=== СВОДКА ===")
    print(f"{'сеть':<10} {'тип':<12} {'вариант':<13} {'всего':>5} {'ok':>3} {'статусы':<14} {'редир.':>6} "
          f"{'h1 в HTML':>9} {'КБ p50':>7} {'TTFB p50':>9}  подозрения")
    for g in summary["groups"]:
        st = " ".join(f"{k}×{v}" for k, v in g["statuses"].items())
        ttfb = f"{g['ttfb_p50_ms']} мс" if g["ttfb_p50_ms"] is not None else "—"
        h1 = f"{g['has_h1']}/{g['ok_200']}"
        print(f"{g['network']:<10} {g['type']:<12} {g['variant']:<13} {g['total']:>5} {g['ok']:>3} {st:<14} "
              f"{g['redirected']:>6} {h1:>9} {g['median_kb']:>7} {ttfb:>9}  {','.join(g['suspects'])}")


def main(argv=None, env_file=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # консоль Windows может быть не в UTF-8
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.env_file or env_file)
        if not settings.networks:
            raise ConfigError("Не заданы сети: добавьте их в веб-интерфейсе (python web.py → «Доступ → Сети») или в .env (NETWORKS)")
        asyncio.run(run_collection(args, settings))
    except (ConfigError, ValueError) as e:
        print(f"Ошибка настроек: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nПрервано. Уже собранное сохранено в data/.", file=sys.stderr)
        return 130
    return 0
