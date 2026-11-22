# SettleBot: payables and bank-line posting that pays each invoice exactly once

**Track:** Buying and paying (ContextCon 05, BITS Pilani: *Services, Replaced*)
**Client:** a mid-size Indian 3PL (records released at kickoff)

## The job, and who pays for it today

The job is accounts-payable verification and settlement. Raw bank-statement lines or vendor invoices come in. For each one, SettleBot:

1. identifies the real payee from live sources;
2. posts it to the right ledger account;
3. holds duplicates and bank-detail fraud;
4. pays only what a person approves, exactly once, even if the process is killed half-way.

Today, mid-size 3PLs send this work to outside firms: accounting BPOs, CA back-offices, and freight audit and payment firms. They charge per invoice or as a share of freight spend. *Prices still to be sourced before the finals; see `finance:` in `config.yaml`.*

## Input

A JSON array (the format from the kickoff kit replaces this once released).

```json
[
  {"date": "2026-10-03", "description": "RAZ*SWIGGY BLR", "amount_inr": 1240},
  {"kind": "invoice", "invoice_no": "INV-101", "vendor_id": "V001", "date": "2026-10-05",
   "description": "VRL Logistics", "amount_inr": 48000,
   "bank_account": {"account_number": "50100123450001", "ifsc": "HDFC0000123", "name": "VRL LOGISTICS LIMITED"}}
]
```

A line without `invoice_no` or `vendor_id` is treated as a bank line.

## Finished result

`out/<run-id>/report.json` gives one row per line, and the same table is printed to the terminal.

| Status | Meaning | Posted? | Paid? |
| --- | --- | --- | --- |
| `VERIFIED` | Legal payee and account backed by a cited source | Yes, once | Invoices, after approval |
| `HELD_DUPLICATE` | Same date, descriptor and amount as an earlier line (invoices: same vendor, invoice number and amount) | Only if a person releases it | No |
| `HELD_FRAUD` | Invoice bank details differ from the vendor master | No | Never from this batch |
| `CANNOT_VERIFY` | No source settles the payee, sources disagree, or a limit was reached; names what is missing | No | No |

Each fact cites the source, the URL, the exact field or passage, the record date where the source gives one, and when it was retrieved. Example from a real run: payee **Swiggy Limited**, from `results[2].snippet`, which reads "Name of the Listed Entity Swiggy Limited…" in a Crustdata web search response, retrieved 22 Nov 2026.

## Pass mark

A run passes only if all of these hold:

- **Every `VERIFIED` fact is sourced:** each one has a citation whose quote appears verbatim in a response stored during the run. No facts come from model memory.
- **The hard cases come out right:**
  - duplicates are never posted or paid;
  - changed bank details are blocked;
  - an unidentifiable payee gives `CANNOT_VERIFY`, not a guess.
- **Restarts are exactly-once:** killing the process at any moment (`kill -9`) and rerunning with the same `--run-id` finishes the batch with every posting and payment happening once.
- **Limits hold:** total cost stays under `limits.max_cost_inr` and active time under `limits.max_time_seconds`. Past a limit, the remaining lines fail closed to `CANNOT_VERIFY`.

## Data sources the service calls

| Source | What we take from it | Mode | Cost logged as |
| --- | --- | --- | --- |
| Crustdata `POST /company/identify` | Brand name and primary domain, used as context only (confidence ≥ 0.9) | Live | credits → INR |
| Crustdata `POST /web/search/live` | The legal name, the passage that states it, and the page date | Live | credits → INR |
| Vendor master (client record, `vendor_master` in config) | Vendor ID, legal name, bank account and IFSC | Client file | — |
| Anthropic Claude API (`llm.model`, default `claude-haiku-5-5`) | **Not a source.** It only proposes which stored result names the payee, or which account fits; code checks every proposal | Live | tokens → INR |
| Razorpay Orders API | Settlement of approved invoices | **Test mode only** (`rzp_test_` keys; live keys are refused) | calls |

## How a line is decided

