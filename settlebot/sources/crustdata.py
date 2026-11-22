"""Crustdata adapters: company identify (brand + domain) and live web search (legal name).

Docs: POST /company/identify, POST /web/search/live, Bearer key + x-api-version header.
Every call is cached per run, retried on 429/5xx/timeouts, and written to the cost ledger.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

import requests

from ..config import env
from ..entities import canonical, defined_term_names, token_names
from .base import Fetched, PermanentError, Source, SourceHit, TransientError

DEFAULT_CREDITS = {"/company/identify": 0.2, "/web/search/live": 1.0}
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                       "nov", "dec"], start=1)}


class CrustdataClient:
    def __init__(self, cfg: dict[str, Any]):
        c = cfg.get("crustdata", {})
        self.base = c.get("base_url", "https://api.crustdata.com").rstrip("/")
        self.version = c.get("api_version", "2025-11-01")
        self.timeout = float(cfg.get("sources", {}).get("timeout_seconds", 20))
        self.key = env("CRUSTDATA_API_KEY")

    def post(self, path: str, body: dict[str, Any]) -> tuple[Any, float]:
        if not self.key:
            raise PermanentError("CRUSTDATA_API_KEY is not set")
        try:
            r = requests.post(self.base + path, json=body, timeout=self.timeout, headers={
                "authorization": f"Bearer {self.key}", "content-type": "application/json",
                "x-api-version": self.version})
        except (requests.Timeout, requests.ConnectionError) as e:
            raise TransientError(f"{path}: {type(e).__name__}") from e
        if r.status_code == 429 or r.status_code >= 500:
            raise TransientError(f"{path}: HTTP {r.status_code}")
        if r.status_code >= 400:
            raise PermanentError(f"{path}: HTTP {r.status_code} {r.text[:200]}")
        credits = r.headers.get("x-credits-used") or r.headers.get("X-Credits-Used")
        return r.json(), float(credits) if credits else DEFAULT_CREDITS.get(path, 0.0)


def _snippet_date(snippet: str) -> str | None:
    """Search snippets often start with the page date: 'Jan 8, 2025 —' or '26 Feb 2024 —'."""
    m = re.match(r"\s*([A-Z][a-z]{2})\w*\s+(\d{1,2}),\s+(\d{4})\s+—", snippet) or None
    if m:
        mon, day, year = m.group(1).lower(), int(m.group(2)), int(m.group(3))
    else:
        m = re.match(r"\s*(\d{1,2})\s+([A-Z][a-z]{2})\w*\s+(\d{4})\s+—", snippet)
        if not m:
            return None
        day, mon, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    if mon not in _MONTHS:
        return None
    return f"{year:04d}-{_MONTHS[mon]:02d}-{day:02d}"


_LEGAL_WORDS = ("legal name", "listed entity", "registered", "incorporated", "cin", "lei", "gstin",
                "company limited", "private limited", "sarl", "marketplace operator", "is the")


def _legal_signal(text: str) -> int:
    """How strongly a sentence reads as a statement of the legal entity's name."""
    t = text.casefold()
    return sum(w in t for w in _LEGAL_WORDS)


def _clean_snippet(s: str) -> str:
    return re.sub(r"\s*(?:\.\.\.)?Read more\s*$", "", s or "").strip()


class _CrustdataSource(Source):
    provider = "crustdata"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.client = CrustdataClient(ctx.cfg)

    def _call(self, path: str, body: dict[str, Any]):
        def call() -> Fetched:
            data, credits = self.client.post(path, body)
            return Fetched(body=json.dumps(data, ensure_ascii=False, sort_keys=True), url=self.client.base + path,
                           record_date=None, units=credits, unit_type="credits",
                           cost_inr=self.ctx.ledger.crustdata_inr(credits))
        return call


