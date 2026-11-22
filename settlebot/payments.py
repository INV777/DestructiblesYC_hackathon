"""Exactly-once settlement in test mode.

1. Before calling the provider, the payment is written as IN_FLIGHT with a deterministic
   receipt (prefix + 32 hex chars of the item key; Razorpay allows at most 40).
2. On any (re)start, an IN_FLIGHT payment is first looked up at the provider by receipt;
   if it exists it is adopted, and only if it does not is a new order created.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import requests

from .config import env
from .ledger import Ledger
from .store import Store, now_iso


class PaymentError(Exception):
    pass


class Provider:
    name = "base"

    def find_by_receipt(self, receipt: str) -> dict | None:
        raise NotImplementedError

    def create(self, receipt: str, amount_paise: int, notes: dict[str, str]) -> dict:
        raise NotImplementedError


class RazorpayOrders(Provider):
    """Razorpay Orders API, test mode only."""
    name = "razorpay"
    base = "https://api.razorpay.com/v1"

    def __init__(self, cfg: dict[str, Any]):
        self.key_id, self.secret = env("RAZORPAY_KEY_ID"), env("RAZORPAY_KEY_SECRET")
        if not (self.key_id and self.secret):
            raise PaymentError("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set")
        if not self.key_id.startswith("rzp_test_"):
            raise PaymentError("refusing to run with a live Razorpay key: use a rzp_test_ key")
        self.currency = cfg.get("payments", {}).get("currency", "INR")
        self.timeout = float(cfg.get("sources", {}).get("timeout_seconds", 20))

    def _req(self, method: str, path: str, **kw) -> Any:
        r = requests.request(method, self.base + path, auth=(self.key_id, self.secret), timeout=self.timeout, **kw)
        if r.status_code >= 400:
            raise PaymentError(f"razorpay {method} {path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    def find_by_receipt(self, receipt: str) -> dict | None:
        data = self._req("GET", "/orders", params={"receipt": receipt, "count": 5})
        for o in data.get("items", []):
            if o.get("receipt") == receipt:
                return o
        return None

    def create(self, receipt: str, amount_paise: int, notes: dict[str, str]) -> dict:
        return self._req("POST", "/orders", json={"amount": amount_paise, "currency": self.currency,
                                                  "receipt": receipt, "notes": notes})


class FakeProvider(Provider):
    """A separate SQLite file playing the provider's side, for kill tests. Not a payment."""
    name = "fake"

    def __init__(self, path: str | Path, slow: float = 0.0):
        self.conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, receipt TEXT UNIQUE, "
                          "amount INTEGER, notes TEXT, created_at TEXT)")
        self.slow = slow

    def find_by_receipt(self, receipt: str) -> dict | None:
        row = self.conn.execute("SELECT id, receipt, amount FROM orders WHERE receipt=?", (receipt,)).fetchone()
        return {"id": row[0], "receipt": row[1], "amount": row[2]} if row else None

    def create(self, receipt: str, amount_paise: int, notes: dict[str, str]) -> dict:
        if self.slow:
            time.sleep(self.slow)  # widen the window between "sent" and "recorded" for kill tests
        oid = "order_fake_" + receipt[-12:]
        self.conn.execute("INSERT INTO orders (id, receipt, amount, notes, created_at) VALUES (?,?,?,?,?)",
                          (oid, receipt, amount_paise, json.dumps(notes), now_iso()))
        if self.slow:
            time.sleep(self.slow)
        return {"id": oid, "receipt": receipt, "amount": amount_paise}


def build_provider(cfg: dict[str, Any], options: dict[str, Any]) -> Provider:
    name = options.get("pay_provider") or cfg.get("payments", {}).get("provider", "razorpay_orders")
    if name == "fake":
        return FakeProvider(options.get("fake_remote_db", "fake_provider.db"), float(options.get("slow", 0)))
    if name == "razorpay_orders":
        return RazorpayOrders(cfg)
    raise PaymentError(f"unknown payment provider '{name}'")


def receipt_for(cfg: dict[str, Any], item_key: str) -> str:
    prefix = cfg.get("payments", {}).get("receipt_prefix", "sb_")
    receipt = prefix + item_key[:32]
    assert len(receipt) <= 40, "Razorpay receipts are at most 40 characters"
    return receipt


def settle(store: Store, ledger: Ledger, provider: Provider, cfg: dict[str, Any], run_id: str,
           item_key: str, amount_paise: int, notes: dict[str, str]) -> dict:
    receipt = receipt_for(cfg, item_key)
    row = store.one("SELECT * FROM payments WHERE run_id=? AND item_key=?", (run_id, item_key))
    if row and row["status"] == "CREATED":
        return dict(row)
    if row is None:
        with store.tx() as c:
            c.execute("INSERT OR IGNORE INTO payments (run_id, item_key, receipt, status, provider, amount_paise,"
                      " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                      (run_id, item_key, receipt, "IN_FLIGHT", provider.name, amount_paise, now_iso(), now_iso()))

    t0 = time.monotonic()
    existing = provider.find_by_receipt(receipt)          # reconcile before (re)trying
    ledger.record(provider=provider.name, op="find_by_receipt", ok=True, item_key=item_key, step="pay",
                  latency_ms=int((time.monotonic() - t0) * 1000), detail=receipt)
    if existing is None:
        t0 = time.monotonic()
        existing = provider.create(receipt, amount_paise, notes)
        ledger.record(provider=provider.name, op="create_order", ok=True, item_key=item_key, step="pay",
                      latency_ms=int((time.monotonic() - t0) * 1000), detail=receipt)
    else:
        ledger.record(provider=provider.name, op="adopt_existing_order", ok=True, item_key=item_key, step="pay",
                      detail=f"{receipt} -> {existing.get('id')}")
    with store.tx() as c:
        c.execute("UPDATE payments SET status='CREATED', provider_id=?, updated_at=? WHERE run_id=? AND item_key=?",
                  (existing.get("id"), now_iso(), run_id, item_key))
    return dict(store.one("SELECT * FROM payments WHERE run_id=? AND item_key=?", (run_id, item_key)))
