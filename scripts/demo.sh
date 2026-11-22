#!/usr/bin/env bash
# Screening demo in one command:
#   1. verify + post bank lines with live data, 2. kill mid-run and restart (each action once),
#   3. invoices: fraud/duplicate/lookalike holds, 4. approve + pay once in Razorpay test mode,
#   5. show the cost ledger.
#
#   bash scripts/demo.sh            # live (needs .env)
#   bash scripts/demo.sh --offline  # fixtures + fake payment provider, no network
set -uo pipefail
cd "$(dirname "$0")/.."

MODE=live
[[ "${1:-}" == "--offline" ]] && MODE=offline
RUN="demo-$(date +%H%M%S)"
BANK=tests/fixtures/pdf_example_bank_lines.json
INV=tests/fixtures/invoices_sample.json
EXTRA=()
PAY=()
if [[ $MODE == offline ]]; then
  EXTRA=(--fake --slow 0.8)
  PAY=(--pay-provider fake --fake-remote-db "out/$RUN-remote.db")
fi
mkdir -p out
hr() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

hr "1. Verify and post the bank lines ($MODE)"
python main.py run "$BANK" --run-id "$RUN-bank" "${EXTRA[@]}"

hr "2. Kill on stage: start, kill -9 after 1.5 s, restart with the same run-id"
python main.py run "$BANK" --run-id "$RUN-kill" "${EXTRA[@]}" >/dev/null 2>&1 &
PID=$!
sleep 1.5
kill -9 "$PID" 2>/dev/null && echo "killed pid $PID"
wait "$PID" 2>/dev/null
python main.py run "$BANK" --run-id "$RUN-kill" "${EXTRA[@]}"
python - "$RUN-kill" <<'EOF'
import sqlite3, sys
c = sqlite3.connect("state.db")
run = sys.argv[1]
posted = c.execute("SELECT COUNT(*), COUNT(DISTINCT item_key) FROM journal WHERE run_id=?", (run,)).fetchone()
retried = c.execute("SELECT COALESCE(SUM(attempts-1),0) FROM steps WHERE run_id=?", (run,)).fetchone()[0]
hits = c.execute("SELECT COUNT(*) FROM ledger WHERE run_id=? AND cache_hit=1", (run,)).fetchone()[0]
print(f"journal postings: {posted[0]} (distinct lines {posted[1]}), steps redone after kill: {retried}, "
      f"cached responses reused: {hits}")
EOF

hr "3. Invoices: bank-detail fraud, duplicate, unknown vendor, lookalike domain and name"
python main.py run "$INV" --run-id "$RUN-inv" "${EXTRA[@]}"

hr "4. Approve line 1 (person) and pay exactly once in test mode"
python main.py approve --run-id "$RUN-inv" --line 1 --action PAY --yes --approver "${APPROVER:-finance-controller}"
python main.py run "$INV" --run-id "$RUN-inv" "${EXTRA[@]}" --pay "${PAY[@]}" </dev/null | grep -E "settlement|^  1 |payment error"
echo "-- paying again changes nothing:"
python main.py run "$INV" --run-id "$RUN-inv" "${EXTRA[@]}" --pay "${PAY[@]}" </dev/null >/dev/null
python - "$RUN-inv" <<'EOF2'
import sqlite3, sys
rows = sqlite3.connect("state.db").execute(
    "SELECT receipt, status, provider_id FROM payments WHERE run_id=?", (sys.argv[1],)).fetchall()
print(f"payments recorded: {len(rows)} -> {rows}")
EOF2

hr "5. Cost ledger (last 6 calls of the bank run)"
tail -n 6 "out/$RUN-bank/cost_ledger.jsonl" | python -c "
import json, sys
for l in sys.stdin:
    r = json.loads(l)
    print(f\"{r['provider']:<10} {r['op']:<22} units={r['units']:<6} INR={r['cost_inr']:<8} {r['latency_ms']} ms cache={r['cache_hit']}\")"

hr "Done. Reports: out/$RUN-*/report.json"
