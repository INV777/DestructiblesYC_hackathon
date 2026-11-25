#!/usr/bin/env python3
"""Save the Crustdata responses of a live run as a replay cassette.

  python scripts/export_cassette.py --db state.db --run-id live-1 --out data/replay/live-1.json

Replay it later with no key and no cost:

  python main.py run batch.json --run-id demo --replay data/replay/live-1.json

Only Crustdata responses are exported (they are public company data). Your batch, bank details and
approvals stay in state.db.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from settlebot.store import Store  # noqa: E402

PATHS = {"crustdata_identify": ("/company/identify", "names="), "crustdata_web": ("/web/search/live", "q=")}


def export(store: Store, run_id: str) -> dict:
    rows = store.q("SELECT source, query_key, body, retrieved_at FROM evidence WHERE run_id=? AND source IN (?, ?) "
                   "ORDER BY retrieved_at", (run_id, *PATHS))
    if not rows:
        raise SystemExit(f"no Crustdata responses stored for run '{run_id}'")
    out: dict = {
        "_comment": f"Real Crustdata API responses from run '{run_id}', exported for replay.",
        "_recorded_at": min(r["retrieved_at"] for r in rows),
    }
    for r in rows:
        path, prefix = PATHS[r["source"]]
        key = r["query_key"][len(prefix):] if r["query_key"].startswith(prefix) else r["query_key"]
        out.setdefault(path, {})[key.upper()] = json.loads(r["body"])
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="state.db")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    cassette = export(Store(args.db), args.run_id)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cassette, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    n = sum(len(v) for k, v in cassette.items() if not k.startswith("_"))
    print(f"wrote {n} responses to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
