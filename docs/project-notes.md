# Project notes

[Back to the README](../README.md)

- [History](#history)
- [After the hackathon](#after-the-hackathon)
- [What we tried](#what-we-tried)
- [Why this way](#why-this-way)
- [Prior art](#prior-art)

## History

Built by team DestructiblesYC at ContextCon 05 (BITS Pilani, track *Buying and paying*, theme *Services, Replaced*) on 22 Nov 2026 for a mid-size Indian 3PL. The submitted version is the [`Hackathon` release](https://github.com/INV777/Settlebot/releases/tag/Hackathon); everything since is listed below.

## After the hackathon

**Done**

- **Claude calls are priced in rupees again.** A broken price block in `config.yaml` had logged every Claude call at ₹0, so the cost limit ignored model spend. Prices are now ₹9.33 in / ₹46.67 out per million tokens (USD 0.10 / 0.50 for `claude-haiku-5-5` × RBI reference rate 93.3461 on 19 Nov 2026), with a regression test.
- **Duplicate invoices are explained correctly.** The hold reason now says "same vendor, invoice number and amount" for invoices, and keeps "same date, descriptor and amount" for bank lines.
- **Payment runs no longer hang in Windows scripts.** With input redirected from `NUL`, Windows reports a terminal, so `run --pay` waited on a prompt nobody could answer and crashed. SettleBot now checks for a real console, and an unanswered prompt always leaves the action pending. CI found this on its first run.
- **CI** runs the tests and both kill tests (verification and payment) on Linux and Windows for every push and pull request.
- **Web console** (`python main.py console`). Start a run, watch each line's checks, crash the worker and resume it, approve or reject, pay approved invoices, and read the cost ledger. Each run is its own `main.py run` process, so "Crash the worker" is a real kill. "Show the stored response" opens the raw API answer with the cited field highlighted. See [Run it](running.md#the-web-console).
- **Replay mode** (`--replay`). Reruns real recorded Crustdata responses with no key and no network. Ledger rows say `crustdata-replay` at ₹0, citations keep the original fetch time, and the report estimates what the same calls cost live. `scripts/export_cassette.py` turns any live run into a replay.
- **Fixed: a second run could pay for the same call twice after a crash.** Stored responses were keyed without the run, so a second run with an identical response lost its cache row, and a crash in that run would have repeated the call. Keys now include the run; the regression test fails on the old code.
- **Evaluation suite** with a score in CI ([how it is scored](how-it-works.md#evaluation-suite)). Its first run found two problems, both fixed with regression tests: an invoice re-sent with the same number and a **different amount** was VERIFIED and payable (it is now held for a person, with both amounts in the reason), and an invoice under a new vendor ID with a known vendor's exact name was held without saying so (names are now compared without any legal form).
- **Reads any bank or accounts export, for ₹0.** Upload a statement or a list of bills as it comes, preview what was read, fix any column from a dropdown, then run ([how it reads a file](running.md#what-you-can-give-it)). Offline rules only, no AI. Eight invented messy files are scored in CI. In the public demo, runs from your own file open only in the browser that started them, are deleted after an hour, and the uploaded file is not kept once the run starts.
- **Two approvers from ₹1,00,000.** A second `PAY` rule in `config.yaml`; the comment there now matches the code (the highest matching threshold decides).

**Still open**

- Replace the fictional vendor master (`data/vendor_master.json`), chart of accounts and limits with a real client's records.
- Set the real INR price per Crustdata credit (`crustdata.inr_per_credit` is still a volume-tier placeholder of ₹0.93).

**Planned**

- **Match on registry IDs, not names.** Use GSTIN, PAN and CIN as the join key between invoice, vendor master and live sources (the vendor master already has a `gstin` field).
- **Bank-account name check.** Confirm that the account holder's name matches the vendor before the first payment to new bank details, using a provider's account-validation API.
- **Continuous re-checks.** Re-verify vendors on a schedule with Crustdata Watch, so a status change raises a hold before the next payment run.
- **Outcome billing.** Turn the per-run unit-economics line into actual metered billing: charge only for lines finished without a person.
- **Production payments.** Move from Razorpay test mode to live settlement, behind the same approval rules and reconcile-before-retry logic.

## What we tried

| We tried | What happened | What we do now |
| --- | --- | --- |
| `temperature: 0` for repeatable Claude answers | The live smoke test showed the current Anthropic SDK does not take a temperature argument | Determinism comes from forced tool calls, per-run caching of every answer, and code checks on every proposal |
| Letting Claude pick the ledger account from its own knowledge | Its answer was not tied to any source | Claude now gets the cited passages and classifies from those (live: Swiggy → 6110 Staff meals) |
| Taking the legal name from page titles | Titles carry brand names and marketing text, not the legal entity | Sentences that state a legal name ("Name of the Listed Entity…") rank above titles |
| Using Crustdata company identify as the payee source | It gives a brand and a domain, not the legal entity that gets paid | Identify is context only (confidence ≥ 0.9); the legal name comes from live web search, with the passage |
| Invoice duplicates keyed on date | A re-sent invoice with a new date would slip through | Key on vendor + normalized invoice number + amount (`INV-0101` = `INV-101`, `INV-1001` stays distinct) |
| Lookalike detection by generating permutations, as dnstwist does | A forward list can say "suspicious" but not why | `lookalike.py` compares the two values in reverse and names the exact edit, so the hold reason is readable |
| Entity rules with a fixed list of suffixes | Missed foreign forms such as Sdn Bhd, Pty Ltd and S.p.A. | Added them; names the rules still miss fall to Claude behind the same checks |
| `SIGKILL` in the kill test and a bash-only demo | Neither worked on a Windows laptop | `Popen.kill()` (TerminateProcess on Windows), a Python demo for PowerShell, LF endings via `.gitattributes`, console output safe for non-ASCII text |
| Leaving the outsourced benchmark blank until sourced | Margin had nothing to compare against | Sourced ₹50 per invoice (low end of Pazy's ₹50–80) and the ₹120 in-house figure from Tally |

We also added `tests/test_no_secrets.py`, which fails the suite if an API key appears in any tracked file.

## Why this way

- **Claude proposes; code decides.** In payables, a wrong payee is money gone. So no fact comes from model memory: Claude only points at text already stored from a live source, and code checks that the quote is verbatim, names the entity and matches the descriptor. With no Anthropic key, the service still runs on rules alone and fails closed.
- **"Cannot verify" beats a guess.** An outsourced clerk who isn't sure asks; SettleBot does the same. Ambiguous sources, unknown payees and limit breaches all land in `CANNOT_VERIFY` with the missing piece named, and are not billed.
- **Never pay on a name alone.** Payment needs a vendor-master ID, a matching live legal name and matching bank details. Bank-detail changes and lookalikes are the classic invoice-fraud routes, so they hold by default.
- **Exactly-once over "usually once".** A crash between "order sent" and "order recorded" is exactly when double payments happen. Deterministic receipts plus look-up-before-create make every retry safe, and the kill tests prove it under random kills.
- **SQLite instead of a workflow engine.** One file, no extra services, and WAL transactions give us checkpointed steps with the same guarantees we needed from DBOS-style durable execution, on a laptop.
- **Everything a judge might change lives in `config.yaml`.** Limits, approval rules, accounts, prefixes and prices are one-line live fixes, not code changes.
- **Priced on outcomes.** We charge only for lines finished without a person, so our margin depends on getting lines right, not on touching more of them.
- **Haiku by default.** Claude is called only for gaps the rules leave, so a small, cheap model is enough, and every token is in the cost ledger.

## Prior art

| Idea | Where it came from | Licence | What we reused |
| --- | --- | --- | --- |
| Lookalike categories and homoglyph folding | [dnstwist](https://github.com/elceef/dnstwist) | Apache-2.0 | The categories and the confusable-character idea. No code: dnstwist generates permutations forward, while `lookalike.py` compares two values in reverse to name the edit |
| Per-field evidence (source, passage, date, confidence) | [Parallel Task API "Basis"](https://docs.parallel.ai/task-api/guides/access-research-basis) | Docs | The shape of a citation record |
| Exactly-once steps with idempotency keys | [DBOS Transact](https://github.com/DBOS-project/dbos-transact-py) | MIT | The pattern only (checkpoint each step, key each external action) |
| Approval gate before risky calls | [HumanLayer](https://github.com/humanlayer/humanlayer) | Apache-2.0 | The pattern only (decision stored, resumable after restart) |
