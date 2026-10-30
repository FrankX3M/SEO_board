"""Пересчитать сравнение вариантов по уже собранному запуску (сайт не запрашивается).

    python compare_html.py                         # последний запуск, пары plain:bot,bot:browser
    python compare_html.py --run 20261015-101500-preprod --pairs plain:browser
"""
import argparse
import sys

from techaudit.comparer import compare_run, latest_run, parse_pairs, print_diff_summary, summarize_diffs
from techaudit.config import load_settings
from techaudit.diffreport import write_report
from techaudit.storage import Store


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", help="id запуска (по умолчанию последний)")
    ap.add_argument("--env-file", help="путь к .env (по умолчанию ищется выше проекта, затем в проекте)")
    ap.add_argument("--pairs", default="plain:bot,bot:browser", help="пары A:B через запятую")
    args = ap.parse_args()
    settings = load_settings(args.env_file)
    store = Store(settings.data_dir)
    run_id = args.run or latest_run(store)
    if not run_id:
        print("В БД нет запусков. Сначала: python collect_html.py", file=sys.stderr)
        return 1
    results = compare_run(store, run_id, parse_pairs(args.pairs))
    print_diff_summary(summarize_diffs(results))
    if results:
        print(f"\nОтчёт: {write_report(settings.data_dir, run_id, results)}")
    else:
        print("Нет страниц, у которых собраны оба варианта пары.")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
