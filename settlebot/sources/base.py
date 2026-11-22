"""Shared source plumbing: cache, retries, evidence storage and the cost ledger."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from ..ledger import Ledger
from ..models import sha256
from ..store import Store, now_iso


class TransientError(Exception):
    """A failure worth retrying (timeout, 429, 5xx)."""


class PermanentError(Exception):
    """A failure not worth retrying (4xx, bad input)."""


@dataclass
class SourceContext:
    store: Store
    ledger: Ledger
    cfg: dict[str, Any]
    run_id: str
    options: dict[str, Any] = field(default_factory=dict)  # e.g. {"slow": 0.05} for kill tests


@dataclass
class SourceHit:
    """One candidate payee, with the exact passage or field that names it."""
    source: str
    url: str
    legal_name: str | None
    passage: str                 # verbatim text from the stored response that states the name
    field: str | None            # API field path, when the source is structured
    record_date: str | None
    retrieved_at: str
    evidence_id: str
    domain: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Fetched:
    body: str
    url: str
    record_date: str | None
    units: float = 0.0
    unit_type: str | None = None
    cost_inr: float = 0.0


class Source:
    name = "base"
    provider = "base"

    def __init__(self, ctx: SourceContext):
        self.ctx = ctx

    def lookup(self, query: dict[str, Any], *, item_key: str, step: str, attempt: int) -> tuple[list[SourceHit], list[str]]:
        """Return (hits, problems). Problems are human-readable reasons a source gave nothing."""
        raise NotImplementedError

    # ---- shared helpers -------------------------------------------------------
    def fetch(self, query_key: str, call: Callable[[], Fetched], *, item_key: str, step: str,
              attempt: int, op: str) -> dict[str, Any]:
        """Cached, retried, ledgered call. Returns the evidence row as a dict."""
        store, ledger, run_id = self.ctx.store, self.ctx.ledger, self.ctx.run_id
        cached = store.cached_evidence(run_id, self.name, query_key)
        if cached is not None:
            ledger.record(provider=self.provider, op=op, ok=True, cache_hit=True,
                          item_key=item_key, step=step, attempt=attempt, detail=query_key)
            return dict(cached)

        scfg = self.ctx.cfg.get("sources", {})
        retries = int(scfg.get("retries", 2))
        backoff = float(scfg.get("backoff_seconds", 1.5))
        last_err: Exception | None = None
        for i in range(retries + 1):
            t0 = time.monotonic()
            try:
                got = call()
            except TransientError as e:
                ledger.record(provider=self.provider, op=op, ok=False, latency_ms=int((time.monotonic() - t0) * 1000),
                              item_key=item_key, step=step, attempt=attempt, detail=f"retry {i}: {e}")
                last_err = e
                time.sleep(backoff * (i + 1))
                continue
            except PermanentError as e:
                ledger.record(provider=self.provider, op=op, ok=False, latency_ms=int((time.monotonic() - t0) * 1000),
                              item_key=item_key, step=step, attempt=attempt, detail=str(e))
                raise
            ms = int((time.monotonic() - t0) * 1000)
            ledger.record(provider=self.provider, op=op, ok=True, units=got.units, unit_type=got.unit_type,
                          cost_inr=got.cost_inr, latency_ms=ms, item_key=item_key, step=step,
                          attempt=attempt, detail=query_key)
            row = {
                "evidence_id": sha256(f"{self.name}|{query_key}|{got.body}")[:24],
                "run_id": run_id, "source": self.name, "query_key": query_key, "url": got.url,
                "retrieved_at": now_iso(), "record_date": got.record_date,
                "body_sha256": sha256(got.body), "body": got.body,
            }
            store.save_evidence(row)
            return row
        raise PermanentError(f"{self.name}: gave up after {retries + 1} tries: {last_err}")