Six steps, each checkpointed:

1. **Normalize** the descriptor: `RAZ*SWIGGY BLR` becomes merchant `SWIGGY` (processor Razorpay, city BLR).
2. **Dedupe** against earlier lines.
3. **Resolve** the payee. Crustdata identify runs first, then live web search, with results cached per run.
4. **Decide.** A candidate legal name is kept only if:
   - its passage is verbatim in the stored response;
   - the passage contains the name;
   - the passage ties the name to the descriptor, either as a defined term (`Amazon Web Services EMEA SARL ("AWS EMEA")`) or by containing every merchant word (`Swiggy Limited`).

   Two different legal names means `CANNOT_VERIFY`. People, the payment processor and lookalikes never qualify, which is why `PAYTM*SHARMA TRD` ends as cannot verify. The account comes from the chart of accounts in config. Claude only fills gaps the rules leave, and its output passes the same checks.
5. **Bank check** for invoices: the vendor must be in the master, the live legal name must match the master, and the bank details must match.
   - **Lookalikes:** an invoice is held as fraud if it names an imitation of a known vendor under an unknown vendor ID ("VRL Logistlcs"), or if it was sent from a domain that imitates the vendor's domain on file (`vrlgr0up.in` vs `vrlgroup.in`). The reason names the exact edit: a letter dropped, 0 for o, a different ending, the same name with a different legal form, and so on.
   - **Bank lines:** a line that can't be verified but resembles a known payee (`SW1GGY`) says so, and is never matched to that payee.
6. **Post** to the journal, with a unique entry ID, so each line posts once.

## Crash recovery ("kill on stage")

- **State:** SQLite in WAL mode (`state.db`). Each step's output and its local side effects commit in one transaction. A restart skips finished steps and reuses cached source responses, so it pays nothing extra for them.
- **Item keys** come from the input file's hash plus the line number, so two identical lines can never collide.
- **Payments:**
  1. Write `IN_FLIGHT` with receipt `sb_` + 32 hex characters, which stays under Razorpay's 40-character limit.
  2. On restart, look up the receipt at Razorpay first, and adopt the order if it exists.
  3. Create a new order only if none exists.
- **Proof:** `python scripts/kill_test.py --rounds 20` (kills during verification) and `python scripts/kill_test.py --pay --rounds 20` (kills during payment). In development, 20 of 20 rounds passed in each. In the payment test, 8 of the kills landed after the order reached the provider but before it was recorded, and each restart adopted the order.

## Approvals, ethics and privacy

- Nothing irreversible happens without a person. Each pending action shows a **decision card** with the payee, masked bank details, the plain-English reason and the sources. Decisions are stored with approver, time and reason, and survive restarts.
- Approval rules are in config, for example `{action: PAY, min_amount_inr: 100000, approvers: 2}`. The same person approving twice counts once.
- Holds fail closed. A `HELD_FRAUD` line never gets a payment action. The fix is to confirm the change with the vendor through a contact on file, never through the invoice.
- `pii_guard` masks PAN, Aadhaar, phone numbers, email, IFSC, UPI IDs and account numbers before any text goes to Claude. Account numbers in reasons and cards show only the last 4 digits.
- No action is ever taken on a name match alone. Payments need a vendor-master ID and matching bank details.

## Cost and limits

Every external call writes a ledger row with provider, operation, units, INR cost, latency, attempt number and whether it was a cache hit. The ledger is stored in `state.db` and exported to `out/<run-id>/cost_ledger.jsonl`. The guard checks spend and active time before every paid call. Each run also prints a unit-economics line: measured cost per line, price, revenue and margin.

Set `crustdata.inr_per_credit` and `llm.price_inr_per_mtok` to your real prices so the INR figures are exact.

## Screening demo (one command)

```bash
bash scripts/demo.sh            # live: Crustdata + Razorpay test mode (needs .env)
bash scripts/demo.sh --offline  # same flow on fixtures and a fake provider, no network
```

The script runs five steps:

