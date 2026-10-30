"""URL selections behind dashboard metrics; SQL is restricted to this catalogue."""
from urllib.parse import urlencode
import re

from . import crawl

H, O, I = crawl._HTML, crawl._OK, crawl._IDX
CRAWL_SELECTIONS = {
    "crawl_pages_total": "1=1",
    "crawl_html_total": H,
    "crawl_2xx_share": f"{H} AND (status IS NULL OR status NOT BETWEEN 200 AND 299)",
    "crawl_3xx": f"{H} AND status BETWEEN 300 AND 399",
    "crawl_4xx": f"{H} AND status BETWEEN 400 AND 499",
    "crawl_5xx": f"{H} AND status BETWEEN 500 AND 599",
    "crawl_indexable_share": f"{O} AND COALESCE(indexability,'')!='Indexable'",
    "crawl_noindex": f"{O} AND indexability_status LIKE '%noindex%'",
    "crawl_no_response": "status IS NULL OR status=0",
    "crawl_canonical_missing": f"{I} AND trim(COALESCE(canonical,''))=''",
    "crawl_links_to_404": "status IN (404,410) AND COALESCE(inlinks,0)>0",
    "crawl_links_to_redirect": "status BETWEEN 300 AND 399 AND COALESCE(inlinks,0)>0",
    "crawl_orphans": f"{I} AND COALESCE(inlinks,0)=0 AND (depth IS NULL OR depth>0)",
    "crawl_title_missing": f"{I} AND COALESCE(title,'')=''",
    "crawl_title_dup": f"{I} AND audit_title_key(title) IN (SELECT audit_title_key(title) FROM crawl_pages WHERE import_id=? AND {I} AND audit_title_key(title)!='' GROUP BY audit_title_key(title) HAVING COUNT(*)>1)",
    "crawl_h1_missing": f"{I} AND COALESCE(h1,'')=''",
    "crawl_h1_multi": f"{I} AND COALESCE(h1_2,'')!=''",
    "crawl_meta_missing": f"{I} AND COALESCE(meta_desc,'')=''",
    "crawl_canonical_other": f"{O} AND COALESCE(canonical,'')!='' AND canonical!=address",
    "crawl_resp_avg_ms": f"{O} AND response_ms IS NOT NULL",
    "crawl_resp_p95_ms": f"{O} AND response_ms IS NOT NULL",
    "crawl_depth_avg": f"{O} AND depth IS NOT NULL",
    "crawl_depth_max": f"{O} AND depth=(SELECT MAX(depth) FROM crawl_pages WHERE import_id=? AND {O})",
}
PROBLEM_METRICS = ("crawl_3xx", "crawl_4xx", "crawl_5xx", "crawl_no_response", "crawl_canonical_missing",
                   "crawl_title_missing", "crawl_title_dup", "crawl_h1_missing", "crawl_h1_multi", "crawl_meta_missing", "crawl_orphans")
CRAWL_SELECTIONS["crawl_problems"] = " OR ".join(f"({CRAWL_SELECTIONS[k]})" for k in PROBLEM_METRICS)
CRAWL_REFERENCE = {"crawl_pages_total", "crawl_html_total", "crawl_noindex", "crawl_canonical_other",
                   "crawl_indexable_share", "crawl_resp_avg_ms", "crawl_resp_p95_ms", "crawl_depth_avg", "crawl_depth_max"}


def download_url(metric, point, network=""):
    if not point:
        return None
    if metric in CRAWL_SELECTIONS and point.startswith("crawl:"):
        return "/export/metric-urls.csv?" + urlencode({"metric": metric, "point": point, "net": network or "all"})
    if not point.startswith("run:"):
        return None
    args = {"run": point[4:], "net": network or ""}
    if metric.startswith("check:"):
        return "/export/urls.csv?" + urlencode({**args, "check": metric[6:], "only": "fail"})
    if metric.startswith("block:"):
        return "/export/urls.csv?" + urlencode({**args, "block": metric[6:], "only": "fail"})
    if metric in ("checks_fail", "checks_warn", "checks_score"):
        return "/export/urls.csv?" + urlencode({**args, "only": "fail", "statuses": {"checks_fail": "fail", "checks_warn": "warn", "checks_score": "fail,warn"}[metric]})
    if metric.startswith("probe:") or metric == "probes_pass_share":
        return "/export/tests.csv?" + urlencode({**args, "kind": metric[6:] if metric.startswith("probe:") else "", "verdict": "fail"})
    if metric in ("pages_ok_share", "pages_http_err", "pages_net_err", "pages_suspect"):
        return "/export/metric-urls.csv?" + urlencode({"metric": metric, "point": point, "net": network or "all"})
    return None


def download_label(metric):
    if metric in CRAWL_REFERENCE:
        return "Скачать URL показателя (CSV)"
    if metric.startswith(("check:", "block:")) or metric in ("checks_fail", "checks_warn", "checks_score"):
        return "Скачать сохранённые проблемные URL (CSV)"
    return "Скачать проблемные URL (CSV)"


def crawl_selection(db, import_id, metric):
    db.create_function("audit_title_key", 1, lambda v: re.sub(r"\s+", " ", v or "").strip().lower(), deterministic=True)
    condition = CRAWL_SELECTIONS[metric]
    args = [import_id] * (1 + condition.count("?"))
    reasons = []
    if metric == "crawl_problems":
        for key in PROBLEM_METRICS:
            reasons.append(f"CASE WHEN ({CRAWL_SELECTIONS[key]}) THEN '{crawl.CATALOG[key][0]}; ' ELSE '' END")
        reason_sql = " || ".join(reasons)
        args = [import_id] * reason_sql.count("?") + args
    else:
        reason_sql = "''"
    return db.execute(f"SELECT *, ({reason_sql}) AS selection_reasons FROM crawl_pages WHERE import_id=? AND ({condition}) ORDER BY address", args)
