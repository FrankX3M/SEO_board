"""CLI: python run_checks.py"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
from pathlib import Path

from ..comparer import latest_run
from ..config import ROOT, ConfigError, load_settings
from ..storage import Store
from . import checklist, plan as planmod, report
from .extras import fallback_rows, with_extras
from .core import FAIL, PASS, REGISTRY, STATUS_LABELS, WARN
from .ctx import Ctx, ensure_schema
from .runner import load_all, load_results, networks_in_run, run_checks


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", help="id запуска (по умолчанию последний)")
    ap.add_argument("--network", help="сети через запятую (по умолчанию все сети запуска)")
    ap.add_argument("--env-file", help="путь к .env")
    ap.add_argument("--checklist", help="путь к чек-листу xlsx (иначе CHECKLIST_XLSX из .env или поиск рядом с проектом)")
    ap.add_argument("--out-dir", help="куда писать отчёты (по умолчанию data/reports)")
    ap.add_argument("--only", help="id проверок через запятую")
    ap.add_argument("--list", action="store_true", help="показать проверки и сопоставление с чек-листом")
    ap.add_argument("--live", action="store_true", help="сделать выборку запросов к проду (ссылки, canonical, sitemap)")
    ap.add_argument("--no-token", action="store_true", help="диагностика: живые запросы без токена")
    ap.add_argument("--ua", help="профиль UA для живых запросов (по умолчанию UA_PROFILE)")
    ap.add_argument("--ai", action="store_true", help="этап ИИ: сводка, первопричины, черновики комментариев и задач (нужен AI_API_KEY)")
    ap.add_argument("--ai-model", help="модель ИИ (иначе AI_MODEL из .env, по умолчанию x5-airun-medium)")
    ap.add_argument("--external", action="store_true", help="внешние сервисы: PageSpeed и Search Console (тратят квоту; ключи списком в .env)")
    ap.add_argument("--debug", action="store_true", help="сохранить условия проверки (пороги, режимы, флаги) для просмотра в интерфейсе")
    ap.add_argument("--ai-only", action="store_true", help="не пересчитывать проверки, только этап ИИ по сохранённым результатам запуска")
    ap.add_argument("--baseline", help="id запуска-эталона для сверки состава разметки")
    ap.add_argument("--mobile-run", help="id запуска с мобильным UA для сверки версий")
    ap.add_argument("--no-xlsx", action="store_true")
    ap.add_argument("--no-html", action="store_true")
    return ap


def print_summary(net: str, res: dict) -> None:
    from collections import Counter
    c = Counter(r.status for r in res.values())
    print(f"\n=== {net}: " + ", ".join(f"{STATUS_LABELS[s]} {c[s]}" for s in (PASS, WARN, FAIL, "skipped", "na") if c[s]))
    for cid, r in res.items():
        if r.status in (FAIL, WARN):
            print(f"  {'FAIL' if r.status == FAIL else 'WARN'}  {cid:<24} {r.summary}")


def save_debug(store, run_id: str, args) -> None:
    """Условия, в которых считались проверки: пороги, флаги окружения, ключи (только число, без значений)."""
    import json
    from dataclasses import asdict
    from ..keys import describe
    from .core import CheckSettings
    from ..webdb import DEBUG_SCHEMA
    store.db.executescript(DEBUG_SCHEMA)
    cs = asdict(CheckSettings.from_env())
    info = {
        "thresholds": {k: (list(v) if isinstance(v, tuple) else v) for k, v in cs.items()},
        "flags": {"live": bool(args.live), "no_token_live": bool(args.no_token), "ua_live": args.ua or "UA_PROFILE",
                  "external": os.environ.get("EXTERNAL_CHECKS") == "1", "ai": bool(args.ai), "only": args.only or ""},
        "keys": describe(),
        "env": {k: os.environ.get(k, "") for k in ("USE_TOKEN", "USE_UA", "UA_PROFILE", "PLAIN_UA_PROFILE", "PROBE_LIMIT",
                                                    "GSC_MAX_URLS", "PSI_MAX_URLS", "CWV_LCP_MS", "CWV_CLS", "CWV_TBT_MS")},
    }
    store.db.execute("DELETE FROM run_debug WHERE run_id=?", (run_id,))
    for k, v in info.items():
        store.db.execute("INSERT INTO run_debug(run_id,key,value) VALUES (?,?,?)", (run_id, k, json.dumps(v, ensure_ascii=False)))
    store.db.commit()


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    if args.external:
        os.environ["EXTERNAL_CHECKS"] = "1"
    if args.ai_only:
        args.ai = True
    load_all()
    try:
        settings = load_settings(args.env_file)
    except ConfigError as e:
        print(f"Ошибка настроек: {e}", file=sys.stderr)
        return 2

    xlsx = checklist.find_checklist(ROOT, args.checklist)
    rows = planmod.enrich(with_extras(checklist.read_rows(xlsx)) if xlsx else fallback_rows())
    if args.list:
        print(f"Чек-лист: {xlsx or 'не найден'}")
        for r in rows:
            print(f"  {r['check_id'] or '— ручной —':<24} {r['block']} / {r['title']}")
        used = {r["check_id"] for r in rows}
        extra = [c for c in REGISTRY if c not in used]
        if extra:
            print("Проверки без пункта в чек-листе:", ", ".join(extra))
        return 0

    store = Store(settings.data_dir)
    ensure_schema(store)
    run_id = args.run or latest_run(store)
    if not run_id:
        print("В БД нет запусков. Сначала: python collect_html.py", file=sys.stderr)
        return 1
    nets = [n.strip() for n in args.network.split(",")] if args.network else networks_in_run(store, run_id)
    only = {x.strip() for x in args.only.split(",")} if args.only else None
    print(f"Запуск {run_id}; сети: {', '.join(nets)}; чек-лист: {xlsx.name if xlsx else 'не найден (отчёт без Excel)'}")

    from .core import CheckSettings
    from .live import collect_live
    for n in nets:
        if args.ai_only:
            continue
        if args.live:
            cfg = settings.networks.get(n)
            if not cfg:
                print(f"  сеть {n} не заведена в настройках — живая выборка пропущена")
            else:
                ctx = Ctx(store, settings, run_id, n, CheckSettings.from_env(), cfg.base_url())
                try:
                    asyncio.run(collect_live(ctx, cfg, token=False if args.no_token else None, ua=args.ua))
                except ConfigError as e:
                    print(f"  {n}: {e}", file=sys.stderr)
        res = run_checks(store, settings, run_id, n, only=only, baseline=args.baseline, mobile_run=args.mobile_run)
        print_summary(n, res)
    if args.debug:
        save_debug(store, run_id, args)

    results = load_results(store, run_id)
    notes: dict = {}
    if args.ai:
        from .ai_stage import load_notes, run_ai
        if args.ai_model:
            os.environ["AI_MODEL"] = args.ai_model
        rows_ai = rows
        print("\n=== ИИ-этап (вывод помечается как черновик, статусы не меняются) ===")
        ai_failed = False
        for n in nets:
            if n not in results:
                continue
            cfg = settings.networks.get(n)
            ctx = Ctx(store, settings, run_id, n, CheckSettings.from_env(), cfg.base_url() if cfg else "")
            st = run_ai(store, settings, ctx, results[n], rows_ai, run_id, n)
            if st["status"] != "ok":
                print(f"  {n}: ИИ {st['status']}: {st['reason']}")
                ai_failed = True
        notes = load_notes(store, run_id)
    else:
        from .ai_stage import load_notes
        notes = load_notes(store, run_id)  # ранее сохранённые выводы ИИ для этого запуска
    out = Path(args.out_dir) if args.out_dir else settings.data_dir / "reports"
    if not args.no_xlsx and xlsx:
        dst = out / f"{run_id}-checklist.xlsx"
        info = checklist.write_xlsx(xlsx, dst, results, run_id, notes)
        print(f"\nExcel: {dst}")
        if info["unmatched"]:
            print(f"  пунктов без автопроверки: {len(info['unmatched'])}")
    if not args.no_html:
        p = report.write_html(out / f"{run_id}-checks.html", run_id, rows, results,
                              {"note": "выборка страниц по справочнику, не полный краул"}, notes)
        print(f"HTML:  {p}")
    store.close()
    if args.ai_only and locals().get("ai_failed"):
        return 3  # задание «только ИИ» не должно выглядеть успешным, если ответа от модели нет
    return 0


if __name__ == "__main__":
    sys.exit(main())
