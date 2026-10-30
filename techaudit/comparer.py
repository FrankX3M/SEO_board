"""Сравнение вариантов страниц уже собранного запуска (без повторных запросов к сайту)."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from .diff import compare, parse_doc
from .storage import Store, now_iso

LABELS = {
    "plain": "без рендеринга (обычный UA)",
    "bot": "с рендерингом для бота (UA поисковика, сырой HTML)",
    "browser": "рендер в браузере (UA поисковика, после JS)",
    "browser-plain": "рендер в браузере (обычный UA, после JS)",
}


def parse_pairs(spec: str) -> list[tuple[str, str]]:
    out = []
    for p in spec.split(","):
        a, _, b = p.strip().partition(":")
        if not a or not b:
            raise ValueError(f"Пара должна быть вида plain:bot, получено '{p}'")
        out.append((a, b))
    return out


def default_pairs(variants: list[str]) -> list[tuple[str, str]]:
    return list(zip(variants, variants[1:]))


def latest_run(store: Store) -> str | None:
    r = store.db.execute("SELECT run_id FROM runs ORDER BY started_at DESC, rowid DESC LIMIT 1").fetchone()
    return r[0] if r else None


def compare_run(store: Store, run_id: str, pairs: list[tuple[str, str]]) -> list[dict]:
    store.db.row_factory = None
    rows = store.db.execute(
        "SELECT network, page_type, url, variant, status, final_url, file_path, headers, error FROM pages WHERE run_id=?",
        (run_id,)).fetchall()
    by_url: dict[tuple, dict] = defaultdict(dict)
    for net, ptype, url, variant, status, final_url, fp, headers, err in rows:
        by_url[(net, ptype, url)][variant] = dict(status=status, final_url=final_url or url, file_path=fp,
                                                  headers=json.loads(headers) if headers else {}, error=err)
    cache: dict[str, object] = {}

    def doc(rec):
        fp = rec["file_path"]
        if fp not in cache:
            body = (store.data_dir / fp).read_bytes()
            cache[fp] = parse_doc(body, rec["final_url"], rec["headers"].get("content-type"))
        return cache[fp]

    for a, b in pairs:  # пересчёт: старые результаты этой пары заменяем
        store.db.execute("DELETE FROM diffs WHERE run_id=? AND variant_a=? AND variant_b=?", (run_id, a, b))
    results = []
    for (net, ptype, url), variants in sorted(by_url.items()):
        for a, b in pairs:
            ra, rb = variants.get(a), variants.get(b)
            if not ra or not rb:
                continue
            skip = None
            for name, r in ((a, ra), (b, rb)):
                if r["error"] or not r["file_path"]:
                    skip = f"{name}: ошибка запроса"
                elif r["status"] != 200:
                    skip = f"{name}: HTTP {r['status']}"
            if skip:
                res = {"verdict": "skipped", "reason": skip, "summary": {}}
            else:
                res = compare(doc(ra), doc(rb))
            s = res["summary"]
            store.db.execute(
                "INSERT INTO diffs(run_id, network, page_type, url, variant_a, variant_b, verdict, blocks_appeared,"
                " blocks_disappeared, blocks_changed, links_appeared, links_disappeared, text_only_b_chars,"
                " text_only_a_chars, coverage, details, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, net, ptype, url, a, b, res["verdict"], s.get("blocks_appeared"), s.get("blocks_disappeared"),
                 s.get("blocks_changed"), s.get("links_appeared"), s.get("links_disappeared"),
                 s.get("text_only_b_chars"), s.get("text_only_a_chars"), s.get("coverage"),
                 json.dumps(res, ensure_ascii=False), now_iso()))
            results.append({"network": net, "page_type": ptype, "url": url, "a": a, "b": b, **res})
    store.db.commit()
    return results


def summarize_diffs(results: list[dict]) -> list[dict]:
    g = defaultdict(list)
    for r in results:
        g[(r["network"], r["page_type"], r["a"], r["b"])].append(r)
    out = []
    for (net, ptype, a, b), rs in sorted(g.items()):
        ok = [r for r in rs if r["verdict"] != "skipped"]
        keys = Counter(blk["key"] for r in ok for blk in r["blocks_appeared"])
        cov = [r["summary"]["coverage"] for r in ok if r["summary"].get("coverage") is not None]
        out.append({
            "network": net, "type": ptype, "a": a, "b": b, "urls": len(rs), "skipped": len(rs) - len(ok),
            "same": sum(r["verdict"] == "same" for r in ok),
            "with_new_blocks": sum(1 for r in ok if r["summary"]["blocks_appeared"]),
            "with_changed_blocks": sum(1 for r in ok if r["summary"]["blocks_changed"]),
            "coverage_avg": round(sum(cov) / len(cov), 3) if cov else None,
            "top_new_blocks": [(k, n) for k, n in keys.most_common(6)],
        })
    return out


def print_diff_summary(summary: list[dict]) -> None:
    if not summary:
        return
    print("\n=== СРАВНЕНИЕ ВАРИАНТОВ (что появилось в B по сравнению с A) ===")
    for s in summary:
        cov = f"{s['coverage_avg']:.0%}" if s["coverage_avg"] is not None else "—"
        print(f"{s['network']}/{s['type']}: {s['a']} → {s['b']}   URL: {s['urls']} (пропущено {s['skipped']}), "
              f"без отличий: {s['same']}, с новыми блоками: {s['with_new_blocks']}, "
              f"с дополненными блоками: {s['with_changed_blocks']}, "
              f"текст B уже есть в A: {cov}")
        if s["top_new_blocks"]:
            print("    появляются: " + ", ".join(f"{k} ({n} стр.)" for k, n in s["top_new_blocks"]))
