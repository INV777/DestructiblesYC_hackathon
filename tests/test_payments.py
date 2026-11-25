"""Approvals and exactly-once settlement, against the fake provider (tests only)."""
from __future__ import annotations

from pathlib import Path

import pytest

from settlebot import approvals
from settlebot.config import load_config
from settlebot.payments import FakeProvider, receipt_for, settle
from settlebot.runner import Runner, settle_run
from settlebot.store import Store

ROOT = Path(__file__).resolve().parents[1]
INVOICES = ROOT / "tests" / "fixtures" / "invoices_sample.json"


@pytest.fixture
def env(tmp_path):
    cfg = load_config(ROOT / "config.yaml")
    store = Store(tmp_path / "s.db")
    runner = Runner(store, cfg, "p1", INVOICES, {"sources": ["fake"], "no_llm": True, "out_dir": str(tmp_path / "o")})
    runner.run()
    provider = FakeProvider(tmp_path / "remote.db")
    key = store.one("SELECT item_key FROM items WHERE run_id='p1' AND line_no=1")["item_key"]
    return cfg, store, runner, provider, key


def orders(provider):
    return provider.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]


def test_nothing_is_paid_without_approval(env):
    cfg, store, runner, provider, key = env
    counts = settle_run(runner, provider)
    assert counts["paid"] == 0 and counts["waiting"] >= 1 and orders(provider) == 0


def test_approved_invoice_is_paid_once(env):
    cfg, store, runner, provider, key = env
    approvals.record(store, "p1", key, "PAY", "asha", True)
    settle_run(runner, provider)
    settle_run(runner, provider)  # second run changes nothing
    assert orders(provider) == 1
    assert len(receipt_for(cfg, key)) <= 40


def test_crash_after_order_created_adopts_it(env):
    cfg, store, runner, provider, key = env
    approvals.record(store, "p1", key, "PAY", "asha", True)
    # The order reached the provider, then the process died before recording it.
    provider.create(receipt_for(cfg, key), 4800000, {})
    with store.tx() as c:
        c.execute("INSERT INTO payments (run_id, item_key, receipt, status, provider, amount_paise, created_at,"
                  " updated_at) VALUES ('p1', ?, ?, 'IN_FLIGHT', 'fake', 4800000, 'x', 'x')",
                  (key, receipt_for(cfg, key)))
    row = settle(store, runner.ledger, provider, cfg, "p1", key, 4800000, {})
    assert row["status"] == "CREATED" and orders(provider) == 1
    assert store.one("SELECT COUNT(*) AS n FROM ledger WHERE op='adopt_existing_order'")["n"] == 1


def test_two_approvers_above_threshold(env):
    cfg, store, runner, provider, key = env
    cfg["approvals"]["rules"].append({"action": "PAY", "min_amount_inr": 40000, "approvers": 2})
    approvals.record(store, "p1", key, "PAY", "asha", True)
    assert settle_run(runner, provider)["paid"] == 0
    approvals.record(store, "p1", key, "PAY", "asha", True)  # same person twice doesn't count
    assert settle_run(runner, provider)["paid"] == 0
    approvals.record(store, "p1", key, "PAY", "ravi", True)
    assert settle_run(runner, provider)["paid"] == 1 and orders(provider) == 1


def test_rejection_blocks_payment(env):
    cfg, store, runner, provider, key = env
    approvals.record(store, "p1", key, "PAY", "asha", False, "wrong PO")
    assert settle_run(runner, provider)["rejected"] == 1 and orders(provider) == 0


def test_fraud_hold_is_never_payable(env):
    cfg, store, runner, provider, key = env
    pending = {p["item"]["line_no"]: p["action"] for p in approvals.pending_actions(store, cfg, "p1")}
    assert 2 not in pending            # HELD_FRAUD offers no PAY action at all
    assert pending.get(3) == "RELEASE_HOLD"


def test_unanswered_prompt_leaves_actions_pending(env):
    """End of input at the approval prompt (e.g. stdin from NUL on Windows) must never approve."""
    cfg, store, runner, provider, key = env
    counts = settle_run(runner, provider, ask=lambda card: None, approver="nobody")
    assert counts["paid"] == 0 and orders(provider) == 0
    assert approvals.pending_actions(store, cfg, "p1")
