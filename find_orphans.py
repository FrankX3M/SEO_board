"""Страницы-сироты: индекс Яндекс Вебмастера / Google Search Console против краула Screaming Frog.

    python find_orphans.py fetch-ywm --network 5ka               # страницы в поиске Яндекса через API (токен — «Доступ → Внешние сервисы»)
    python find_orphans.py fetch-gsc --network 5ka --days 90     # страницы с показами в Google через API
    python find_orphans.py import --network 5ka --source ywm выгрузка.xlsx   # или файл из интерфейса Вебмастера / GSC (CSV, XLSX, TXT)
    python find_orphans.py compare --network 5ka --probe 300     # сравнить с последним краулом, проверить живые ответы, сохранить Excel

Все настройки (токены, сервисные аккаунты, адреса сетей, порог глубины MAX_DEPTH) берутся из «Доступ и настройки»; .env — запасной вариант.
Результат сравнения сохраняется в БД (раздел «Сироты» в интерфейсе) и в data/reports/orphans-<сеть>-<дата>.xlsx.
"""
import argparse
import contextlib
import sys
from datetime import datetime


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-file")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("fetch-ywm", "fetch-gsc", "import", "compare"):
        p = sub.add_parser(name)
        p.add_argument("--network", required=True, help="ключ сети")
        if name == "fetch-ywm":
            p.add_argument("--host-id", default="", help="host_id в Вебмастере (по умолчанию ищется по адресу сети), напр. https:5ka.ru:443")
        if name == "fetch-gsc":
            p.add_argument("--days", type=int, default=0, help="за сколько дней брать страницы с показами (по умолчанию GSC_PAGES_DAYS или 90)")
        if name == "import":
            p.add_argument("--source", required=True, choices=["ywm", "gsc"])
            p.add_argument("--label", default="")
            p.add_argument("file")
        if name == "compare":
            p.add_argument("--crawl-id", type=int, help="номер краула (по умолчанию последний)")
            p.add_argument("--probe", type=int, default=-1, help="сколько адресов «нет в краулe» проверить на сайте (по умолчанию ORPHAN_PROBE_LIMIT или 300; 0 — не проверять)")
            p.add_argument("--no-ywm", action="store_true", help="не использовать Вебмастер")
            p.add_argument("--no-gsc", action="store_true", help="не использовать Google")
            p.add_argument("--no-xlsx", action="store_true")
    args = ap.parse_args(argv)

    import os
    from urllib.parse import urlsplit

    from techaudit import indexed as ix
    from techaudit import orphans as orph
    from techaudit.config import ConfigError, load_settings
    from techaudit.webdb import connect, init_all

    try:
        settings = load_settings(args.env_file)
    except ConfigError as e:
        print(f"Ошибка настроек: {e}", file=sys.stderr)
        return 2
    cfg = settings.networks.get(args.network)
    if not cfg:
        print(f"Сеть «{args.network}» не найдена. Доступны: {', '.join(settings.networks)}", file=sys.stderr)
        return 2
    base = (cfg.url or "").rstrip("/")
    host = urlsplit(base).hostname if base else None
    db = connect(init_all(settings.data_dir))

    def progress(done, total):
        print(f"\r  {done} / {total}", end="", flush=True)

    try:
        if args.cmd == "fetch-ywm":
            token = settings.integrations.get("yandex_webmaster") or os.environ.get("YANDEX_WEBMASTER_TOKEN") or ""
            res = ix.fetch_ywm(db, args.network, base, token, host_id=args.host_id, progress=progress)
            print(f"\nЯндекс Вебмастер ({res['host_id']}): получено {res['rows']} страниц, выгрузка №{res['import_id']}")
            if res.get("truncated"):
                print(f"ВНИМАНИЕ: в Вебмастере {res['total_reported']} страниц, API отдаёт не больше {ix.Ywm.MAX_URLS}. "
                      "Остальное добавьте файлом из интерфейса Вебмастера (команда import).")
        elif args.cmd == "fetch-gsc":
            days = args.days or int(os.environ.get("GSC_PAGES_DAYS") or 90)
            res = ix.fetch_gsc(db, args.network, base, days=days, progress=progress)
            print(f"\nGoogle Search Console ({res['property']}): страниц с показами за {days} дн.: {res['rows']}, выгрузка №{res['import_id']}")
        elif args.cmd == "import":
            res = ix.import_file(db, args.network, args.source, args.file, host=host, label=args.label)
            print(f"{ix.SOURCES[args.source]}: загружено {res['rows']} страниц"
                  + (f", чужих адресов пропущено: {res['skipped_external']}" if res["skipped_external"] else "") + f", выгрузка №{res['import_id']}")
        else:
            probe = args.probe if args.probe >= 0 else int(os.environ.get("ORPHAN_PROBE_LIMIT") or 300)
            res = orph.run_compare(db, settings, args.network, crawl_id=args.crawl_id, ywm_id=0 if args.no_ywm else None,
                                   gsc_id=0 if args.no_gsc else None, probe=probe, progress=progress)
            s = res["summary"]
            print(f"\nВ индексе {s['index_total']} URL, краулом найдено {s['in_crawl']} ({s['coverage']}%). Порог глубины: {s['max_depth']}.")
            for k, (name, _) in orph.CATEGORIES.items():
                print(f"  {name:<24} {s['by_category'][k]}")
            print(f"Сирот (нет в краулe + нет ссылок + глубоко): {s['orphans']} ({s['orphans_share']}%)")
            for w in s["warnings"]:
                print("! " + w)
            print(f"Результат сохранён: раздел «Сироты» → сравнение №{res['run_id']}")
            if not args.no_xlsx:
                from openpyxl import Workbook
                out = settings.data_dir / "reports"
                out.mkdir(parents=True, exist_ok=True)
                path = out / f"orphans-{args.network}-{datetime.now():%Y%m%d-%H%M}.xlsx"
                wb = Workbook(write_only=True)
                ws = wb.create_sheet("Сироты")
                ws.append(orph.EXPORT_HEAD)
                for r in sorted(res["rows"], key=lambda r: (r["category"] not in orph.PROBLEM_CATS, r["category"], -(r["impressions"] or 0), r["url"])):
                    if r["category"] != "ok":
                        ws.append(orph.export_row(r))
                wb.save(path)
                print(f"Excel: {path}")
    except (ix.IndexedError, ConfigError, OSError) as e:
        print(f"\nОшибка: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