class CrustdataIdentify(_CrustdataSource):
    """Brand name and primary domain. Gives context, never a legal name on its own."""
    name = "crustdata_identify"

    def lookup(self, query, *, item_key, step, attempt):
        merchant = query["merchant"]
        body = {"names": [merchant]}
        ev = self.fetch(f"names={merchant.upper()}", self._call("/company/identify", body),
                        item_key=item_key, step=step, attempt=attempt, op="company_identify")
        data = json.loads(ev["body"])
        best = None
        for block in data if isinstance(data, list) else []:
            for i, m in enumerate(block.get("matches", [])):
                score = float(m.get("confidence_score") or 0)
                info = (m.get("company_data") or {}).get("basic_info") or {}
                if score >= 0.9 and info.get("name") and (best is None or score > best[0]):
                    best = (score, i, info)
        if best is None:
            return [], [f"crustdata identify: no confident company match for '{merchant}'"]
        score, i, info = best
        query["identify"] = {"name": info.get("name"), "domain": info.get("primary_domain"), "confidence": score,
                             "evidence_id": ev["evidence_id"], "field": f"[0].matches[{i}].company_data.basic_info.name"}
        return [], []  # context only: the legal name must come from a source that states it


class CrustdataWeb(_CrustdataSource):
    """Live web search; keeps only legal names the result text ties to the descriptor."""
    name = "crustdata_web"

    def _search(self, q: str, *, item_key, step, attempt) -> dict:
        return self.fetch(f"q={q}", self._call("/web/search/live", {"query": q}),
                          item_key=item_key, step=step, attempt=attempt, op="web_search_live")

    def lookup(self, query, *, item_key, step, attempt):
        merchant = query["merchant"]
        queries = [f'"{merchant}" company legal name']
        ident = query.get("identify")
        if ident and ident.get("name") and canonical(ident["name"]) != canonical(merchant):
            queries.append(f'"{ident["name"]}" legal entity name')
        problems: list[str] = []
        for q in queries:
            ev = self._search(q, item_key=item_key, step=step, attempt=attempt)
            query.setdefault("_evidence", []).append(ev["evidence_id"])
            hits = self._extract(ev, merchant, ident)
            if hits:
                return hits, problems
            problems.append(f"crustdata web search '{q}': no result names a legal entity for '{merchant}'")
        return [], problems

    def _extract(self, ev: dict, merchant: str, ident: dict | None) -> list[SourceHit]:
        data = json.loads(ev["body"])
        retrieved = ev["retrieved_at"]
        if isinstance(data, dict) and data.get("timestamp"):
            retrieved = datetime.fromtimestamp(int(data["timestamp"]) / 1000, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
        # Names that match the merchant directly; the brand from identify is a second key.
        keys = [merchant] + ([ident["name"]] if ident and ident.get("name") else [])
        tier1, tier2 = [], []  # defined-term matches beat token matches
        for idx, r in enumerate((data or {}).get("results", []) if isinstance(data, dict) else []):
            for fld in ("snippet", "title"):  # a sentence beats a page title
                text = _clean_snippet(r.get(fld) or "")
                if not text:
                    continue
                for key in keys:
                    for name in defined_term_names(text, key):
                        tier1.append((idx, fld, name, text, r))
                    for name in token_names(text, key):
                        tier2.append((idx, fld, name, text, r))
        chosen = sorted(tier1 or tier2, key=lambda x: (x[1] == "title", -_legal_signal(x[3]), x[0]))
        hits, seen = [], set()
        for idx, fld, name, text, r in chosen:
            k = (canonical(name), idx)
            if k in seen:
                continue
            seen.add(k)
            hits.append(SourceHit(
                source=self.name, url=r.get("url") or ev["url"], legal_name=name, passage=text,
                field=f"results[{idx}].{fld}", record_date=_snippet_date(r.get("snippet") or ""),
                retrieved_at=retrieved, evidence_id=ev["evidence_id"],
                domain=(ident or {}).get("domain"),
                extra={"title": r.get("title"), "tier": "defined_term" if tier1 else "name_match",
                       "identify": ident},
            ))
        return hits
