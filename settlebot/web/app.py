"""SettleBot web console (FastAPI). Start it with `python main.py console`.

Reads go straight to the SQLite store; every run and settlement is a separate worker process
(see workers.py). Set SETTLEBOT_DEMO=1 for a public demo: replay and fixture data only, the
simulated payment provider, small uploads and at most a few workers at once.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import approvals
from ..config import env, load_config, load_dotenv
from ..pii_guard import mask_account
from ..report import build_report, unit_economics
from ..runner import STEPS, load_items
from ..store import Store
from .workers import MODES, ROOT, Paths, Workers

STATIC = Path(__file__).resolve().parent / "static"
SAMPLES = {
    "bank-lines": {
        "path": ROOT / "tests" / "fixtures" / "pdf_example_bank_lines.json",
        "title": "Bank lines from the problem statement",
        "about": "A Swiggy card charge, an AWS charge and its duplicate, and an unknown Paytm payee.",
        "mode": "replay",
    },
    "invoices": {
        "path": ROOT / "tests" / "fixtures" / "invoices_sample.json",
        "title": "Freight vendor invoices",
        "about": "A clean freight bill, changed bank details, a re-sent invoice, an unknown vendor "
                 "and two lookalike senders.",
        "mode": "fixtures",
    },
}
DEMO_MAX_LINES = 50
DEMO_MAX_BYTES = 64_000
DEMO_MAX_WORKERS = 3


class NewRun(BaseModel):
    sample: str | None = None
    lines: list[dict[str, Any]] | None = None
    mode: str = "replay"
    slow: bool = False
    label: str | None = Field(default=None, max_length=80)


class Decision(BaseModel):
    line: int
    action: str
    approve: bool
    approver: str = Field(min_length=1, max_length=60)
    reason: str | None = Field(default=None, max_length=300)


def _can_pay_live() -> bool:
    return (env("RAZORPAY_KEY_ID") or "").startswith("rzp_test_") and bool(env("RAZORPAY_KEY_SECRET"))


def create_app(data_dir: str | Path | None = None, config_path: str | Path | None = None,
               demo: bool | None = None) -> FastAPI:
    load_dotenv()
    demo = (os.environ.get("SETTLEBOT_DEMO") == "1") if demo is None else demo
    paths = Paths(Path(data_dir or os.environ.get("SETTLEBOT_DATA_DIR") or ROOT / "console_data"))
    paths.make()
    config_path = Path(config_path or ROOT / "config.yaml")
    cfg = load_config(config_path)
    workers = Workers(paths, config_path)
    Store(paths.db)  # create the schema once, before any request
    local = threading.local()  # SQLite connections can't cross threads; FastAPI serves from a pool

    class _StorePerThread:
        def __getattr__(self, name):
            s = getattr(local, "store", None)
            if s is None:
                s = local.store = Store(paths.db)
            return getattr(s, name)

    store = _StorePerThread()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        workers.stop_all()

    app = FastAPI(title="SettleBot console", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.state.workers, app.state.paths, app.state.demo = workers, paths, demo

    def live_allowed() -> bool:
        return not demo and bool(env("CRUSTDATA_API_KEY"))

    def meta_or_404(run_id: str) -> dict[str, Any]:
        meta = workers.load_meta(run_id)
        if not meta:
            raise HTTPException(404, f"No run called {run_id}.")
        return meta

    # ---- info -----------------------------------------------------------------
    @app.get("/api/info")
    def info():
        modes = {k: {"about": v, "available": (live_allowed() if k == "live" else True)} for k, v in MODES.items()}
        return {
            "demo": demo,
            "modes": modes,
            "samples": {k: {kk: vv for kk, vv in s.items() if kk != "path"} for k, s in SAMPLES.items()},
            "limits": cfg.get("limits", {}),
            "approval_rules": cfg.get("approvals", {}).get("rules", []),
            "price_inr_per_line": cfg.get("finance", {}).get("price_inr_per_line"),
            "payments": "Razorpay test mode" if (not demo and _can_pay_live()) else "simulated provider",
            "steps": STEPS,
        }

    # ---- runs -----------------------------------------------------------------
    @app.get("/api/runs")
    def list_runs():
        out = []
        for m in workers.all_meta()[:100]:
            run = store.get_run(m["run_id"])
            counts: dict[str, int] = {}
            for it in store.items(m["run_id"]):
                counts[it["status"]] = counts.get(it["status"], 0) + 1
            out.append({**_public_meta(m), "status": run["status"] if run else "STARTING",
                        "counts": counts, "worker": workers.state(m["run_id"])})
        return out

    @app.post("/api/runs", status_code=201)
    def new_run(body: NewRun):
        if body.mode not in MODES:
            raise HTTPException(400, f"Unknown data mode '{body.mode}'. Use replay, fixtures or live.")
        if body.mode == "live" and not live_allowed():
            raise HTTPException(400, "Live mode is off here. It needs CRUSTDATA_API_KEY in .env, and is never "
                                     "available in the public demo.")
        if demo and workers.running_count() >= DEMO_MAX_WORKERS:
            raise HTTPException(429, "The demo is busy with other runs. Try again in a few seconds.")
        if bool(body.sample) == bool(body.lines):
            raise HTTPException(400, "Send either a sample name or your own lines, not both.")
        if body.sample:
            if body.sample not in SAMPLES:
                raise HTTPException(400, f"Unknown sample '{body.sample}'.")
            raw = SAMPLES[body.sample]["path"].read_text(encoding="utf-8")
            label = body.label or SAMPLES[body.sample]["title"]
        else:
            raw = json.dumps(body.lines, ensure_ascii=False, indent=1)
            label = body.label or "Uploaded batch"
            if demo and (len(body.lines) > DEMO_MAX_LINES or len(raw.encode()) > DEMO_MAX_BYTES):
                raise HTTPException(413, f"The demo takes up to {DEMO_MAX_LINES} lines per batch.")
        sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        input_path = paths.uploads / f"{sha}.json"
        if not input_path.exists():
            input_path.write_text(raw, encoding="utf-8")
        try:
            items, _ = load_items(input_path)
        except (ValueError, KeyError, TypeError) as e:
            raise HTTPException(400, f"That batch could not be read: {e}") from e
        if not items:
            raise HTTPException(400, "That batch has no lines.")
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        meta = {
            "run_id": run_id, "label": label, "mode": body.mode, "slow": body.slow and body.mode != "live",
            "input_path": str(input_path), "lines": len(items),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "kills": 0, "resumes": 0,
            "pay_provider": "razorpay" if (body.mode == "live" and _can_pay_live()) else "fake",
        }
        workers.save_meta(meta)
        workers.start(meta)
        return {"run_id": run_id}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        meta = meta_or_404(run_id)
        run = store.get_run(run_id)
        worker = workers.state(run_id)
        if run is None:  # the worker has not created the run yet
            return {"meta": _public_meta(meta), "status": "STARTING", "worker": worker, "lines": [],
                    "pending": [], "totals": None, "unit_economics": None, "log": workers.log_tail(run_id)}
        report = build_report(store, run_id)
        steps: dict[str, dict[str, dict]] = {}
        for r in store.q("SELECT item_key, step, status, attempts FROM steps WHERE run_id=?", (run_id,)):
            steps.setdefault(r["item_key"], {})[r["step"]] = {"status": r["status"], "attempts": r["attempts"]}
        keys = {it["line_no"]: it["item_key"] for it in store.items(run_id)}
        raws = {it["line_no"]: json.loads(it["raw_json"]) for it in store.items(run_id)}
        lines = []
        for row in report["lines"]:
            raw = raws[row["line"]]
            st = steps.get(keys[row["line"]], {})
            lines.append({**row,
                          "steps": [{"step": s, **st.get(s, {"status": "WAITING", "attempts": 0})} for s in STEPS],
                          "invoice_no": raw.get("invoice_no"), "vendor_id": raw.get("vendor_id"),
                          "sender_email": raw.get("sender_email"),
                          "bank_account": _masked_bank(raw.get("bank_account"))})
        pending = [{"line": p["item"]["line_no"], "action": p["action"], "amount_inr": p["amount_inr"],
                    "need": p["need"],
                    "have": [{"approver": h["approver"], "decision": h["decision"]} for h in p["have"]]}
                   for p in approvals.pending_actions(store, cfg, run_id)]
        ue = unit_economics(report, cfg)
        retried = store.one("SELECT COALESCE(SUM(attempts - 1), 0) AS n FROM steps WHERE run_id=?", (run_id,))["n"]
        adopted = store.one("SELECT COUNT(*) AS n FROM ledger WHERE run_id=? AND op='adopt_existing_order'",
                            (run_id,))["n"]
        return {
            "meta": _public_meta(meta), "status": run["status"], "worker": worker,
            "lines": lines, "pending": pending, "unit_economics": ue,
            "totals": {k: report[k] for k in ("elapsed_seconds", "total_cost_inr", "ledger_calls", "cache_hits",
                                              "replay")}
            | {"steps_redone_after_crash": retried, "orders_adopted_after_crash": adopted,
               "posted": store.one("SELECT COUNT(*) AS n FROM journal WHERE run_id=?", (run_id,))["n"],
               # Measured, not assumed: lines with more than one journal entry or payment order.
               "posted_twice": store.one("SELECT COUNT(*) AS n FROM (SELECT item_key FROM journal WHERE run_id=? "
                                         "GROUP BY item_key HAVING COUNT(*) > 1)", (run_id,))["n"],
               "paid_twice": store.one("SELECT COUNT(*) AS n FROM (SELECT item_key FROM payments WHERE run_id=? "
                                       "GROUP BY item_key HAVING COUNT(*) > 1)", (run_id,))["n"]},
            "log": workers.log_tail(run_id) if worker["exit_code"] not in (None, 0) else "",
        }

    @app.post("/api/runs/{run_id}/kill")
    def kill(run_id: str):
        meta = meta_or_404(run_id)
        if not workers.kill(run_id):
            raise HTTPException(409, "No worker is running for this run, so there is nothing to crash.")
        meta["kills"] = int(meta.get("kills", 0)) + 1
        workers.save_meta(meta)
        return {"killed": True, "kills": meta["kills"]}

    @app.post("/api/runs/{run_id}/resume")
    def resume(run_id: str):
        meta = meta_or_404(run_id)
        if workers.state(run_id)["running"]:
            raise HTTPException(409, "This run is already running.")
        if demo and workers.running_count() >= DEMO_MAX_WORKERS:
            raise HTTPException(429, "The demo is busy with other runs. Try again in a few seconds.")
        meta["resumes"] = int(meta.get("resumes", 0)) + 1
        workers.save_meta(meta)
        workers.start(meta, settle=bool(store.q("SELECT 1 FROM payments WHERE run_id=? LIMIT 1", (run_id,))))
        return {"resumed": True}

    @app.post("/api/runs/{run_id}/decisions")
    def decide(run_id: str, body: Decision):
        meta_or_404(run_id)
        pending = approvals.pending_actions(store, cfg, run_id)
        match = [p for p in pending if p["item"]["line_no"] == body.line and p["action"] == body.action]
        if not match:
            raise HTTPException(409, f"Line {body.line} has no {body.action} waiting for a decision.")
        p = match[0]
        name = body.approver.strip()
        if any(h["approver"] == name for h in p["have"]):
            raise HTTPException(409, f"{name} has already decided this. A second approval must come from "
                                     "someone else.")
        approvals.record(store, run_id, p["item"]["item_key"], p["action"], name, body.approve,
                         (body.reason or "").strip() or None)
        st, rows = approvals.state(store, cfg, run_id, p["item"]["item_key"], p["action"], p["amount_inr"])
        return {"state": st, "approvals": len([r for r in rows if r["decision"] == "APPROVE"]), "need": p["need"]}

    @app.post("/api/runs/{run_id}/settle")
    def settle(run_id: str):
        meta = meta_or_404(run_id)
        run = store.get_run(run_id)
        if not run or run["status"] == "RUNNING" or workers.state(run_id)["running"]:
            raise HTTPException(409, "Let the run finish before paying.")
        if demo and workers.running_count() >= DEMO_MAX_WORKERS:
            raise HTTPException(429, "The demo is busy with other runs. Try again in a few seconds.")
        workers.start(meta, settle=True)
        return {"started": True, "provider": meta.get("pay_provider")}

    @app.get("/api/runs/{run_id}/ledger")
    def ledger(run_id: str):
        meta_or_404(run_id)
        lines = {it["item_key"]: it["line_no"] for it in store.items(run_id)}
        rows = store.q("SELECT ts, item_key, step, attempt, provider, op, units, unit_type, cost_inr, latency_ms, ok, "
                       "cache_hit, detail FROM ledger WHERE run_id=? ORDER BY id", (run_id,))
        return [{**{k: r[k] for k in r.keys() if k != "item_key"}, "line": lines.get(r["item_key"])} for r in rows]

    @app.get("/api/runs/{run_id}/evidence/{evidence_id}")
    def evidence(run_id: str, evidence_id: str):
        meta_or_404(run_id)
        row = store.evidence(evidence_id)
        if not row or row["run_id"] != run_id:
            raise HTTPException(404, "No stored response with that id in this run.")
        return {"evidence_id": evidence_id, "source": row["source"], "url": row["url"],
                "retrieved_at": row["retrieved_at"], "body_sha256": row["body_sha256"], "body": row["body"][:400_000]}

    # ---- page -------------------------------------------------------------------
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/healthz", include_in_schema=False)
    def health():
        return {"ok": True}

    return app


def _public_meta(m: dict[str, Any]) -> dict[str, Any]:
    return {k: m.get(k) for k in ("run_id", "label", "mode", "slow", "lines", "created_at", "kills", "resumes",
                                  "pay_provider")}


def _masked_bank(b: dict[str, Any] | None) -> dict[str, Any] | None:
    if not b:
        return None
    return {"account": mask_account(b.get("account_number")), "ifsc": b.get("ifsc"), "name": b.get("name")}
