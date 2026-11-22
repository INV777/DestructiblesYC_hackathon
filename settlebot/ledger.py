"""Cost ledger and the budget/time guard. Every external call writes one row."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .store import Store


class LimitReached(Exception):
    """Raised before a call that would run past the run's cost or time limit."""


@dataclass
class Ledger:
    store: Store
    run_id: str
    cfg: dict[str, Any]

    def record(self, *, provider: str, op: str, ok: bool, units: float = 0, unit_type: str | None = None,
               cost_inr: float = 0.0, latency_ms: int | None = None, cache_hit: bool = False,
               item_key: str | None = None, step: str | None = None, attempt: int | None = None,
               detail: str | None = None) -> None:
        self.store.ledger_add({
            "run_id": self.run_id, "item_key": item_key, "step": step, "attempt": attempt,
            "provider": provider, "op": op, "units": units, "unit_type": unit_type,
            "cost_inr": round(cost_inr, 6), "latency_ms": latency_ms, "ok": int(ok),
            "cache_hit": int(cache_hit), "detail": detail,
        })

    def spent(self) -> float:
        return self.store.total_cost(self.run_id)

    # ---- pricing helpers ------------------------------------------------------
    def crustdata_inr(self, credits: float) -> float:
        return credits * float(self.cfg.get("crustdata", {}).get("inr_per_credit", 0.0))

    def llm_inr(self, input_tokens: int, output_tokens: int) -> float:
        p = self.cfg.get("llm", {}).get("price_inr_per_mtok", {})
        return (input_tokens * float(p.get("input", 0)) + output_tokens * float(p.get("output", 0))) / 1_000_000


@dataclass
class Guard:
    """Checks the cost and time limits before each step that can spend money."""
    ledger: Ledger
    max_cost_inr: float
    max_time_seconds: float
    elapsed_before: float      # active seconds from earlier processes of this run
    started_monotonic: float   # when this process started working on the run

    def elapsed(self, now_monotonic: float) -> float:
        return self.elapsed_before + (now_monotonic - self.started_monotonic)

    def check(self, now_monotonic: float) -> None:
        spent = self.ledger.spent()
        if spent >= self.max_cost_inr:
            raise LimitReached(f"cost limit reached: spent INR {spent:.2f} of {self.max_cost_inr:.2f}")
        if self.elapsed(now_monotonic) >= self.max_time_seconds:
            raise LimitReached(
                f"time limit reached: {self.elapsed(now_monotonic):.0f}s of {self.max_time_seconds:.0f}s")
