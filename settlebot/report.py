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
    paid = {r["item_key"]: dict(r) for r in store.q("SELECT * FROM payments WHERE run_id=?", (run_id,))}
    appr: dict[str, list] = {}
    for r in store.q("SELECT * FROM approvals WHERE run_id=? ORDER BY decided_at", (run_id,)):
        appr.setdefault(r["item_key"], []).append({"action": r["action"], "approver": r["approver"],
                                                   "decision": r["decision"], "reason": r["reason"],
                                                   "decided_at": r["decided_at"]})
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
            "approvals": appr.get(it["item_key"], []),
            "payment": ({k: paid[it["item_key"]][k] for k in ("status", "provider", "provider_id", "receipt")}
                        if it["item_key"] in paid else None),
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


def unit_economics(report: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    f = cfg.get("finance", {})
    n = len(report["lines"])
    done = sum(1 for r in report["lines"] if r["status"] == "VERIFIED")
    price = float(f.get("price_inr_per_line") or 0)
    cost = report["total_cost_inr"]
    revenue = price * done                      # charged only for lines finished without a person
    bench = f.get("benchmark_inr_per_line")
    return {
        "lines": n, "finished_lines": done, "measured_cost_inr": round(cost, 4),
        "measured_cost_per_line_inr": round(cost / n, 4) if n else 0.0,
        "price_inr_per_line": price, "revenue_inr": round(revenue, 2),
        "margin_pct": round(100 * (revenue - cost) / revenue, 1) if revenue else None,
        "benchmark_inr_per_line": bench, "benchmark_source_url": f.get("benchmark_source_url"),
    }


def print_unit_economics(ue: dict[str, Any]) -> None:
    m = f"{ue['margin_pct']}%" if ue["margin_pct"] is not None else "n/a"
    print(f"Unit economics: {ue['finished_lines']}/{ue['lines']} lines finished | measured cost INR "
          f"{ue['measured_cost_inr']:.2f} ({ue['measured_cost_per_line_inr']:.2f}/line) | price INR "
          f"{ue['price_inr_per_line']:.2f}/finished line | revenue INR {ue['revenue_inr']:.2f} | margin {m}")
    if ue["benchmark_inr_per_line"] and ue["benchmark_source_url"]:
        print(f"Outsourced benchmark: INR {ue['benchmark_inr_per_line']}/line ({ue['benchmark_source_url']})")
    else:
        print("Outsourced benchmark: not sourced yet (set finance.benchmark_* in config.yaml)")


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
        if r.get("payment"):
            what += f"  [paid {r['payment']['provider_id']}]"
        print(f"{r['line']:>3}  {str(r['date']):<11} {str(r['description'])[:24]:<24} "
              f"{float(r['amount_inr']):>12,.2f}  {r['status']:<15} {what[:60]}")
    print("-" * 110)
