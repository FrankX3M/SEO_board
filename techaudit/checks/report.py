"""Итоговый HTML-отчёт по чек-листу: сводка, блоки, статусы, детали по URL. Один файл без внешних зависимостей."""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from . import plan as planmod
from .checklist import worst
from .core import FAIL, NA, PASS, SKIPPED, STATUS_LABELS, WARN

ORDER = (FAIL, WARN, PASS, SKIPPED, NA)
PRIO_W = {"Критично": 0, "Высокий": 1, "Средний": 2, "Низкий": 3}
MAX_ITEMS = 40

CSS = """
:root{--bg:#fff;--fg:#1d2129;--mut:#667085;--card:#f7f8fa;--line:#e3e6eb;--pass:#1a7f4b;--passbg:#e3f4ea;--fail:#b42318;--failbg:#fde8e6;
--warn:#8a5a00;--warnbg:#fff3d6;--skip:#475467;--skipbg:#eceef2;--acc:#2456d6}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#14161a;--fg:#e6e8ec;--mut:#98a2b3;--card:#1c1f25;--line:#2c3038;
--pass:#6fd39b;--passbg:#16301f;--fail:#ff9d94;--failbg:#3a1b18;--warn:#f1c35e;--warnbg:#38300f;--skip:#b4bcc9;--skipbg:#262a31;--acc:#8fb0ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 10px}.sub{color:var(--mut);margin:0 0 20px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:16px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card b{display:block;font-size:26px;line-height:1.1}.card span{color:var(--mut);font-size:13px}
.bar{display:flex;height:10px;border-radius:5px;overflow:hidden;margin:8px 0 4px;background:var(--line)}
.bar i{display:block}.c-pass{background:var(--pass)}.c-fail{background:var(--fail)}.c-warn{background:var(--warn)}.c-skipped{background:#98a2b3}.c-na{background:#cbd2dc}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:20px 0 4px}.tabs button,.flt button{border:1px solid var(--line);background:var(--card);color:var(--fg);
padding:6px 12px;border-radius:999px;cursor:pointer;font:inherit}.tabs button.on,.flt button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.flt{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}
table{width:100%;border-collapse:collapse;margin:6px 0 4px}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12.5px;font-weight:600;white-space:nowrap}
.b-pass{background:var(--passbg);color:var(--pass)}.b-fail{background:var(--failbg);color:var(--fail)}.b-warn{background:var(--warnbg);color:var(--warn)}
.b-skipped,.b-na{background:var(--skipbg);color:var(--skip)}
.prio{font-size:12px;color:var(--mut)}.plan td.n,.plan th.n{text-align:right;white-space:nowrap}.rl{font-size:13px;white-space:nowrap}.crit{color:var(--fail);font-weight:600}
details{margin-top:6px}summary{cursor:pointer;color:var(--acc);font-size:13px}
.items{margin:6px 0 0;padding:0;list-style:none;font-size:13px}.items li{padding:3px 0;border-top:1px dashed var(--line);word-break:break-word}
.items .u{color:var(--mut)}.ok{color:var(--pass)}.no{color:var(--fail)}
.ai{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--acc);border-radius:8px;padding:12px 16px;margin:14px 0}
.ai h3{margin:0 0 6px;font-size:15px}.ai .tag{font-size:12px;color:var(--mut);font-weight:400}.ai ul{margin:6px 0 0;padding-left:20px}
.aic{font-size:13px;color:var(--mut);margin-top:6px;font-style:italic}
.net{display:none}.net.on{display:block}
tr.hide{display:none}.crit-row td:first-child{border-left:3px solid var(--fail)}
@media (max-width:720px){th:nth-child(3),td:nth-child(3),th:nth-child(4),td:nth-child(4){display:none}}
@media print{.tabs,.flt{display:none}.net{display:block!important}details{display:block}}
"""

JS = """
const tabs=[...document.querySelectorAll('.tabs button')],nets=[...document.querySelectorAll('.net')];
function show(n){tabs.forEach(b=>b.classList.toggle('on',b.dataset.n===n));nets.forEach(s=>s.classList.toggle('on',s.dataset.n===n));}
tabs.forEach(b=>b.onclick=()=>show(b.dataset.n));if(tabs[0])show(tabs[0].dataset.n);
document.querySelectorAll('.flt').forEach(f=>{f.querySelectorAll('button').forEach(b=>b.onclick=()=>{
 f.querySelectorAll('button').forEach(x=>x.classList.toggle('on',x===b));
 const s=b.dataset.s,net=f.closest('.net');net.querySelectorAll('tbody tr').forEach(r=>r.classList.toggle('hide',s!=='all'&&r.dataset.s!==s));});});
"""


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _badge(st: str) -> str:
    return f'<span class="badge b-{st}">{STATUS_LABELS[st]}</span>'


