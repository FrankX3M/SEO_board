"""Подключает веб-интерфейс к проекту (один раз, безопасно повторять):
  1) collect_html: список страниц сети берётся из БД (если для сети там пусто — из registry/<сеть>.csv, как раньше);
  2) requirements.txt: пакеты веб-интерфейса;
  3) README.md: раздел «Веб-интерфейс».
Запуск из папки проекта:  python apply_web_patch.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
changed = []

p = ROOT / "techaudit" / "collect.py"
s = p.read_text(encoding="utf-8")
orig = s
imp = "from .registry import RegistryEntry, build_url, load_registry, path_qs\n"
if "from .db_registry import entries_for" not in s:
    if imp not in s:
        raise SystemExit("collect.py: не найдена строка импорта registry — патч не применён, ничего не изменено")
    s = s.replace(imp, imp + "from .db_registry import entries_for\n", 1)
old = 'entries = load_registry(settings.registry_dir / f"{name}.csv", name)'
if old in s:
    s = s.replace(old, "entries = entries_for(settings, name)  # справочник из БД (веб-интерфейс), иначе registry/<сеть>.csv", 1)
elif "entries_for(settings, name)" not in s:
    raise SystemExit("collect.py: не найдено место чтения справочника — патч не применён")
if s != orig:
    (ROOT / "techaudit" / "collect.py.bak").write_text(orig, encoding="utf-8")
    p.write_text(s, encoding="utf-8")
    changed.append("techaudit/collect.py (копия: collect.py.bak)")

p = ROOT / "requirements.txt"
req = p.read_text(encoding="utf-8")
add = [x for x in ("fastapi>=0.110", "uvicorn>=0.29", "jinja2>=3.1", "python-multipart>=0.0.9")
       if x.split(">")[0] not in req]
if add:
    p.write_text(req.rstrip("\n") + "\n" + "\n".join(add) + "\n", encoding="utf-8")
    changed.append("requirements.txt")

p = ROOT / "README.md"
rd = p.read_text(encoding="utf-8")
if "## Веб-интерфейс" not in rd:
    p.write_text(rd.rstrip("\n") + "\n\n" + (ROOT / "README_web.md").read_text(encoding="utf-8"), encoding="utf-8")
    changed.append("README.md")

print("Изменено:", ", ".join(changed) if changed else "ничего (уже применено)")
print("Дальше:  pip install -r requirements.txt   затем   python web.py")
