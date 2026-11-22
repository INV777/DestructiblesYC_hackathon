"""The step runner. Each line goes through fixed steps; each finished step is checkpointed
with its output, so a restart with the same --run-id skips everything already done."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .accounts import map_account
from .entities import canonical, corroborates
from .grounding import name_in_passage, passage_in_body
from .ledger import Guard, Ledger, LimitReached
from .models import Citation, Decision, LineItem, Status, sha256
from .normalize import fingerprint, normalize_descriptor
from .sources import SourceContext, build_sources
from .sources.base import PermanentError
from .store import Store

STEPS = ["normalize", "dedupe", "resolve", "decide", "post"]


class RunError(Exception):
    pass


def load_items(path: str | Path) -> tuple[list[LineItem], str]:
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    if isinstance(data, dict):  # tolerate {"lines": [...]} / {"items": [...]} wrappers
        data = data.get("lines") or data.get("items") or data.get("bank_lines") or data.get("invoices") or []
    items = [LineItem.from_dict(i + 1, d) for i, d in enumerate(data)]
    return items, sha256(raw.decode("utf-8"))


class Runner:
    def __init__(self, store: Store, cfg: dict[str, Any], run_id: str, input_path: str | Path,
                 options: dict[str, Any] | None = None, decider=None):
        self.store, self.cfg, self.run_id = store, cfg, run_id
        self.options = options or {}
        self.items, self.input_sha = load_items(input_path)

        existing = store.get_run(run_id)
        if existing and existing["input_sha"] != self.input_sha:
            raise RunError(f"run-id '{run_id}' was started with a different input file; use a new run-id")
        store.create_run(run_id, self.input_sha, str(input_path), cfg)
        for it in self.items:
            store.upsert_item(run_id, it.item_key(self.input_sha), it.line_no, it.kind, it.raw)

        self.ledger = Ledger(store, run_id, cfg)
        run = store.get_run(run_id)
        self.t_start = time.monotonic()
        self.guard = Guard(self.ledger, float(cfg["limits"]["max_cost_inr"]),
                           float(cfg["limits"]["max_time_seconds"]),
                           float(run["elapsed_seconds"]), self.t_start)
        self.elapsed_before = float(run["elapsed_seconds"])
        ctx = SourceContext(store, self.ledger, cfg, run_id, self.options)
        names = self.options.get("sources") or cfg.get("sources", {}).get("order", [])
        self.sources = build_sources(names, ctx)
        if decider is None:
            from .llm import make_decider  # lazy: llm imports this module
            decider = make_decider(cfg, self.options)
        self.decider = decider
        self.limit_hit = False

    # ---- main loop ------------------------------------------------------------
    def run(self) -> str:
        for item in self.items:
            key = item.item_key(self.input_sha)
            done: dict[str, Any] = {}
            for step in STEPS:
                out = self.store.step_output(self.run_id, key, step)
                if out is not None:
                    done[step] = out
                    continue
                attempt = self.store.begin_step(self.run_id, key, step)
                out, effects = getattr(self, f"step_{step}")(item, key, done, attempt)
                self.store.finish_step(self.run_id, key, step, out, effects)
                self._save_elapsed()
                done[step] = out
            self.store.export_ledger_jsonl(self.run_id, self.ledger_path())
        status = "LIMIT_REACHED" if self.limit_hit else "DONE"
        self.store.set_run_status(self.run_id, status)
        self.store.export_ledger_jsonl(self.run_id, self.ledger_path())
        return status

    def ledger_path(self) -> Path:
        p = Path(self.options.get("out_dir", "out")) / self.run_id
        p.mkdir(parents=True, exist_ok=True)
        return p / "cost_ledger.jsonl"

    def _save_elapsed(self) -> None:
        total = self.elapsed_before + (time.monotonic() - self.t_start)
        with self.store.tx() as c:
            c.execute("UPDATE runs SET elapsed_seconds=? WHERE run_id=?", (total, self.run_id))

    # ---- steps ----------------------------------------------------------------
    def step_normalize(self, item: LineItem, key: str, done: dict, attempt: int):
        out = normalize_descriptor(item.description, self.cfg)
        out["fingerprint"] = fingerprint(item, out["descriptor_norm"])
        effects = [("UPDATE items SET fingerprint=? WHERE run_id=? AND item_key=?",
                    (out["fingerprint"], self.run_id, key))]
        return out, effects

    def step_dedupe(self, item: LineItem, key: str, done: dict, attempt: int):
        fp = done["normalize"]["fingerprint"]
        row = self.store.one(
            "SELECT line_no FROM items WHERE run_id=? AND fingerprint=? AND line_no < ? ORDER BY line_no LIMIT 1",
            (self.run_id, fp, item.line_no))
        return {"duplicate_of_line": row["line_no"] if row else None}, []

    def step_resolve(self, item: LineItem, key: str, done: dict, attempt: int):
        if done["dedupe"]["duplicate_of_line"] is not None:
            return {"hits": [], "problems": [], "skipped": "duplicate"}, []
        query = {"merchant": done["normalize"]["merchant"], "descriptor": item.description,
                 "city": done["normalize"]["city"], "processor": done["normalize"]["processor"]}
        hits, problems = [], []
        for src in self.sources:
            try:
                self.guard.check(time.monotonic())
            except LimitReached as e:
                self.limit_hit = True
                return {"hits": [h.to_dict() for h in hits], "problems": problems, "limit": str(e),
                        "evidence_ids": query.get("_evidence", [])}, []
            try:
                h, p = src.lookup(query, item_key=key, step="resolve", attempt=attempt)
            except PermanentError as e:
                h, p = [], [f"{src.name}: {e}"]
            hits.extend(h)
            problems.extend(p)
            if any(self._grounded(x.to_dict(), query["merchant"]) for x in h):
                break  # first source that names the payee with a verifiable passage wins
        return {"hits": [h.to_dict() for h in hits], "problems": problems,
                "evidence_ids": query.get("_evidence", [])}, []

    def step_decide(self, item: LineItem, key: str, done: dict, attempt: int):
        dup = done["dedupe"]["duplicate_of_line"]
        res = done["resolve"]
        if dup is not None:
            d = Decision(status=Status.HELD_DUPLICATE, related_line=dup,
                         explanation=(f"Same date, descriptor and amount as line {dup}. "
                                      "Held for approval as a possible duplicate charge."))
        elif res.get("limit"):
            self.limit_hit = True
            d = Decision(status=Status.CANNOT_VERIFY, missing=[res["limit"]],
                         explanation="Stopped before verifying this line because a run limit was reached.")
        else:
            try:
                self.guard.check(time.monotonic())
                d = self.decider.decide(item, done["normalize"], res, self, key, attempt)
            except LimitReached as e:
                self.limit_hit = True
                d = Decision(status=Status.CANNOT_VERIFY, missing=[str(e)],
                             explanation="Stopped before deciding this line because a run limit was reached.")
        effects = [("UPDATE items SET status=?, decision_json=?, updated_at=datetime('now') WHERE run_id=? AND item_key=?",
                    (d.status.value, d.to_json(), self.run_id, key))]
        return d.to_dict(), effects

    def step_post(self, item: LineItem, key: str, done: dict, attempt: int):
        d = Decision.from_dict(done["decide"])
        if d.status != Status.VERIFIED:
            return {"posted": False, "reason": d.status.value}, []
        entry_id = sha256(f"{self.run_id}|{key}|post")[:32]
        effects = [(
            "INSERT OR IGNORE INTO journal (run_id, entry_id, item_key, line_no, date, account_code, account_name,"
            " payee, amount_paise, posted_at) VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))",
            (self.run_id, entry_id, key, item.line_no, item.date, d.account_code, d.account_name, d.payee,
             item.amount_paise),
        )]
        return {"posted": True, "entry_id": entry_id}, effects

    # ---- helpers --------------------------------------------------------------
    def _grounded(self, hit: dict, merchant: str | None = None) -> bool:
        """Passage is in the stored response, names the payee, and ties it to the descriptor."""
        ev = self.store.evidence(hit["evidence_id"])
        if not (ev and hit.get("legal_name") and passage_in_body(hit["passage"], ev["body"])
                and name_in_passage(hit["legal_name"], hit["passage"])):
            return False
        if merchant is None or hit.get("source") == "fake":
            return True
        ident = (hit.get("extra") or {}).get("identify") or {}
        keys = [merchant] + ([ident["name"]] if ident.get("name") else [])
        return any(corroborates(k, hit["legal_name"], hit["passage"]) for k in keys)


def citation_for(h: dict) -> Citation:
    return Citation(source=h["source"], source_url=h["url"], field_or_passage=h["passage"],
                    record_date=h.get("record_date"), retrieved_at=h["retrieved_at"],
                    evidence_id=h["evidence_id"], field=h.get("field"))


def cannot_verify_no_payee(item: LineItem, norm: dict, res: dict) -> Decision:
    missing = [f"No source names a payee for descriptor '{item.description}' "
               f"(searched merchant '{norm['merchant']}')."]
    missing += res.get("problems", [])
    return Decision(status=Status.CANNOT_VERIFY, missing=missing,
                    explanation="No source identifies the payee. Held for a person.")


def grounded_by_name(runner: "Runner", norm: dict, res: dict) -> dict[str, list[dict]]:
    """Grounded hits grouped by canonical legal name."""
    groups: dict[str, list[dict]] = {}
    for h in res.get("hits", []):
        if runner._grounded(h, norm["merchant"]):
            groups.setdefault(canonical(h["legal_name"]), []).append(h)
    return groups


def ambiguous(groups: dict[str, list[dict]]) -> Decision:
    names = sorted({g[0]["legal_name"] for g in groups.values()})
    return Decision(status=Status.CANNOT_VERIFY, citations=[citation_for(g[0]) for g in groups.values()][:3],
                    missing=[f"Sources name more than one possible payee: {'; '.join(names)}."],
                    explanation="More than one legal entity fits this descriptor. Held for a person.")


class RulesDecider:
    """No-LLM decision: one grounded legal name, account mapped by keyword."""

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg

    def decide(self, item: LineItem, norm: dict, res: dict, runner: Runner, key: str, attempt: int) -> Decision:
        groups = grounded_by_name(runner, norm, res)
        if not groups:
            return cannot_verify_no_payee(item, norm, res)
        if len(groups) > 1:
            return ambiguous(groups)
        hits = next(iter(groups.values()))
        h = hits[0]
        cites = [citation_for(x) for x in hits[:2]]
        acct = map_account([norm["merchant"], h["legal_name"], (h.get("extra") or {}).get("category_hint") or "",
                            (h.get("extra") or {}).get("title") or ""], self.cfg)
        if not acct:
            return Decision(status=Status.CANNOT_VERIFY, payee=h["legal_name"], citations=cites,
                            missing=["No chart-of-accounts rule matches this payee."],
                            explanation=f"Payee identified as {h['legal_name']}, but no account rule applies. "
                                        "Held for a person.")
        code, name, kw = acct
        where = h.get("field") or "passage"
        expl = (f"Descriptor '{item.description}' points to merchant '{norm['merchant']}'. "
                f"{h['source']} ({where}) states \"{h['passage'][:160]}\", naming the payee {h['legal_name']}. "
                f"Posted to {name} because '{kw}' matches the chart of accounts.")
        return Decision(status=Status.VERIFIED, payee=h["legal_name"], account_code=code, account_name=name,
                        explanation=expl, citations=cites)