def _items_html(items: list[dict], stats: dict | None = None) -> str:
    if not items:
        return ""
    bad = [i for i in items if not i["ok"]]
    show = (bad or items)[:MAX_ITEMS]
    more = len(bad or items) - len(show)
    li = "".join(f'<li><span class="{"ok" if i["ok"] else "no"}">{"✓" if i["ok"] else "✗"}</span> '
                 f'<span class="u">{esc(i.get("kind") or "")}</span> {esc(i["url"])} — {esc(i["msg"])}</li>' for i in show)
    if more > 0:
        li += f'<li class="u">… и ещё {more} (остальные сохранённые примеры — в Excel, лист «Автопроверка - детали»)</li>'
    stats = stats or {}
    label = (f"Всего замечаний: {stats.get('bad', len(bad))}; примеров: {len(bad)}"
             if bad else f"Проверено: {stats.get('total', len(items))}; примеров: {len(items)}")
    return f"<details><summary>{label}</summary><ul class='items'>{li}</ul></details>"


def _ai_comment(notes: dict, net: str, cid: str | None) -> str:
    if not cid:
        return ""
    n = notes.get(net) or {}
    parts = []
    c = ((n.get("analysis") or {}).get("comments") or {}).get(cid)
    if c:
        parts.append(f"<div class='aic'>ИИ: {esc(c)}</div>")
    if cid == "text_descriptions":
        for d in (n.get("descriptions") or {}).get("pages") or []:
            parts.append(f"<div class='aic'>ИИ · {esc(d.get('url', ''))}: описание {'есть' if d.get('has_description') else 'не найдено'}"
                         f"{', скрыто' if d.get('hidden') else ''}. {esc(d.get('comment', ''))}</div>")
    return "".join(parts)


def _plan_html(rows: list[dict], res: dict[str, dict], net: str) -> str:
    """План работ по сети: итоги по ролям и таблица задач с приоритетом, ролью, часами и результатом."""
    tasks = planmod.build_tasks(rows, {net: res})
    if not tasks:
        return ""
    tot = planmod.totals_by_role(tasks)
    cards = "".join(f"<div class='card'><b>{planmod.fmt_hours(a['hours'])} ч</b><span>{esc(a['role_label'])} · задач: {a['tasks']}</span></div>" for a in tot)
    cards += f"<div class='card'><b>{planmod.fmt_hours(round(sum(t['hours'] for t in tasks), 2))} ч</b><span>всего · задач: {len(tasks)}</span></div>"
    body = "".join(
        f"<tr><td>{esc(planmod.WAVE_LABEL[t['wave']])}</td><td class='prio {'crit' if t['priority'] == 'Критично' else ''}'>{esc(t['priority'])}</td>"
        f"<td>{esc(t['title'])}<br><span class='prio'>{esc(t['block'])} · {esc(t['summary'])}</span></td><td class='rl'>{esc(t['role_label'])}</td>"
        f"<td class='n'>{planmod.fmt_hours(t['hours'])}</td><td>{esc(t['result'])}</td></tr>" for t in tasks)
    return ("<h2>План работ</h2><div class='cards'>" + cards + "</div>"
            "<table class='plan'><thead><tr><th>Волна</th><th>Приоритет</th><th>Задача</th><th>Роль</th><th class='n'>Часы</th><th>Результат</th></tr></thead>"
            f"<tbody>{body}</tbody></table><div class='sub'>Часы — оценка на исправление: «Не пройдено» полностью, «Требует внимания» — "
            f"{planmod.warn_share():.0%}. Значения по умолчанию можно заменить в PLAN_FILE (.env).</div>")


