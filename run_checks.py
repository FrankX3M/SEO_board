"""Проверки чек-листа по собранному HTML и пробам + Excel и HTML-отчёт.

    python run_checks.py                         # последний запуск, все сети, отчёты в data/reports/
    python run_checks.py --run 20261021-144752-prod --network 5ka
    python run_checks.py --live                  # + выборка ссылок/canonical/sitemap с прода (нужен токен сети из настроек)
    python run_checks.py --list                  # список проверок и пунктов чек-листа без запуска
    python run_checks.py --only canonical_present,viewport
    python run_checks.py --baseline <run_id>     # эталон разметки текущего прода
    python run_checks.py --mobile-run <run_id>   # прогон с мобильным UA для сверки версий

Настройки (токен, UA, пороги) берутся из тех же настроек (веб-интерфейс / .env), что и у collect_html.py.
"""
import sys

from techaudit.checks.cli import main

if __name__ == "__main__":
    sys.exit(main())