1. Verify and post the bank lines.
2. `kill -9` mid-run, then restart with the same run ID. It shows postings = 2, the steps redone, and the cached responses reused.
3. Invoices: fraud, duplicate, unknown vendor, lookalike domain and lookalike name.
4. Approve line 1 and pay. Paying again changes nothing (payments recorded = 1).
5. The last cost-ledger rows.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # add CRUSTDATA_API_KEY, ANTHROPIC_API_KEY (optional), RAZORPAY_KEY_ID/SECRET (test)

# Verify and post (live data)
python main.py tests/fixtures/pdf_example_bank_lines.json --run-id oct-2026-run1

# Approve pending actions (interactive decision cards), then settle in Razorpay test mode
python main.py approve --run-id oct-2026-run1
python main.py run tests/fixtures/pdf_example_bank_lines.json --run-id oct-2026-run1 --pay

# Re-print the report
python main.py report --run-id oct-2026-run1
```

Tests run offline in about 2 seconds, using fixtures and recorded real responses:

```bash
python -m pytest -q
python scripts/kill_test.py --rounds 20
python scripts/kill_test.py --pay --rounds 20
python scripts/smoke_live.py      # live check of Crustdata, Claude and Razorpay test mode (no side effects)
```

`--fake` runs use fixture sources and are for tests only. They never make a real decision.

## Prior art

| Idea | Where it came from | Licence | What we reused |
| --- | --- | --- | --- |
| Lookalike categories and homoglyph folding | [dnstwist](https://github.com/elceef/dnstwist) | Apache-2.0 | The categories and the confusable-character idea. No code: dnstwist generates permutations forward, while `lookalike.py` compares two values in reverse to name the edit. |
| Per-field evidence (source, passage, date, confidence) | [Parallel Task API "Basis"](https://docs.parallel.ai/task-api/guides/access-research-basis) | Docs | The shape of a citation record |
| Exactly-once steps with idempotency keys | [DBOS Transact](https://github.com/DBOS-project/dbos-transact-py) | MIT | The pattern only (checkpoint each step, key each external action) |
| Approval gate before risky calls | [HumanLayer](https://github.com/humanlayer/humanlayer) | Apache-2.0 | The pattern only (decision stored, resumable after restart) |

## Live-fix cheat sheet

| Change request | Where |
| --- | --- |
| New cost or time limit | `limits.max_cost_inr`, `limits.max_time_seconds` |
| Second approver above ₹X | Add `{action: PAY, min_amount_inr: X, approvers: 2}` to `approvals.rules` |
| New account or mapping | `chart_of_accounts` |
| New processor prefix (e.g. `PHONEPE*`) | `descriptor.processor_prefixes` |
| New output field | `output.extra_fields`, plus the report builder |
| Switch Claude model | `llm.model`, plus `llm.price_inr_per_mtok` |
| New vendor domain on file | `domains` in the vendor master |

## Repository layout

```
main.py                 CLI: run, approve, report
config.yaml             everything a live fix may change
settlebot/
  runner.py             steps, checkpoints, settlement
  store.py              SQLite WAL schema and transactions
  sources/              crustdata.py (identify, web search), fake.py (tests)
  entities.py           legal-name extraction and descriptor corroboration
  grounding.py          quote-in-source check
  llm.py                Claude proposer behind deterministic gates
  vendors.py            vendor master and bank-change hold
  approvals.py          rules, decision cards
  payments.py           Razorpay Orders (test) and reconcile-before-retry
  ledger.py, report.py, pii_guard.py, normalize.py, accounts.py
tests/                  unit tests, recorded Crustdata responses, fixtures
scripts/kill_test.py    random SIGKILL rounds
architecture.mmd        system diagram
data/vendor_master.json fictional placeholder until the kickoff kit
```

## Still to do at kickoff

- Replace the input and output formats, vendor master, chart of accounts and limits with the kit's.
- Set real INR prices per Crustdata credit and per Claude token.
- Source the outsourced benchmark price.
- Commit to the organizer repository.