def build_html(run_id: str, rows: list[dict], results: dict[str, dict[str, dict]], meta: dict, notes: dict | None = None) -> str:
    rows = planmod.enrich(rows)
    nets = sorted(results)
    notes = notes or {}
    parts = [f"<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>Отчёт техаудита — {esc(run_id)}</title><style>{CSS}</style></head><body><div class='wrap'>",
             f"<h1>Отчёт технического аудита</h1><p class='sub'>Запуск <b>{esc(run_id)}</b> · {datetime.now():%d.%m.%Y %H:%M} · "
             f"проверяется прод · {esc(meta.get('note', ''))}</p>"]
    parts.append("<div class='tabs'>" + "".join(f"<button data-n='{esc(n)}'>{esc(n)}</button>" for n in nets) + "</div>")
    for n in nets:
        res = results[n]
        cnt = {s: sum(1 for x in res.values() if x["status"] == s) for s in ORDER}
        total = sum(cnt.values()) or 1
        done = cnt[PASS] + cnt[FAIL] + cnt[WARN]
        parts.append(f"<section class='net' data-n='{esc(n)}'><h2>{esc(n)}: итог</h2><div class='cards'>")
        for s in ORDER:
            parts.append(f"<div class='card'><b>{cnt[s]}</b><span>{STATUS_LABELS[s]}</span></div>")
        parts.append("</div><div class='bar'>" + "".join(f"<i class='c-{s}' style='width:{cnt[s] / total * 100:.1f}%'></i>" for s in ORDER) +
                     f"</div><div class='sub'>Автоматически проверено {done} из {total} пунктов; "
                     f"«Пропущено» — нужны внешние сервисы, краул, пробы или ручная проверка.</div>")
        a = (notes.get(n) or {}).get("analysis") or {}
        if a:
            parts.append(f"<div class='ai'><h3>Сводка ИИ <span class='tag'>черновик, модель {esc((notes.get(n) or {}).get('model', ''))}; статусы не меняет — проверьте вручную</span></h3>"
                         f"<p>{esc(a.get('summary', ''))}</p>")
            if a.get("root_causes"):
                parts.append("<b>Первопричины</b><ul>" + "".join(
                    f"<li>{esc(x.get('title', ''))} — {esc(x.get('action', ''))} <span class='tag'>({esc(', '.join(x.get('checks') or []))})</span></li>"
                    for x in a["root_causes"]) + "</ul>")
            if a.get("tasks"):
                parts.append("<b>Черновики задач</b><ul>" + "".join(
                    f"<li><b>{esc(x.get('title', ''))}</b> ({esc(x.get('priority', ''))}) — {esc(x.get('description', ''))}</li>"
                    for x in a["tasks"]) + "</ul>")
            parts.append("</div>")
        parts.append(_plan_html(rows, res, n))
        crit = [r for r in rows if r["check_id"] in res and res[r["check_id"]]["status"] == FAIL and r["priority"] == "Критично"]
        if crit:
            parts.append("<h2>Критичные пункты, которые не прошли</h2><ul class='items'>" +
                         "".join(f"<li><b>{esc(r['title'])}</b> — {esc(res[r['check_id']]['summary'])}</li>" for r in crit) + "</ul>")
        parts.append("<div class='flt'><button class='on' data-s='all'>Все</button>" +
                     "".join(f"<button data-s='{s}'>{STATUS_LABELS[s]} ({cnt[s]})</button>" for s in ORDER if cnt[s]) + "</div>")
        parts.append("<table><thead><tr><th>Пункт</th><th>Статус</th><th>Приоритет</th><th>Роль · часы</th><th>Результат</th></tr></thead><tbody>")
        block = None
        for r in rows:
            cid = r["check_id"]
            if r["block"] != block:
                block = r["block"]
                parts.append(f"<tr data-s='all'><td colspan='5'><b>{esc(block)}</b></td></tr>")
            x = res.get(cid) if cid else None
            st = x["status"] if x else SKIPPED
            summary = x["summary"] if x else "ручной пункт — автоматической проверки нет"
            cls = " class='crit-row'" if st == FAIL and r["priority"] == "Критично" else ""
            expected = r.get('criterion') or ('Выполнение критерия: ' + r['title'])
            parts.append(f"<tr data-s='{st}'{cls}><td>{esc(r['title'])}<br><span class='prio'>Ожидаемый результат: {esc(expected)}</span></td>"
                         f"<td>{_badge(st)}</td><td class='prio {'crit' if r['priority'] == 'Критично' else ''}'>{esc(r['priority'] or '')}</td>"
                         f"<td class='rl'>{esc(r['role_label'])}" + (f"<br><span class='prio'>{planmod.fmt_hours(planmod.hours_for(r['hours'], st))} ч</span>" if st in (FAIL, WARN) else "") + "</td>"
                         f"<td>{esc(summary)}{_items_html(x['items'], x.get('stats')) if x else ''}{_ai_comment(notes, n, cid)}</td></tr>")
        parts.append("</tbody></table></section>")
    parts.append(f"</div><script>{JS}</script></body></html>")
    return "".join(parts)


def write_html(path: Path, run_id: str, rows, results, meta: dict | None = None, notes: dict | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_html(run_id, rows, results, meta or {}, notes), encoding="utf-8")
    return path
