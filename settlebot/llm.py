"""Claude as a proposer, never as a source.

Claude sees only masked search results already stored as evidence. It may propose
(a) which result states the payee's legal name, with an exact quote, and (b) an account
code from the chart of accounts. Code then checks the quote against the stored response
and the descriptor; anything that fails becomes cannot verify. No key -> rules only.
"""
from __future__ import annotations

import json
import time
from typing import Any

from .accounts import account_by_code
from .config import env
from .entities import corroborates
from .grounding import name_in_passage
from .models import Decision, LineItem, Status, sha256
from .pii_guard import mask
from .runner import (RulesDecider, ambiguous, cannot_verify_no_payee, citation_for, grounded_by_name)
from .store import now_iso

EXTRACT_TOOL = {
    "name": "record_payee",
    "description": "Record the legal entity named in the search results, or that none is named.",
    "input_schema": {
        "type": "object",
        "properties": {
            "found": {"type": "boolean", "description": "true only if a result states the merchant's legal name"},
            "result_index": {"type": "integer", "description": "index of the result that states it"},
            "field": {"type": "string", "enum": ["snippet", "title"]},
            "legal_name": {"type": "string"},
            "quote": {"type": "string", "description": "exact, unedited substring of that field containing the legal name"},
            "reason": {"type": "string", "description": "one sentence on why this entity is the merchant"},
        },
        "required": ["found", "reason"],
    },
}

ACCOUNT_TOOL = {
    "name": "choose_account",
    "description": "Choose the ledger account for a verified payee from the chart of accounts.",
    "input_schema": {
        "type": "object",
        "properties": {
            "account_code": {"type": "string", "description": "a code from the chart, or 9999 if none fits"},
            "reason": {"type": "string", "description": "one sentence a finance controller would accept"},
        },
        "required": ["account_code", "reason"],
    },
}

SYSTEM = ("You help an accounts-payable agent. Use ONLY the text you are given. Never use your own "
          "knowledge of companies. If the text does not state the legal name, say found=false. "
          "Quotes must be copied exactly, character for character.")


class ClaudeUnavailable(Exception):
    pass


class Claude:
    def __init__(self, cfg: dict[str, Any], runner):
        self.cfg, self.runner = cfg, runner
        lcfg = cfg.get("llm", {})
        self.model = lcfg.get("model", "claude-haiku-5-5")
        self.max_tokens = int(lcfg.get("max_tokens", 1024))
        self.temperature = float(lcfg.get("temperature", 0.0))
        key = env("ANTHROPIC_API_KEY")
        if not key:
            raise ClaudeUnavailable("ANTHROPIC_API_KEY is not set")
        import anthropic
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=key, max_retries=0,
                                          timeout=float(cfg.get("sources", {}).get("timeout_seconds", 20)))

    def call_tool(self, tool: dict, prompt: str, *, item_key: str, step: str, attempt: int) -> dict:
        """One forced tool call, cached per run so a restart never pays twice for the same prompt."""
        store, ledger, run_id = self.runner.store, self.runner.ledger, self.runner.run_id
        qkey = sha256(f"{self.model}|{tool['name']}|{prompt}")[:32]
        cached = store.cached_evidence(run_id, "claude", qkey)
        if cached is not None:
            ledger.record(provider="anthropic", op=tool["name"], ok=True, cache_hit=True,
                          item_key=item_key, step=step, attempt=attempt)
            return json.loads(cached["body"])["input"]
        retries = int(self.cfg.get("sources", {}).get("retries", 2))
        for i in range(retries + 1):
            t0 = time.monotonic()
            try:
                msg = self.client.messages.create(
                    model=self.model, max_tokens=self.max_tokens, temperature=self.temperature, system=SYSTEM,
                    tools=[tool], tool_choice={"type": "tool", "name": tool["name"]},
                    messages=[{"role": "user", "content": prompt}])
            except (self._anthropic.RateLimitError, self._anthropic.APIConnectionError,
                    self._anthropic.InternalServerError, self._anthropic.APITimeoutError) as e:
                ledger.record(provider="anthropic", op=tool["name"], ok=False, item_key=item_key, step=step,
                              attempt=attempt, latency_ms=int((time.monotonic() - t0) * 1000),
                              detail=f"retry {i}: {type(e).__name__}")
                time.sleep(1.5 * (i + 1))
                continue
            except self._anthropic.APIError as e:
                ledger.record(provider="anthropic", op=tool["name"], ok=False, item_key=item_key, step=step,
                              attempt=attempt, detail=type(e).__name__)
                raise ClaudeUnavailable(str(e)) from e
            ms = int((time.monotonic() - t0) * 1000)
            u = msg.usage
            ledger.record(provider="anthropic", op=tool["name"], ok=True,
                          units=u.input_tokens + u.output_tokens, unit_type="tokens",
                          cost_inr=ledger.llm_inr(u.input_tokens, u.output_tokens), latency_ms=ms,
                          item_key=item_key, step=step, attempt=attempt,
                          detail=f"in={u.input_tokens} out={u.output_tokens} model={self.model}")
            block = next((b for b in msg.content if getattr(b, "type", "") == "tool_use"), None)
            out = {"input": dict(block.input) if block else {}, "model": self.model}
            body = json.dumps(out, ensure_ascii=False, sort_keys=True)
            store.save_evidence({"evidence_id": sha256(f"claude|{qkey}|{body}")[:24], "run_id": run_id,
                                 "source": "claude", "query_key": qkey, "url": f"anthropic:{self.model}",
                                 "retrieved_at": now_iso(), "record_date": None, "body_sha256": sha256(body),
                                 "body": body})
            return out["input"]
        raise ClaudeUnavailable("Claude did not answer after retries")


