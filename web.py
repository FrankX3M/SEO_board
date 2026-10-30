"""Веб-интерфейс техаудита: дашборд в динамике, справочник страниц, запуски и расписание.

    python web.py                      # http://127.0.0.1:8765
    python web.py --port 9000 --no-open
    python web.py --env-file D:\\secrets\\.env
    python web.py --reseed             # дозагрузить справочник из справочники/ и registry/ (существующее не меняется)

Слушает только локальный адрес и не имеет входа. Токены берутся из .env так же, как у консольных скриптов
и в интерфейсе не показываются.
"""
import argparse
import contextlib
import sys
import threading
import webbrowser

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--env-file", help="путь к .env (как у collect_html.py)")
    ap.add_argument("--no-open", action="store_true", help="не открывать браузер")
    ap.add_argument("--reseed", action="store_true", help="дозагрузить справочник из файлов проекта и выйти")
    ap.add_argument("--allow-remote", action="store_true",
                    help="разрешить не-локальный адрес. Входа в интерфейсе нет — закройте доступ сетью/прокси сами")
    args = ap.parse_args(argv)

    if args.host not in LOOPBACK and not args.allow_remote:
        print(f"Адрес {args.host} не локальный, а в интерфейсе нет входа. Если так и задумано — добавьте --allow-remote.",
              file=sys.stderr)
        return 2
    try:
        import uvicorn
        from techaudit.web.app import create_app
    except ImportError as e:
        print(f"Не хватает пакета: {e.name}. Установите: pip install -r requirements.txt", file=sys.stderr)
        return 2

    if args.reseed:
        from techaudit import db_registry as reg
        from techaudit.config import ROOT, load_settings
        from techaudit.webdb import connect, init_all
        settings = load_settings(args.env_file)
        db = connect(init_all(settings.data_dir))
        print(reg.seed_from_files(db, ROOT / "справочники", settings.registry_dir))
        return 0

    extra = (args.host,) if args.host not in LOOPBACK else ()
    app = create_app(args.env_file, extra_hosts=extra)
    url = f"http://{'localhost' if args.host in LOOPBACK else args.host}:{args.port}"
    print(f"Техаудит: {url}   (Ctrl+C — остановить; расписание работает, пока процесс запущен)")
    if not args.no_open:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
