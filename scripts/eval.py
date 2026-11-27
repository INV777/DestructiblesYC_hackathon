#!/usr/bin/env python3
"""Run the evaluation suite (evals/cases.yaml) through the real pipeline and score it.

  python scripts/eval.py                # print the scorecard; exit 1 on any wrong or unsafe answer
  python scripts/eval.py --write        # also write evals/RESULTS.md and evals/badge.json

+1 correct, 0 for an honest "cannot verify", -2 for any other wrong answer. UNSAFE means SettleBot
said VERIFIED when the right answer was anything else. Offline: fixtures and recorded responses only.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import tempfile
import time
from collections import OrderedDict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from settlebot.config import load_config  # noqa: E402
from settlebot.importer import analyse, read_tables  # noqa: E402
from settlebot.lookalike import core_name  # noqa: E402
from settlebot.report import build_report  # noqa: E402
from settlebot.runner import Runner  # noqa: E402
from settlebot.store import Store  # noqa: E402
from settlebot.vendors import load_vendor_master  # noqa: E402

EVALS = ROOT / "evals"
POINTS = {"correct": 1, "abstained": 0, "wrong": -2}


def merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = merge(out.get(k, {}), v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def judge(exp: dict, got: dict) -> tuple[str, list[str]]:
    """Compare one line. Returns (outcome, reasons the answer is not correct)."""
    problems = []
    if got["status"] != exp["status"]:
        problems.append(f"status {got['status']}, expected {exp['status']}")
    if "payee" in exp and got.get("payee") != exp["payee"]:
        problems.append(f"payee {got.get('payee')!r}, expected {exp['payee']!r}")
    if "account" in exp and not (got.get("account") or "").startswith(exp["account"]):
        problems.append(f"account {got.get('account')!r}, expected {exp['account']}")
    if "related_line" in exp and got.get("related_line") != exp["related_line"]:
        problems.append(f"points to line {got.get('related_line')}, expected {exp['related_line']}")
    if "says" in exp:
        text = " ".join([got.get("explanation") or ""] + list(got.get("missing") or [])).lower()
        if exp["says"].lower() not in text:
            problems.append(f"reason does not mention {exp['says']!r}")
    if not problems:
        return "correct", []
    if got["status"] == "CANNOT_VERIFY" and exp["status"] != "CANNOT_VERIFY":
        return "abstained", problems
    return "wrong", problems


def read_file_case(case: dict, cfg: dict) -> tuple[list[dict], list[str]]:
    """A messy file read with automatic choices only (no person's edits). Returns (lines, problems)."""
    path = ROOT / case["file"]
    known = {core_name(v["legal_name"]): vid for vid, v in load_vendor_master(cfg).items()}
    res = analyse(read_tables(path.read_bytes(), path.name), path.name, known_vendors=known)
    problems = list(res.problems)
    for k, want in (case.get("read") or {}).items():
        got = getattr(res, k)
        if got != want:
            problems.append(f"read {k}={got!r}, expected {want!r}")
    if len(res.lines) != len(case["expect"]):
        problems.append(f"read {len(res.lines)} lines, expected {len(case['expect'])}")
    return res.lines, problems


def run_case(case: dict, base_cfg: dict, tmp: Path) -> list[dict]:
    cfg = merge(base_cfg, case.get("config", {}))
    cfg["vendor_master"] = str(ROOT / case["vendor_master"]) if case.get("vendor_master") \
        else str(EVALS / "vendor_master.json")
    read_problems: list[str] = []
    lines = case.get("lines")
    if case.get("file"):
        lines, read_problems = read_file_case(case, cfg)
    if read_problems:  # a file read wrongly fails every line of its case
        return [{"case": case["id"], "category": case["category"], "line": i, "expected": e["status"], "got": "-",
                 "outcome": "wrong", "problems": read_problems, "unsafe": False, "known_gap": case.get("known_gap")}
                for i, e in enumerate(case["expect"], start=1)]
    batch = tmp / f"{case['id']}.json"
    batch.write_text(json.dumps(lines, ensure_ascii=False), encoding="utf-8")
    options = {"no_llm": True, "out_dir": str(tmp / "out")}
    if case.get("mode") == "replay":
        options["replay"] = True
    else:
        fixture = ROOT / case["fixture"] if case.get("fixture") else EVALS / "sources.json"
        options.update({"sources": ["fake"], "fake_fixture": str(fixture)})
    store = Store(tmp / f"{case['id']}.db")
    try:
        Runner(store, cfg, case["id"], batch, options).run()
        got = {r["line"]: r for r in build_report(store, case["id"])["lines"]}
    finally:
        store.close()
    rows = []
    for i, exp in enumerate(case["expect"], start=1):
        outcome, problems = judge(exp, got[i])
        rows.append({
            "case": case["id"], "category": case["category"], "line": i, "expected": exp["status"],
            "got": got[i]["status"], "outcome": outcome, "problems": problems,
            "unsafe": got[i]["status"] == "VERIFIED" and exp["status"] != "VERIFIED",
            "known_gap": case.get("known_gap"),
        })
    return rows


def scorecard(rows: list[dict], seconds: float) -> tuple[str, dict]:
    cats: "OrderedDict[str, dict]" = OrderedDict()
    for r in rows:
        c = cats.setdefault(r["category"], {"cases": set(), "lines": 0, "correct": 0, "abstained": 0, "wrong": 0,
                                            "unsafe": 0, "score": 0})
        c["cases"].add(r["case"])
        c["lines"] += 1
        c[r["outcome"]] += 1
        c["unsafe"] += r["unsafe"]
        c["score"] += POINTS[r["outcome"]]
    tot = {k: sum(c[k] for c in cats.values()) for k in ("lines", "correct", "abstained", "wrong", "unsafe", "score")}
    tot["cases"] = len({r["case"] for r in rows})
    lines = ["| Category | Cases | Lines | Correct | Cannot verify | Wrong | Unsafe | Score |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for name, c in cats.items():
        lines.append(f"| {name} | {len(c['cases'])} | {c['lines']} | {c['correct']} | {c['abstained']} | {c['wrong']} | "
                     f"{c['unsafe']} | {c['score']} of {c['lines']} |")
    lines.append(f"| **Total** | **{tot['cases']}** | **{tot['lines']}** | **{tot['correct']}** | **{tot['abstained']}** | "
                 f"**{tot['wrong']}** | **{tot['unsafe']}** | **{tot['score']} of {tot['lines']}** |")
    misses = [r for r in rows if r["outcome"] != "correct"]
    if misses:
        lines += ["", "Lines not answered correctly:", ""]
        for r in misses:
            gap = f" Known gap: {r['known_gap']}" if r["known_gap"] else ""
            lines.append(f"- `{r['case']}` line {r['line']}: {'; '.join(r['problems'])}.{gap}")
    lines += ["", f"{tot['lines']} lines in {seconds:.1f} s, offline (fixtures and recorded Crustdata responses)."]
    return "\n".join(lines), tot


def badge(tot: dict) -> dict:
    """The README's evaluation badge (a shields.io endpoint), so the number shown is the number measured."""
    clean = tot["wrong"] == 0 and tot["unsafe"] == 0
    return {"schemaVersion": 1, "label": "evaluation",
            "message": f"{tot['score']} of {tot['lines']} right, {tot['unsafe']} unsafe",
            "color": "2e7d32" if clean else "b3261e"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help="write evals/RESULTS.md and the README badge")
    ap.add_argument("--case", action="append", help="run only these case ids")
    args = ap.parse_args(argv)

    cases = yaml.safe_load((EVALS / "cases.yaml").read_text(encoding="utf-8"))
    if args.case:
        cases = [c for c in cases if c["id"] in args.case]
    base = load_config(ROOT / "config.yaml")
    t0 = time.monotonic()
    rows: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="settlebot-eval-", ignore_cleanup_errors=True) as tmp:
        for case in cases:
            rows += run_case(case, base, Path(tmp))
    table, tot = scorecard(rows, time.monotonic() - t0)
    print(table)
    if args.write:
        head = ("# Evaluation results\n\nGenerated by `python scripts/eval.py --write` from "
                "[`cases.yaml`](cases.yaml). Scoring: +1 correct, 0 for an honest \"cannot verify\", "
                "-2 for any other wrong answer. Unsafe: SettleBot said VERIFIED when the right answer was "
                "anything else.\n\n")
        (EVALS / "RESULTS.md").write_text(head + table + "\n", encoding="utf-8")
        (EVALS / "badge.json").write_text(json.dumps(badge(tot), indent=1) + "\n", encoding="utf-8")
    elif not args.case and (EVALS / "badge.json").exists():
        shown = json.loads((EVALS / "badge.json").read_text(encoding="utf-8"))
        if shown != badge(tot):
            print(f"\nThe README badge says \"{shown['message']}\" but this run measured "
                  f"\"{badge(tot)['message']}\". Run python scripts/eval.py --write and commit the result.")
            return 1
    gated = [r for r in rows if not r["known_gap"] and (r["outcome"] == "wrong" or r["unsafe"])]
    return 1 if gated else 0


if __name__ == "__main__":
    sys.exit(main())
