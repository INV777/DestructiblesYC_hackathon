"""Audit report: one row per line with status, payee, account, reasons and sources."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .store import Store


def build_report(store: Store, run_id: str) -> dict[str, Any]:
    run = store.get_run(run_id)
    if run is None:
        raise SystemExit(f"no run with id '{run_id}'")
    posted = {r["item_key"]: dict(r) for r in store.q("SELECT * FROM journal WHERE run_id=?", (run_id,))}
    rows = []
    for it in store.items(run_id):
        raw = json.loads(it["raw_json"])
        dec = json.loads(it["decision_json"]) if it["decision_json"] else {}
        rows.append({
            "line": it["line_no"],
            "kind": it["kind"],
            "date": raw.get("date"),
            "description": raw.get("description"),
            "amount_inr": raw.get("amount_inr", raw.get("amount")),
            "status": it["status"],
            "payee": dec.get("payee"),
            "account": (f"{dec['account_code']} {dec['account_name']}" if dec.get("account_code") else None),
            "explanation": dec.get("explanation"),
            "missing": dec.get("missing", []),
            "related_line": dec.get("related_line"),
            "citations": dec.get("citations", []),
            "posted_entry": posted.get(it["item_key"], {}).get("entry_id"),
        })
    cost = store.total_cost(run_id)
    calls = store.one("SELECT COUNT(*) AS n, SUM(cache_hit) AS c FROM ledger WHERE run_id=?", (run_id,))
    return {
        "run_id": run_id,
        "status": run["status"],
        "elapsed_seconds": round(float(run["elapsed_seconds"]), 2),
        "total_cost_inr": round(cost, 4),
        "cost_per_line_inr": round(cost / len(rows), 4) if rows else 0.0,
        "ledger_calls": calls["n"], "cache_hits": calls["c"] or 0,
        "lines": rows,
    }


def write_report(report: dict[str, Any], out_dir: str | Path) -> Path:
    p = Path(out_dir) / report["run_id"]
    p.mkdir(parents=True, exist_ok=True)
    path = p / "report.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def print_report(report: dict[str, Any]) -> None:
    print(f"\nRun {report['run_id']}  status={report['status']}  "
          f"cost=INR {report['total_cost_inr']:.2f}  ({report['cost_per_line_inr']:.2f}/line)  "
          f"time={report['elapsed_seconds']:.1f}s  calls={report['ledger_calls']} (cache hits {report['cache_hits']})")
    print("-" * 110)
    for r in report["lines"]:
        what = r["payee"] or "-"
        if r["account"]:
            what += f"  ->  {r['account']}"
        if r["status"] == "HELD_DUPLICATE":
            what = f"possible duplicate of line {r['related_line']}"
        elif r["status"] == "CANNOT_VERIFY":
            what = "cannot verify: " + "; ".join(r["missing"][:1])
        print(f"{r['line']:>3}  {str(r['date']):<11} {str(r['description'])[:24]:<24} "
              f"{float(r['amount_inr']):>12,.2f}  {r['status']:<15} {what[:60]}")
    print("-" * 110)
