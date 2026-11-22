#!/usr/bin/env python3
"""Kill test: start a run, SIGKILL it at a random moment, restart with the same --run-id,
repeat until it finishes, then check that every action happened exactly once.

  python scripts/kill_test.py --rounds 20

Uses fixture sources with an artificial delay so kills land mid-step. No network, no cost.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from settlebot.store import Store  # noqa: E402

EXPECTED = {1: "VERIFIED", 2: "VERIFIED", 3: "HELD_DUPLICATE", 4: "CANNOT_VERIFY"}


def one_round(i: int, input_path: Path, slow: float, max_restarts: int) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix=f"killtest{i}_"))
    db, out = tmp / "state.db", tmp / "out"
    cmd = [sys.executable, str(ROOT / "main.py"), "run", str(input_path), "--run-id", f"kill-{i}",
           "--fake", "--slow", str(slow), "--db", str(db), "--out", str(out), "--config", str(ROOT / "config.yaml")]
    kills = 0
    for _ in range(max_restarts):
        p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        delay = random.uniform(0.02, slow * 6)
        try:
            p.wait(timeout=delay)
        except subprocess.TimeoutExpired:
            os.kill(p.pid, signal.SIGKILL)
            p.wait()
            kills += 1
            continue
        if p.returncode != 0:
            raise SystemExit(f"round {i}: run exited {p.returncode}: {p.stderr.read().decode()[-500:]}")
        break
    else:
        # Let it finish without a kill so the round can be checked.
        subprocess.run(cmd, cwd=ROOT, check=True, stdout=subprocess.DEVNULL)

    s = Store(db)
    run_id = f"kill-{i}"
    statuses = {r["line_no"]: r["status"] for r in s.items(run_id)}
    journal = s.q("SELECT item_key, COUNT(*) AS n FROM journal WHERE run_id=? GROUP BY item_key", (run_id,))
    dup_steps = s.one("SELECT COUNT(*) AS n FROM (SELECT item_key, step, COUNT(*) c FROM steps "
                      "WHERE run_id=? GROUP BY item_key, step HAVING c > 1)", (run_id,))["n"]
    dup_ledger = s.one("SELECT COUNT(*) AS n FROM (SELECT item_key, step, attempt, op, cache_hit, detail, COUNT(*) c "
                       "FROM ledger WHERE run_id=? AND ok=1 GROUP BY 1,2,3,4,5,6 HAVING c > 1)", (run_id,))["n"]
    problems = []
    if statuses != EXPECTED:
        problems.append(f"statuses {statuses}")
    if len(journal) != 2 or any(r["n"] != 1 for r in journal):
        problems.append(f"journal rows {[dict(r) for r in journal]}")
    if dup_steps:
        problems.append(f"{dup_steps} steps recorded twice")
    if dup_ledger:
        problems.append(f"{dup_ledger} duplicate ledger rows")
    retried = s.one("SELECT COALESCE(SUM(attempts - 1), 0) AS n FROM steps WHERE run_id=?", (run_id,))["n"]
    return {"round": i, "kills": kills, "steps_retried_after_kill": retried, "ok": not problems,
            "problems": problems}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--slow", type=float, default=0.15)
    ap.add_argument("--max-restarts", type=int, default=30)
    ap.add_argument("--input", default=str(ROOT / "tests" / "fixtures" / "pdf_example_bank_lines.json"))
    args = ap.parse_args()

    results = [one_round(i, Path(args.input), args.slow, args.max_restarts) for i in range(1, args.rounds + 1)]
    total_kills = sum(r["kills"] for r in results)
    failed = [r for r in results if not r["ok"]]
    for r in results:
        mark = "PASS" if r["ok"] else "FAIL"
        print(f"round {r['round']:>2}: {mark}  kills={r['kills']}  steps retried={r['steps_retried_after_kill']}"
              + (f"  {r['problems']}" if r["problems"] else ""))
    print(f"\n{len(results) - len(failed)}/{len(results)} rounds passed, {total_kills} kills in total.")
    print(json.dumps({"rounds": len(results), "passed": len(results) - len(failed), "kills": total_kills}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
