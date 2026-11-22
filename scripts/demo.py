#!/usr/bin/env python3
"""Screening / video demo that runs the same on Windows PowerShell, macOS and Linux.

  python scripts/demo.py              # live (needs .env)
  python scripts/demo.py --pause      # waits for Enter before each step (for recording)
  python scripts/demo.py --offline    # fixtures + fake payment provider, no network

Steps: 1 bank lines verified and posted, 2 kill mid-run and restart (each action once),
3 invoice holds, 4 approve and pay exactly once (test mode), 5 cost ledger.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BANK = "tests/fixtures/pdf_example_bank_lines.json"
INV = "tests/fixtures/invoices_sample.json"
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def main_py(*args: str, quiet: bool = False, stdin_null: bool = True) -> int:
    cmd = [sys.executable, "main.py", *args]
    kw = {"cwd": ROOT, "env": ENV}
    if stdin_null:
        kw["stdin"] = subprocess.DEVNULL
    if quiet:
        kw["stdout"] = subprocess.DEVNULL
        kw["stderr"] = subprocess.DEVNULL
    return subprocess.run(cmd, **kw).returncode


def step(n: int, title: str, pause: bool) -> None:
    if pause:
        input(f"\n[Enter] step {n}: {title} ")
    print(f"\n{'=' * 20} {n}. {title} {'=' * 20}", flush=True)


def db() -> sqlite3.Connection:
    return sqlite3.connect(ROOT / "state.db")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--pause", action="store_true", help="wait for Enter before each step")
    ap.add_argument("--kill-after", type=float, default=None, help="seconds before the kill (default 3 live, 1.5 offline)")
    a = ap.parse_args()

    run = "demo-" + datetime.now().strftime("%H%M%S")
    extra = ["--fake", "--slow", "0.8"] if a.offline else []
    pay = ["--pay-provider", "fake", "--fake-remote-db", f"out/{run}-remote.db"] if a.offline else []
    kill_after = a.kill_after or (1.5 if a.offline else 3.0)
    (ROOT / "out").mkdir(exist_ok=True)

    step(1, f"Verify and post the bank lines ({'offline fixtures' if a.offline else 'live data'})", a.pause)
    main_py("run", BANK, "--run-id", f"{run}-bank", *extra)
    rep = json.loads((ROOT / "out" / f"{run}-bank" / "report.json").read_text(encoding="utf-8"))
    c = (rep["lines"][0].get("citations") or [None])[0]
    if c:
        print("\nSource for line 1:", json.dumps({k: c[k] for k in ("source_url", "field", "field_or_passage",
                                                                   "retrieved_at")}, indent=2, ensure_ascii=False))

    step(2, f"Kill on stage: start, hard-kill after {kill_after}s, restart with the same run-id", a.pause)
    p = subprocess.Popen([sys.executable, "main.py", "run", BANK, "--run-id", f"{run}-kill", *extra],
                         cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
    time.sleep(kill_after)
    if p.poll() is None:
        p.kill()  # SIGKILL on Linux/macOS, TerminateProcess on Windows: no cleanup runs
        p.wait()
        print(f"KILLED process {p.pid} mid-run")
    else:
        print("(run finished before the kill; rerun with --kill-after 1)")
    main_py("run", BANK, "--run-id", f"{run}-kill", *extra)
    with db() as con:
        posted, distinct = con.execute("SELECT COUNT(*), COUNT(DISTINCT item_key) FROM journal WHERE run_id=?",
                                       (f"{run}-kill",)).fetchone()
        redone = con.execute("SELECT COALESCE(SUM(attempts-1),0) FROM steps WHERE run_id=?", (f"{run}-kill",)).fetchone()[0]
        hits = con.execute("SELECT COUNT(*) FROM ledger WHERE run_id=? AND cache_hit=1", (f"{run}-kill",)).fetchone()[0]
    print(f"\nAFTER RESTART: journal postings {posted} (distinct lines {distinct}), steps redone {redone}, "
          f"cached responses reused {hits}")

    step(3, "Invoices: bank-detail fraud, duplicate, unknown vendor, lookalike domain and name", a.pause)
    main_py("run", INV, "--run-id", f"{run}-inv", *extra)

    step(4, "A person approves line 1, then pay exactly once (test mode)", a.pause)
    if a.pause and sys.stdin.isatty():
        main_py("approve", "--run-id", f"{run}-inv", stdin_null=False)   # interactive decision cards
    else:
        main_py("approve", "--run-id", f"{run}-inv", "--line", "1", "--action", "PAY", "--yes",
                "--approver", os.environ.get("APPROVER", "finance-controller"))
    main_py("run", INV, "--run-id", f"{run}-inv", *extra, "--pay", *pay, quiet=True)
    print("-- paying a second time:")
    main_py("run", INV, "--run-id", f"{run}-inv", *extra, "--pay", *pay, quiet=True)
    with db() as con:
        rows = con.execute("SELECT receipt, status, provider_id FROM payments WHERE run_id=?", (f"{run}-inv",)).fetchall()
    print(f"PAYMENTS RECORDED: {len(rows)} -> {rows}")

    step(5, "Cost ledger (last calls of the bank run)", a.pause)
    for line in (ROOT / "out" / f"{run}-bank" / "cost_ledger.jsonl").read_text(encoding="utf-8").splitlines()[-6:]:
        r = json.loads(line)
        print(f"{r['provider']:<10} {r['op']:<22} units={r['units']:<6} INR={r['cost_inr']:<8} "
              f"{r['latency_ms']} ms cache={r['cache_hit']}")
    print(f"\nDone. Reports in out/{run}-*/report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
