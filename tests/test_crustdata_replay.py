"""The real Crustdata path, replaying responses recorded from the live API."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from settlebot.config import load_config
from settlebot.report import build_report
from settlebot.runner import Runner
from settlebot.sources import crustdata as cd
from settlebot.store import Store

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "tests" / "fixtures" / "pdf_example_bank_lines.json"
RECORDED = json.loads((ROOT / "tests" / "fixtures" / "crustdata_recorded.json").read_text(encoding="utf-8"))


@pytest.fixture
def replay(monkeypatch):
    calls = []

    def fake_post(self, path, body):
        calls.append((path, body))
        text = json.dumps(body).upper()
        for merchant, data in RECORDED[path].items():
            if merchant in text:
                return data, cd.DEFAULT_CREDITS[path]
        return ({"results": []} if "search" in path else []), cd.DEFAULT_CREDITS[path]

    monkeypatch.setattr(cd.CrustdataClient, "post", fake_post)
    monkeypatch.setenv("CRUSTDATA_API_KEY", "test-key-not-real")
    return calls


def test_problem_statement_example_with_recorded_crustdata(tmp_path, replay):
    cfg = load_config(ROOT / "config.yaml")
    cfg["crustdata"]["inr_per_credit"] = 1.0
    store = Store(tmp_path / "state.db")
    Runner(store, cfg, "r1", EXAMPLE, {"out_dir": str(tmp_path / "out")}).run()
    rep = build_report(store, "r1")
    lines = {r["line"]: r for r in rep["lines"]}

    assert lines[1]["status"] == "VERIFIED" and lines[1]["payee"] == "Swiggy Limited"
    assert lines[1]["account"] == "6110 Staff meals"
    assert lines[2]["status"] == "VERIFIED" and lines[2]["payee"] == "Amazon Web Services EMEA SARL"
    assert lines[2]["citations"][0]["source_url"] == "https://aws.amazon.com/legal/aws-emea/"
    assert lines[3]["status"] == "HELD_DUPLICATE"
    assert lines[4]["status"] == "CANNOT_VERIFY"       # no guess from Paytm / Sharma noise
    assert lines[4]["payee"] is None

    # The duplicate line made no calls; costs are in the ledger in INR.
    assert rep["total_cost_inr"] > 0
    assert store.one("SELECT COUNT(*) AS n FROM ledger WHERE item_key IN "
                     "(SELECT item_key FROM items WHERE line_no=3)")["n"] == 0


def test_restart_reuses_cached_responses(tmp_path, replay):
    cfg = load_config(ROOT / "config.yaml")
    store = Store(tmp_path / "state.db")
    Runner(store, cfg, "r1", EXAMPLE, {"out_dir": str(tmp_path / "out")}).run()
    n_calls = len(replay)
    # Simulate a crash after the resolve step of line 1 finished but before decide:
    store.conn.execute("DELETE FROM steps WHERE step IN ('resolve','decide','post')")
    store.conn.execute("DELETE FROM journal")
    Runner(store, cfg, "r1", EXAMPLE, {"out_dir": str(tmp_path / "out")}).run()
    assert len(replay) == n_calls  # every repeated lookup came from the evidence cache
    assert store.one("SELECT COUNT(*) AS n FROM journal")["n"] == 2
