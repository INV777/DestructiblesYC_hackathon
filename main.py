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
from settlebot.report import build_report, print_report, print_unit_economics, unit_economics, write_report
import getpass
import json

from settlebot import approvals
from settlebot.payments import PaymentError, build_provider
from settlebot.runner import RunError, Runner, settle_run
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
    run.add_argument("--no-llm", action="store_true", help="rules only, no Claude calls")
    run.add_argument("--pay", action="store_true", help="after verifying, settle approved actions (test mode)")
    run.add_argument("--pay-provider", default=None, help="razorpay_orders (default) | fake (tests only)")
    run.add_argument("--fake-remote-db", default="fake_provider.db", help="fake provider's own state (tests only)")
    run.add_argument("--approver", default=None, help="name recorded with your approvals (default: OS user)")
    _common(run)

    apr = sub.add_parser("approve", help="approve or reject pending actions (decision cards)")
    apr.add_argument("--run-id", required=True)
    apr.add_argument("--approver", default=None)
    apr.add_argument("--line", type=int, help="non-interactive: the line to decide")
    apr.add_argument("--action", choices=["PAY", "RELEASE_HOLD"], help="non-interactive: the action")
    g = apr.add_mutually_exclusive_group()
    g.add_argument("--yes", action="store_true")
    g.add_argument("--no", action="store_true")
    apr.add_argument("--reason", default=None)
    _common(apr)

    rep = sub.add_parser("report", help="print the audit report for a run")
    rep.add_argument("--run-id", required=True)
    _common(rep)

    args = ap.parse_args(argv)
    load_dotenv()
    cfg = load_config(args.config)
    store = Store(args.db)

    if args.cmd == "run":
        options = {"out_dir": args.out, "slow": args.slow, "no_llm": args.no_llm}
        if args.fake:
            options["sources"] = ["fake"]
            options["no_llm"] = True
        try:
            runner = Runner(store, cfg, args.run_id, args.input, options)
        except RunError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        runner.run()
        if args.pay:
            options.update({"pay_provider": args.pay_provider, "fake_remote_db": args.fake_remote_db})
            try:
                provider = build_provider(cfg, options)
            except PaymentError as e:
                print(f"payment error: {e}", file=sys.stderr)
                return 3
            approver = args.approver or getpass.getuser()
            ask = _ask if sys.stdin.isatty() else None
            counts = settle_run(runner, provider, ask=ask, approver=approver)
            print(f"settlement: {counts}")
            if counts["waiting"]:
                print(f"waiting for approval: python main.py approve --run-id {args.run_id}")

    if args.cmd == "approve":
        approver = args.approver or getpass.getuser()
        pending = approvals.pending_actions(store, cfg, args.run_id)
        if args.line is not None:
            match = [p for p in pending if p["item"]["line_no"] == args.line and (not args.action or p["action"] == args.action)]
            if not match:
                print(f"nothing pending for line {args.line}", file=sys.stderr)
                return 4
            if not (args.yes or args.no):
                print("pass --yes or --no", file=sys.stderr)
                return 4
            p = match[0]
            approvals.record(store, args.run_id, p["item"]["item_key"], p["action"], approver, args.yes, args.reason)
            print(f"{'approved' if args.yes else 'rejected'}: line {args.line} {p['action']} by {approver}")
        else:
            for p in pending:
                yes, reason = _ask(approvals.decision_card(p))
                approvals.record(store, args.run_id, p["item"]["item_key"], p["action"], approver, yes, reason)
            print(f"{len(pending)} decision(s) recorded by {approver}. Run again with --pay to settle.")

    report = build_report(store, args.run_id)
    report["unit_economics"] = unit_economics(report, cfg)
    path = write_report(report, args.out)
    print_report(report)
    print_unit_economics(report["unit_economics"])
    print(f"report: {path}")
    return 0


def _ask(card: str) -> tuple[bool, str | None]:
    print(card)
    yes = input("Approve this action? [y/N] ").strip().lower() in ("y", "yes")
    reason = None if yes else (input("Reason (optional): ").strip() or None)
    return yes, reason


if __name__ == "__main__":
    sys.exit(main())
