#!/usr/bin/env python3
"""SettleBot CLI.

  python main.py run INPUT.json --run-id oct-2026-run1        # verify and post
  python main.py INPUT.json --run-id oct-2026-run1            # same as `run`
  python main.py report --run-id oct-2026-run1                # print the audit report again

Re-running the same command with the same --run-id resumes after a crash: finished steps are
skipped, cached source responses are reused, and nothing is posted or paid twice.
"""
from __future__ import annotations

import argparse
import sys

from settlebot.config import load_config, load_dotenv
from settlebot.report import build_report, print_report, write_report
from settlebot.runner import RunError, Runner
from settlebot.store import Store


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--db", default="state.db", help="SQLite state file (WAL)")
    p.add_argument("--out", default="out", help="folder for report.json and cost_ledger.jsonl")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0].endswith(".json"):
        argv = ["run"] + argv

    ap = argparse.ArgumentParser(prog="settlebot", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="verify, hold and post a batch")
    run.add_argument("input")
    run.add_argument("--run-id", required=True)
    run.add_argument("--fake", action="store_true", help="use fixture sources (tests only, never a real decision)")
    run.add_argument("--slow", type=float, default=0.0, help="fake sources only: delay per call, for kill tests")
    _common(run)

    rep = sub.add_parser("report", help="print the audit report for a run")
    rep.add_argument("--run-id", required=True)
    _common(rep)

    args = ap.parse_args(argv)
    load_dotenv()
    cfg = load_config(args.config)
    store = Store(args.db)

    if args.cmd == "run":
        options = {"out_dir": args.out, "slow": args.slow}
        if args.fake:
            options["sources"] = ["fake"]
        try:
            runner = Runner(store, cfg, args.run_id, args.input, options)
        except RunError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        runner.run()

    report = build_report(store, args.run_id)
    path = write_report(report, args.out)
    print_report(report)
    print(f"report: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
