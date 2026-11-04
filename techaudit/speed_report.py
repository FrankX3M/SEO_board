"""Presentation of persisted PSI measurements; never requests the external API."""
import json


def report(db, network, run_ids, current_run):
    history = []
    if run_ids:
        marks = ",".join("?" for _ in run_ids)
        rows = db.execute(f"SELECT cr.run_id, cr.created_at, cr.stats FROM check_results cr "
                          f"JOIN runs r ON r.run_id=cr.run_id WHERE cr.network=? AND cr.check_id='cwv' "
                          f"AND cr.run_id IN ({marks}) ORDER BY r.started_at, cr.id", [network, *run_ids])
        for row in rows:
            stats = json.loads(row["stats"] or "{}")
            for measurement in stats.get("speed_measurements", []):
                history.append({**measurement, "run_id": row["run_id"], "saved_at": row["created_at"]})
    return {"latest": [r for r in history if r["run_id"] == current_run], "history": history}
