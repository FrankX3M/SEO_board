"""Один HTML-файл со сравнением вариантов: сводка + детали по каждому URL."""
from __future__ import annotations

from collections import Counter
from html import escape as e
from pathlib import Path

from .comparer import LABELS, summarize_diffs

CSS = """
:root{--bg:#fff;--fg:#1d1d1f;--mut:#6b6b73;--line:#e3e3e8;--ok:#1a7f37;--warn:#9a6700;--bad:#cf222e;--chip:#f3f3f6}
@media(prefers-color-scheme:dark){:root{--bg:#161618;--fg:#ececf0;--mut:#9a9aa4;--line:#2d2d33;--ok:#3fb950;--warn:#d29922;--bad:#f85149;--chip:#232328}}
body{font:14px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0 auto;max-width:1100px;padding:24px 16px}
h1{font-size:22px}h2{font-size:17px;margin-top:32px}table{border-collapse:collapse;width:100%}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}th{color:var(--mut);font-weight:500}
code{background:var(--chip);padding:1px 5px;border-radius:4px;font-size:12px}details{border:1px solid var(--line);border-radius:8px;margin:8px 0;padding:6px 12px}
summary{cursor:pointer}.same{color:var(--ok)}.partial{color:var(--warn)}.major,.empty_b{color:var(--bad)}.skipped{color:var(--mut)}
.mut{color:var(--mut)}.num{text-align:right;font-variant-numeric:tabular-nums}
.tl{display:inline-block;min-width:92px;font-weight:600}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
ul.brief{list-style:none;padding:0;margin:8px 0}ul.brief li{border-bottom:1px solid var(--line);padding:8px 0}
"""
VERDICT = {"same": "без отличий", "partial": "небольшие отличия", "major": "много нового", "empty_b": "B пустой",
           "skipped": "пропущено"}


def _blocks(title: str, items: list[dict]) -> str:
    if not items:
        return ""
    rows = "".join(
        f"<tr><td><code>{e(b['key'])}</code></td><td class=num>{b.get('instances', '')}</td>"
        f"<td class=num>{b.get('chars', '')}</td><td class=num>{b.get('links', '')}</td>"
        f"<td class=mut>{e(' · '.join(b.get('sample', [])[:2]))}</td></tr>" for b in items)
    return (f"<h3>{e(title)} ({len(items)})</h3><table><tr><th>блок</th><th class=num>экз.</th>"
            f"<th class=num>симв.</th><th class=num>ссылок</th><th>фрагмент текста</th></tr>{rows}</table>")


def _facts(rs: list[dict]) -> list[str]:
    """Короткие факты по группе URL: что изменилось в title/h1/description, микроразметке и ключевых блоках."""
    facts: list[str] = []
    heads = Counter(k for r in rs for k in r["head"])
    names = {"title": "title", "h1": "h1", "description": "description", "canonical": "canonical"}
    changed = [names[k] for k in names if heads.get(k)]
    if changed:
        facts.append("отличаются " + ", ".join(changed))
    gained = Counter()
    for r in rs:
        md = r["structured"].get("microdata")
        if md:
            for t, n in md["b"].items():
                if n > md["a"].get(t, 0):
                    gained[t] += 1
        jl = r["structured"].get("jsonld")
        if jl:
            for t, n in jl["b"].items():
                if n > jl["a"].get(t, 0):
                    gained["JSON-LD " + t] += 1
    if gained:
        facts.append("добавляется разметка: " + ", ".join(t for t, _ in gained.most_common(5)))
    top = Counter()
    for r in rs:
        for b in r["blocks_appeared"][:5]:
            top[b["key"].replace("qa:", "")] += b.get("instances", 1)
    if top:
        facts.append("главные новые блоки: " + ", ".join(k for k, _ in top.most_common(4)))
    return facts


