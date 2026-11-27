"""SettleBot web console (FastAPI). Start it with `python main.py console`.

Reads go straight to the SQLite store; every run and settlement is a separate worker process
(see workers.py). Set SETTLEBOT_DEMO=1 for a public demo: replay and fixture data only (so nothing is
ever spent: no Crustdata calls, no Claude calls), the simulated payment provider, capped uploads and at
most a few workers at once. In the demo, runs from a visitor's own file are visible only to that
browser and are deleted after an hour.

Uploaded files are read by settlebot.importer: offline rules, no network, no AI.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import approvals
from ..config import env, load_config, load_dotenv
from ..importer import analyse, read_tables
from ..lookalike import core_name
from ..pii_guard import mask_account
from ..report import build_report, unit_economics
from ..runner import STEPS, load_items
from ..store import Store
from ..vendors import load_vendor_master
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
DEMO_MAX_LINES = 300
DEMO_MAX_BYTES = 200_000          # a JSON batch posted directly
DEMO_MAX_FILE_BYTES = 2_000_000   # an uploaded export
LOCAL_MAX_FILE_BYTES = 25_000_000
DEMO_MAX_WORKERS = 3
DEMO_PRIVATE_TTL_SECONDS = 3600   # runs and uploads from a visitor's own file
IMPORT_SAMPLES = ROOT / "tests" / "fixtures" / "imports"
SAMPLE_FILES = {
    "statement_withdrawal_deposit.xls": "A bank statement (old Excel, account details on top, totals below)",
    "statement_drcr_semicolon.csv": "A bank statement (semicolon CSV with a Dr/Cr column)",
    "invoice_register.xlsx": "A list of bills (Excel)",
}


class ImportChoices(BaseModel):
    """A person's corrections in the preview. Anything left out is chosen automatically."""
    sheet: str | None = None
    header_row: int | None = Field(default=None, ge=0)   # 0 means the table has no header row
    kind: str | None = None
    mapping: dict[str, int | None] | None = None
    amount_mode: str | None = None
    day_first: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class NewRun(BaseModel):
    sample: str | None = None
    lines: list[dict[str, Any]] | None = None
    import_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    choices: ImportChoices | None = None
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
               demo: bool | None = None, seed: bool | None = None) -> FastAPI:
    load_dotenv()
    demo = (os.environ.get("SETTLEBOT_DEMO") == "1") if demo is None else demo
    seed = (os.environ.get("SETTLEBOT_DEMO_SEED", "1") == "1") if seed is None else seed
    paths = Paths(Path(data_dir or os.environ.get("SETTLEBOT_DATA_DIR") or ROOT / "console_data"))
    paths.make()
    config_path = Path(config_path or ROOT / "config.yaml")
    cfg = load_config(config_path)
    workers = Workers(paths, config_path, keyless=demo)
    Store(paths.db)  # create the schema once, before any request
    local = threading.local()  # SQLite connections can't cross threads; FastAPI serves from a pool

    class _StorePerThread:
        def __getattr__(self, name):
            s = getattr(local, "store", None)
            if s is None:
                s = local.store = Store(paths.db)
            return getattr(s, name)

    store = _StorePerThread()
    known_vendors = {core_name(v["legal_name"]): vid for vid, v in load_vendor_master(cfg).items()}

    def owner_dep(x_settlebot_owner: str | None = Header(default=None)) -> str | None:
        """Who is asking, as far as the demo's privacy goes: a random token kept in that browser."""
        if not x_settlebot_owner or len(x_settlebot_owner) < 16:
            return None
        return hashlib.sha256(x_settlebot_owner.encode()).hexdigest()[:24]

    def need_owner(owner: str | None) -> None:
        if demo and owner is None:
            raise HTTPException(400, "Send an X-SettleBot-Owner header (any random text of 16 or more characters). "
                                     "In the public demo, runs from your own data are visible only to it.")

    def visible(meta: dict[str, Any], owner: str | None) -> bool:
        return not demo or meta.get("owner") is None or meta.get("owner") == owner

    def purge_expired() -> None:
        """Demo only: delete runs and uploads made from visitors' own files after an hour."""
        if not demo:
            return
        now = time.time()
        for m in workers.all_meta():
            if m.get("owner") and now - float(m.get("created_ts", now)) > DEMO_PRIVATE_TTL_SECONDS:
                workers.forget(m["run_id"])
                store.delete_run(m["run_id"])
                Path(m["input_path"]).unlink(missing_ok=True)
        for p in paths.imports.glob("*.json"):
            try:
                created = float(json.loads(p.read_text(encoding="utf-8")).get("created_ts", 0))
            except (OSError, ValueError):
                created = 0
            if now - created > DEMO_PRIVATE_TTL_SECONDS:
                p.with_suffix(".bin").unlink(missing_ok=True)
                p.unlink(missing_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if demo and seed and not workers.all_meta():
            # A visitor's first look should be finished runs they can open, not an empty list.
            for key in SAMPLES:
                try:
                    start_run(NewRun(sample=key, mode=SAMPLES[key]["mode"]))
                except HTTPException:
                    pass
        yield
        workers.stop_all()

    app = FastAPI(title="SettleBot console", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.state.workers, app.state.paths, app.state.demo = workers, paths, demo

    def live_allowed() -> bool:
        return not demo and bool(env("CRUSTDATA_API_KEY"))

    def meta_or_404(run_id: str, owner: str | None) -> dict[str, Any]:
        meta = workers.load_meta(run_id)
        if not meta or not visible(meta, owner):
            raise HTTPException(404, f"No run called {run_id}." + (
                " In the public demo, a run from someone's own file opens only in the browser that started it, "
                "and is deleted after an hour." if demo else ""))
        return meta

    def import_or_404(import_id: str, owner: str | None) -> dict[str, Any]:
        p = paths.imports / f"{import_id}.json"
        if not re.fullmatch(r"[0-9a-f]{16}", import_id) or not p.exists():
            raise HTTPException(404, "That upload has expired or was already used. Upload the file again.")
        meta = json.loads(p.read_text(encoding="utf-8"))
        if not visible(meta, owner):
            raise HTTPException(404, "That upload has expired or was already used. Upload the file again.")
        return meta

    def read_import(meta: dict[str, Any], choices: dict[str, Any]):
        data = (paths.imports / f"{meta['import_id']}.bin").read_bytes()
        try:
            sheets = read_tables(data, meta["filename"])
            return analyse(sheets, meta["filename"], choices, known_vendors)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except Exception as e:  # a corrupt or unusual workbook
            raise HTTPException(400, "SettleBot couldn't find a table in this file. It reads Excel (.xlsx, .xls), "
                                     f"CSV and text exports. ({type(e).__name__})") from e

    def import_view(meta: dict[str, Any], res) -> dict[str, Any]:
        d = res.to_dict()
        for ln in d["lines"]:  # account numbers never come back in full
            if ln.get("bank_account"):
                ln["bank_account"] = _masked_bank(ln["bank_account"])
        for c in d["columns"]:
            c["sample"] = _mask_long_numbers(c["sample"])
        for r in d["skipped"]:
            r["text"] = _mask_long_numbers(r.get("text"))
        return {"import_id": meta["import_id"], **d}

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
            "sample_files": [{"name": n, "title": t} for n, t in SAMPLE_FILES.items()],
            "upload_max_mb": (DEMO_MAX_FILE_BYTES if demo else LOCAL_MAX_FILE_BYTES) // 1_000_000,
            "private_hours": DEMO_PRIVATE_TTL_SECONDS // 3600 if demo else None,
        }

    @app.get("/api/sample-files/{name}")
    def sample_file(name: str):
        if name not in SAMPLE_FILES:
            raise HTTPException(404, "No sample file with that name.")
        return FileResponse(IMPORT_SAMPLES / name, filename=name)

    # ---- uploads: read any export, show what was read, let a person correct it ----------
    @app.post("/api/imports", status_code=201)
    async def create_import(request: Request, filename: str = "upload", owner: str | None = Depends(owner_dep)):
        need_owner(owner)
        await run_in_threadpool(purge_expired)
        data = await request.body()
        limit = DEMO_MAX_FILE_BYTES if demo else LOCAL_MAX_FILE_BYTES
        if not data:
            raise HTTPException(400, "The file is empty.")
        if len(data) > limit:
            raise HTTPException(413, f"Files up to {limit // 1_000_000} MB can be read here.")
        if data[:5] == b"%PDF-":
            raise HTTPException(400, "PDFs can't be read yet. Download the statement from your bank's website as "
                                     "Excel or CSV instead.")
        import_id = secrets.token_hex(8)
        name = re.sub(r"[^\w.\- ()]", "_", Path(filename).name)[:80] or "upload"
        meta = {"import_id": import_id, "filename": name, "bytes": len(data), "owner": owner if demo else None,
                "sha256": hashlib.sha256(data).hexdigest(), "created_ts": time.time()}
        (paths.imports / f"{import_id}.bin").write_bytes(data)
        (paths.imports / f"{import_id}.json").write_text(json.dumps(meta), encoding="utf-8")
        try:
            res = await run_in_threadpool(read_import, meta, {})
        except HTTPException:
            for suffix in (".bin", ".json"):
                (paths.imports / f"{import_id}{suffix}").unlink(missing_ok=True)
            raise
        return import_view(meta, res)

    @app.post("/api/imports/{import_id}/preview")
    def preview_import(import_id: str, choices: ImportChoices, owner: str | None = Depends(owner_dep)):
        meta = import_or_404(import_id, owner)
        return import_view(meta, read_import(meta, choices.as_dict()))

    # ---- runs -----------------------------------------------------------------
    @app.get("/api/runs")
    def list_runs(owner: str | None = Depends(owner_dep)):
        purge_expired()
        out = []
        for m in [m for m in workers.all_meta() if visible(m, owner)][:100]:
            run = store.get_run(m["run_id"])
            counts: dict[str, int] = {}
            for it in store.items(m["run_id"]):
                counts[it["status"]] = counts.get(it["status"], 0) + 1
            out.append({**_public_meta(m), "status": run["status"] if run else "STARTING",
                        "counts": counts, "worker": workers.state(m["run_id"])})
        return out

    @app.post("/api/runs", status_code=201)
    def new_run(body: NewRun, owner: str | None = Depends(owner_dep)):
        purge_expired()
        return start_run(body, owner)

    def start_run(body: NewRun, owner: str | None = None) -> dict[str, str]:
        if body.mode not in MODES:
            raise HTTPException(400, f"Unknown data mode '{body.mode}'. Use replay, fixtures or live.")
        if body.mode == "live" and not live_allowed():
            raise HTTPException(400, "Live mode is off here. It needs CRUSTDATA_API_KEY in .env, and is never "
                                     "available in the public demo.")
        if demo and workers.running_count() >= DEMO_MAX_WORKERS:
            raise HTTPException(429, "The demo is busy with other runs. Try again in a few seconds.")
        if sum(bool(x) for x in (body.sample, body.lines, body.import_id)) != 1:
            raise HTTPException(400, "Send one of: a sample name, your own lines, or an uploaded file.")
        source, imported = None, None
        if body.sample:
            if body.sample not in SAMPLES:
                raise HTTPException(400, f"Unknown sample '{body.sample}'.")
            raw = SAMPLES[body.sample]["path"].read_text(encoding="utf-8")
            label = body.label or SAMPLES[body.sample]["title"]
        elif body.import_id:
            imported = import_or_404(body.import_id, owner)
            res = read_import(imported, body.choices.as_dict() if body.choices else {})
            if res.problems:
                raise HTTPException(400, res.problems[0])
            if demo and len(res.lines) > DEMO_MAX_LINES:
                raise HTTPException(413, f"The demo takes up to {DEMO_MAX_LINES} lines per file.")
            raw = json.dumps(res.lines, ensure_ascii=False, indent=1)
            label = body.label or imported["filename"]
            source = {"file": imported["filename"], "sha256": imported["sha256"], "sheet": res.sheet,
                      "header_row": res.header_row, "kind": res.kind, "money_in_left_out": res.money_in,
                      "rows_skipped": len(res.skipped)}
        else:
            raw = json.dumps(body.lines, ensure_ascii=False, indent=1)
            label = body.label or "Uploaded batch"
            if demo and (len(body.lines) > DEMO_MAX_LINES or len(raw.encode()) > DEMO_MAX_BYTES):
                raise HTTPException(413, f"The demo takes up to {DEMO_MAX_LINES} lines per batch.")
        private = demo and not body.sample
        if private:
            need_owner(owner)
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        input_path = paths.uploads / (f"{run_id}.json" if private else f"{sha}.json")
        if not input_path.exists():
            input_path.write_text(raw, encoding="utf-8")
        try:
            items, _ = load_items(input_path)
        except (ValueError, KeyError, TypeError) as e:
            raise HTTPException(400, f"That batch could not be read: {e}") from e
        if not items:
            raise HTTPException(400, "That batch has no lines.")
        meta = {
            "run_id": run_id, "label": label, "mode": body.mode, "slow": body.slow and body.mode != "live",
            "input_path": str(input_path), "lines": len(items),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "kills": 0, "resumes": 0,
            "pay_provider": "razorpay" if (body.mode == "live" and _can_pay_live()) else "fake",
            "created_ts": time.time(), "owner": owner if private else None, "source": source,
        }
        workers.save_meta(meta)
        workers.start(meta)
        if imported:  # the lines are saved; the uploaded file itself is no longer kept
            for suffix in (".bin", ".json"):
                (paths.imports / f"{imported['import_id']}{suffix}").unlink(missing_ok=True)
        return {"run_id": run_id}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, owner: str | None = Depends(owner_dep)):
        meta = meta_or_404(run_id, owner)
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
    def kill(run_id: str, owner: str | None = Depends(owner_dep)):
        meta = meta_or_404(run_id, owner)
        if not workers.kill(run_id):
            raise HTTPException(409, "No worker is running for this run, so there is nothing to crash.")
        meta["kills"] = int(meta.get("kills", 0)) + 1
        workers.save_meta(meta)
        return {"killed": True, "kills": meta["kills"]}

    @app.post("/api/runs/{run_id}/resume")
    def resume(run_id: str, owner: str | None = Depends(owner_dep)):
        meta = meta_or_404(run_id, owner)
        if workers.state(run_id)["running"]:
            raise HTTPException(409, "This run is already running.")
        if demo and workers.running_count() >= DEMO_MAX_WORKERS:
            raise HTTPException(429, "The demo is busy with other runs. Try again in a few seconds.")
        meta["resumes"] = int(meta.get("resumes", 0)) + 1
        workers.save_meta(meta)
        workers.start(meta, settle=bool(store.q("SELECT 1 FROM payments WHERE run_id=? LIMIT 1", (run_id,))))
        return {"resumed": True}

    @app.post("/api/runs/{run_id}/decisions")
    def decide(run_id: str, body: Decision, owner: str | None = Depends(owner_dep)):
        meta_or_404(run_id, owner)
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
    def settle(run_id: str, owner: str | None = Depends(owner_dep)):
        meta = meta_or_404(run_id, owner)
        run = store.get_run(run_id)
        if not run or run["status"] == "RUNNING" or workers.state(run_id)["running"]:
            raise HTTPException(409, "Let the run finish before paying.")
        if demo and workers.running_count() >= DEMO_MAX_WORKERS:
            raise HTTPException(429, "The demo is busy with other runs. Try again in a few seconds.")
        workers.start(meta, settle=True)
        return {"started": True, "provider": meta.get("pay_provider")}

    @app.get("/api/runs/{run_id}/ledger")
    def ledger(run_id: str, owner: str | None = Depends(owner_dep)):
        meta_or_404(run_id, owner)
        lines = {it["item_key"]: it["line_no"] for it in store.items(run_id)}
        rows = store.q("SELECT ts, item_key, step, attempt, provider, op, units, unit_type, cost_inr, latency_ms, ok, "
                       "cache_hit, detail FROM ledger WHERE run_id=? ORDER BY id", (run_id,))
        return [{**{k: r[k] for k in r.keys() if k != "item_key"}, "line": lines.get(r["item_key"])} for r in rows]

    @app.get("/api/runs/{run_id}/evidence/{evidence_id}")
    def evidence(run_id: str, evidence_id: str, owner: str | None = Depends(owner_dep)):
        meta_or_404(run_id, owner)
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
    out = {k: m.get(k) for k in ("run_id", "label", "mode", "slow", "lines", "created_at", "kills", "resumes",
                                 "pay_provider", "source")}
    out["private"] = bool(m.get("owner"))
    if m.get("owner") and m.get("created_ts"):
        out["deleted_after_ts"] = float(m["created_ts"]) + DEMO_PRIVATE_TTL_SECONDS
    return out


def _mask_long_numbers(text: str | None) -> str | None:
    """Card and account numbers in a preview show only their last 4 digits."""
    return re.sub(r"\d{9,}", lambda m: mask_account(m.group()), text) if text else text


def _masked_bank(b: dict[str, Any] | None) -> dict[str, Any] | None:
    if not b:
        return None
    return {"account": mask_account(b.get("account_number")), "ifsc": b.get("ifsc"), "name": b.get("name")}
