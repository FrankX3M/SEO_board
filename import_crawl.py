"""Загрузка краула Screaming Frog (CSV «Internal → All») в БД — для больших файлов, когда неудобно через браузер.

    python import_crawl.py --network 5ka D:\\crawls\\5ka_internal_all.csv
    python import_crawl.py --network perekrestok --label "до релиза" crawl.csv

Чужие хосты в файле отбрасываются по NET_<СЕТЬ>_URL. Выгрузку можно загрузить и в веб-интерфейсе (раздел «Краул»).
"""
import argparse
import contextlib
import sys
from urllib.parse import urlsplit


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", help="CSV из Screaming Frog")
    ap.add_argument("--network", required=True, help="ключ сети из NETWORKS")
    ap.add_argument("--label", default="", help="подпись выгрузки")
    ap.add_argument("--env-file")
    args = ap.parse_args(argv)

    from techaudit import crawl
    from techaudit.config import ConfigError, load_settings
    from techaudit.webdb import connect, init_all

    try:
        settings = load_settings(args.env_file)
    except ConfigError as e:
        print(f"Ошибка настроек: {e}", file=sys.stderr)
        return 2
    cfg = settings.networks.get(args.network)
    host = (urlsplit(cfg.url).hostname if cfg and cfg.url else None)
    db = connect(init_all(settings.data_dir))
    try:
        res = crawl.import_csv(db, args.network, args.file, label=args.label, host=host)
    except (crawl.CrawlError, OSError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    print(f"Загружено: {res['rows']} URL ({res['html_rows']} HTML), краул от {res['taken_at']}, выгрузка №{res['import_id']}"
          + (f", чужих адресов пропущено: {res['skipped_external']}" if res["skipped_external"] else ""))
    if res["missing_columns"]:
        print("Нет колонок (часть метрик будет пустой): " + ", ".join(res["missing_columns"][:12]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