def _judge(a: str, b: str, rs: list[dict], cov: float | None, bot_complete: bool | None = None) -> tuple[str, str]:
    """(светофор ok/warn/bad/mut, вывод одной фразой)."""
    if not rs:
        return "mut", "нет данных: страницы не собраны или заблокированы"
    n_same = sum(r["verdict"] == "same" for r in rs)
    n_major = sum(r["verdict"] == "major" for r in rs)
    if a == "plain" and b == "bot":
        if n_same == len(rs):
            if bot_complete:  # HTML уже полный у всех: отдельный рендеринг для бота не нужен
                return "ok", "сервер сразу отдаёт полный HTML всем, в том числе обычному клиенту; отдельный рендеринг для бота не нужен"
            if bot_complete is False:
                return "bad", ("бот получает тот же HTML, что и обычный клиент, и основной контент в нём отсутствует: "
                               "он появляется только после JS (рендеринга для бота нет или UA не распознан)")
            return "warn", "HTML бота совпадает с HTML обычного клиента; полнота контента не проверена (нет варианта browser)"
        if n_major:
            return "ok", "рендеринг для бота работает: в HTML бота появляется основной контент страницы"
        return "warn", "HTML бота отличается от обычного лишь частично"
    if b in ("browser", "browser-plain"):
        c = cov if cov is not None else 0
        if n_same == len(rs):
            return "ok", "JS ничего не добавляет: HTML уже полный"
        if c >= 0.9:
            return "ok", f"HTML полный, JS добавляет в основном интерфейс (текст из браузера уже есть в HTML на {c:.0%})"
        if c >= 0.6:
            return "warn", f"JS добавляет заметную часть контента (в HTML уже {c:.0%} текста)"
        return "bad", (f"значимый контент появляется только после JS (в HTML лишь {c:.0%} текста): "
                       "боту без выполнения JS он недоступен")
    if n_same == len(rs):
        return "ok", "без отличий"
    return ("bad" if n_major else "warn"), "есть отличия, см. детали ниже"


def quick_summary(results: list[dict]) -> str:
    """Краткий вывод в начале отчёта: по строке на сеть / тип страницы / пару вариантов."""
    groups: dict[tuple, list[dict]] = {}
    for r in results:
        groups.setdefault((r["network"], r["page_type"], r["a"], r["b"]), []).append(r)
    items, counts = [], Counter()
    complete: dict[tuple, bool] = {}  # (сеть, тип) -> HTML бота уже содержит основной контент (по bot → browser)
    for (net, ptype, a, b), rs in groups.items():
        if a == "bot" and b in ("browser", "browser-plain"):
            cv = [r["summary"]["coverage"] for r in rs if r["verdict"] != "skipped" and r["summary"].get("coverage") is not None]
            if cv:
                complete[(net, ptype)] = sum(cv) / len(cv) >= 0.9
    for (net, ptype, a, b), rs in sorted(groups.items()):
        ok = [r for r in rs if r["verdict"] != "skipped"]
        cov_l = [r["summary"]["coverage"] for r in ok if r["summary"].get("coverage") is not None]
        cov = sum(cov_l) / len(cov_l) if cov_l else None
        level, text = _judge(a, b, ok, cov, complete.get((net, ptype)))
        counts[level] += 1
        label = {"ok": "норма", "warn": "проверить", "bad": "проблема", "mut": "нет данных"}[level]
        facts = _facts(ok)
        tail = f"<br><span class=mut>{e('; '.join(facts))}</span>" if facts and level != "mut" else ""
        items.append(f"<li><span class='tl {level}'>{label}</span> <b>{e(net)} / {e(ptype)}</b> · "
                     f"{e(a)} → {e(b)} · URL: {len(rs)}<br>{e(text)}{tail}</li>")
    head = (f"<p>Групп проверено: {sum(counts.values())}. Проблем: <b class=bad>{counts['bad']}</b>, "
            f"проверить: <b class=warn>{counts['warn']}</b>, норма: <b class=ok>{counts['ok']}</b>"
            + (f", нет данных: {counts['mut']}" if counts["mut"] else "") + ".</p>")
    return "<h2>Краткий вывод</h2>" + head + "<ul class=brief>" + "".join(items) + "</ul>"