class ClaudeDecider(RulesDecider):
    """Deterministic gates from RulesDecider, with Claude filling two gaps."""

    def decide(self, item: LineItem, norm: dict, res: dict, runner, key: str, attempt: int) -> Decision:
        try:
            claude = Claude(self.cfg, runner)
        except ClaudeUnavailable:
            return super().decide(item, norm, res, runner, key, attempt)

        groups = grounded_by_name(runner, norm, res)
        if len(groups) > 1:
            return ambiguous(groups)
        if not groups:
            hit = self._extract(claude, item, norm, res, runner, key, attempt)
            if hit is None:
                d = cannot_verify_no_payee(item, norm, res)
                d.missing.append("Claude found no result that states the legal name (or its quote failed the check).")
                return d
            groups = {"_": [hit]}

        d = super().decide(item, norm, {"hits": next(iter(groups.values())), "problems": []}, runner, key, attempt)
        if d.status == Status.CANNOT_VERIFY and d.payee and "No chart-of-accounts rule" in " ".join(d.missing):
            return self._choose_account(claude, item, norm, d, key, attempt)
        return d

    # ---- Claude proposes a legal name; code checks it ---------------------------
    def _extract(self, claude: Claude, item, norm, res, runner, key, attempt) -> dict | None:
        results = []
        for ev_id in res.get("evidence_ids", []):
            ev = runner.store.evidence(ev_id)
            if not ev or ev["source"] != "crustdata_web":
                continue
            data = json.loads(ev["body"])
            for i, r in enumerate(data.get("results", []) if isinstance(data, dict) else []):
                results.append((ev, i, r))
        if not results:
            return None
        listing = "\n".join(f"[{n}] title: {mask(r.get('title') or '')}\n    snippet: {mask(r.get('snippet') or '')}"
                            for n, (_, _, r) in enumerate(results))
        prompt = (f"Bank descriptor: {mask(item.description)}\nMerchant words: {norm['merchant']}\n"
                  f"Payment processor (NOT the payee): {norm.get('processor') or 'none'}\n\n"
                  f"Search results:\n{listing}\n\nWhich result states the legal name of the merchant itself?")
        out = claude.call_tool(EXTRACT_TOOL, prompt, item_key=key, step="decide", attempt=attempt)
        if not out.get("found"):
            return None
        try:
            ev, idx, r = results[int(out["result_index"])]
        except (KeyError, ValueError, IndexError, TypeError):
            return None
        fld = out.get("field") if out.get("field") in ("snippet", "title") else "snippet"
        text, name, quote = r.get(fld) or "", (out.get("legal_name") or "").strip(), (out.get("quote") or "").strip()
        # The three checks that keep Claude honest: the quote is really there, it names the
        # entity, and it ties the entity to this descriptor.
        if not (quote and quote in text and name_in_passage(name, quote) and corroborates(norm["merchant"], name, quote)):
            return None
        hit = {"source": "crustdata_web", "url": r.get("url") or ev["url"], "legal_name": name, "passage": quote,
               "field": f"results[{idx}].{fld}", "record_date": None, "retrieved_at": ev["retrieved_at"],
               "evidence_id": ev["evidence_id"], "extra": {"title": r.get("title"), "proposed_by": claude.model,
                                                           "reason": out.get("reason")}}
        return hit if runner._grounded(hit, norm["merchant"]) else None

    # ---- Claude picks an account; code checks the code exists -------------------
    def _choose_account(self, claude: Claude, item, norm, d: Decision, key, attempt) -> Decision:
        chart = "\n".join(f"{a['code']}: {a['name']}" for a in self.cfg.get("chart_of_accounts", []))
        prompt = (f"Payee: {d.payee}\nBank descriptor: {mask(item.description)}\nAmount: INR {item.amount_inr:,.2f}\n"
                  f"Chart of accounts:\n{chart}\n\nChoose the account.")
        out = claude.call_tool(ACCOUNT_TOOL, prompt, item_key=key, step="decide", attempt=attempt)
        acct = account_by_code(str(out.get("account_code", "")), self.cfg)
        if not acct or acct[0] == "9999":
            d.missing.append("Claude found no fitting account either.")
            return d
        d.status, d.account_code, d.account_name = Status.VERIFIED, acct[0], acct[1]
        d.missing = []
        d.explanation = (f"Payee {d.payee} is backed by the cited source. Account {acct[1]} chosen by "
                         f"{claude.model} from the chart of accounts: {out.get('reason', '').strip()}")
        return d


def make_decider(cfg: dict[str, Any], options: dict[str, Any]):
    if options.get("no_llm") or cfg.get("llm", {}).get("provider") != "anthropic" or not env("ANTHROPIC_API_KEY"):
        return RulesDecider(cfg)
    return ClaudeDecider(cfg)