def render(run_id: str, results: list[dict]) -> str:
    out = [f"<!doctype html><html lang=ru><meta charset=utf-8><title>Сравнение вариантов — {e(run_id)}</title>"
           f"<style>{CSS}</style><h1>Что появляется после рендеринга</h1><p class=mut>Запуск {e(run_id)}. "
           "A — страница до, B — после. «Появились» = блоки, которых нет в A.</p>"]
    used = sorted({v for r in results for v in (r["a"], r["b"])})
    out.append("<p class=mut>" + "<br>".join(f"<b>{e(v)}</b> — {e(LABELS.get(v, v))}" for v in used) + "</p>")

    out.append(quick_summary(results))
    out.append("<h2>Сводка</h2><table><tr><th>сеть / тип</th><th>A → B</th><th class=num>URL</th><th class=num>без отличий</th>"
               "<th class=num>с новыми блоками</th><th class=num>с дополненными</th><th class=num>текст B есть в A</th><th>чаще всего появляется</th></tr>")
    for s in summarize_diffs(results):
        cov = f"{s['coverage_avg']:.0%}" if s["coverage_avg"] is not None else "—"
        top = ", ".join(f"<code>{e(k)}</code> ×{n}" for k, n in s["top_new_blocks"])
        out.append(f"<tr><td>{e(s['network'])} / {e(s['type'])}</td><td>{e(s['a'])} → {e(s['b'])}</td>"
                   f"<td class=num>{s['urls']}</td><td class=num>{s['same']}</td><td class=num>{s['with_new_blocks']}</td><td class=num>{s['with_changed_blocks']}</td>"
                   f"<td class=num>{cov}</td><td>{top}</td></tr>")
    out.append("</table><h2>По страницам</h2>")

    for r in sorted(results, key=lambda r: (r["network"], r["page_type"], r["url"], r["a"], r["b"])):
        s = r["summary"]
        head = (f"<span class={r['verdict']}>{VERDICT.get(r['verdict'], r['verdict'])}</span> · "
                f"{e(r['network'])}/{e(r['page_type'])} · {e(r['a'])} → {e(r['b'])} · {e(r['url'])}")
        if r["verdict"] == "skipped":
            out.append(f"<details><summary>{head}</summary><p class=mut>{e(r.get('reason', ''))}</p></details>")
            continue
        cov = f"{s['coverage']:.0%}" if s.get("coverage") is not None else "—"
        body = [f"<p class=mut>блоков появилось {s['blocks_appeared']}, исчезло {s['blocks_disappeared']}, "
                f"изменилось {s['blocks_changed']}; ссылок +{s['links_appeared']}/−{s['links_disappeared']}; "
                f"текста B, которого нет в A: {s['text_only_b_chars']} симв. (покрытие {cov})</p>",
                _blocks("Появились", r["blocks_appeared"]), _blocks("Исчезли", r["blocks_disappeared"])]
        if r["blocks_changed"]:
            body.append("<h3>Изменились</h3><table><tr><th>блок</th><th>что</th><th>A</th><th>B</th></tr>" + "".join(
                f"<tr><td><code>{e(c['key'])}</code></td><td>{e(c['kind'])}</td>"
                f"<td>{c['a']['instances']} экз., {c['a']['chars']} симв.</td>"
                f"<td>{c['b']['instances']} экз., {c['b']['chars']} симв.</td></tr>" for c in r["blocks_changed"])
                + "</table>")
        if r["head"]:
            body.append("<h3>Мета и заголовки</h3><table><tr><th>поле</th><th>A</th><th>B</th></tr>" + "".join(
                f"<tr><td>{e(k)}</td><td>{e(str(v['a']))}</td><td>{e(str(v['b']))}</td></tr>" for k, v in r["head"].items())
                + "</table>")
        if r["structured"]:
            body.append("<h3>Микроразметка</h3><table><tr><th>формат</th><th>A</th><th>B</th></tr>" + "".join(
                f"<tr><td>{e(k)}</td><td>{e(str(v['a']))}</td><td>{e(str(v['b']))}</td></tr>" for k, v in r["structured"].items())
                + "</table>")
        if r["links_appeared_sample"]:
            body.append("<h3>Новые ссылки (примеры)</h3><p>" + "<br>".join(e(u) for u in r["links_appeared_sample"][:15]) + "</p>")
        out.append(f"<details><summary>{head}</summary>{''.join(body)}</details>")
    return "".join(out)


def write_report(data_dir: Path, run_id: str, results: list[dict]) -> Path:
    path = data_dir / "reports" / f"{run_id}-diff.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(run_id, results), encoding="utf-8")
    return path
