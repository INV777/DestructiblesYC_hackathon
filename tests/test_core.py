"""Core behaviour on the problem statement's example, using fixture sources only."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from settlebot.config import load_config
from settlebot.grounding import passage_in_body
from settlebot.models import LineItem
from settlebot.normalize import normalize_descriptor
from settlebot.report import build_report
from settlebot.runner import RunError, Runner
from settlebot.store import Store

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "tests" / "fixtures" / "pdf_example_bank_lines.json"


@pytest.fixture
def cfg():
    return load_config(ROOT / "config.yaml")


def run_fake(tmp_path, cfg, run_id="t1", input_path=EXAMPLE):
    store = Store(tmp_path / "state.db")
    Runner(store, cfg, run_id, input_path, {"sources": ["fake"], "out_dir": str(tmp_path / "out")}).run()
    return store


def test_identical_lines_get_distinct_keys():
    a = LineItem.from_dict(2, {"date": "2026-10-07", "description": "AWS EMEA", "amount_inr": 18500})
    b = LineItem.from_dict(3, {"date": "2026-10-07", "description": "AWS EMEA", "amount_inr": 18500})
    assert a.item_key("x") != b.item_key("x")


def test_normalize_strips_processor_and_city(cfg):
    n = normalize_descriptor("RAZ*SWIGGY BLR", cfg)
    assert (n["merchant"], n["processor"], n["city"]) == ("SWIGGY", "Razorpay", "BLR")
    n = normalize_descriptor("PAYTM*SHARMA TRD", cfg)
    assert (n["merchant"], n["processor"]) == ("SHARMA TRD", "Paytm")


def test_problem_statement_example(tmp_path, cfg):
    store = run_fake(tmp_path, cfg)
    lines = {r["line"]: r for r in build_report(store, "t1")["lines"]}
    assert lines[1]["status"] == "VERIFIED" and lines[1]["payee"] == "Swiggy Limited"
    assert lines[1]["account"] == "6110 Staff meals"
    assert lines[2]["status"] == "VERIFIED" and lines[2]["payee"] == "Amazon Web Services EMEA SARL"
    assert lines[2]["account"] == "6420 Cloud hosting"
    assert lines[3]["status"] == "HELD_DUPLICATE" and lines[3]["related_line"] == 2
    assert lines[4]["status"] == "CANNOT_VERIFY" and lines[4]["payee"] is None
    assert lines[4]["missing"]
    # Only the two verified lines are posted; the duplicate and the unknown payee are not.
    assert store.one("SELECT COUNT(*) AS n FROM journal WHERE run_id='t1'")["n"] == 2
    # Every verified fact carries a citation with a URL, a passage and a retrieval date.
    for n in (1, 2):
        c = lines[n]["citations"][0]
        assert c["source_url"] and c["field_or_passage"] and c["retrieved_at"]


def test_rerun_same_run_id_changes_nothing(tmp_path, cfg):
    store = run_fake(tmp_path, cfg)
    ledger_rows = store.one("SELECT COUNT(*) AS n FROM ledger")["n"]
    run_fake(tmp_path, cfg)  # same run-id, same input
    assert store.one("SELECT COUNT(*) AS n FROM journal")["n"] == 2
    assert store.one("SELECT COUNT(*) AS n FROM ledger")["n"] == ledger_rows  # no new calls at all


def test_run_id_cannot_be_reused_for_other_input(tmp_path, cfg):
    run_fake(tmp_path, cfg)
    other = tmp_path / "other.json"
    other.write_text(json.dumps([{"date": "2026-10-10", "description": "X", "amount_inr": 1}]))
    with pytest.raises(RunError):
        run_fake(tmp_path, cfg, input_path=other)


def test_grounding_rejects_passage_not_in_source():
    body = json.dumps({"name": "Swiggy Limited"})
    assert passage_in_body("Swiggy Limited", body)
    assert not passage_in_body("Swiggy Private Limited", body)


def test_cost_limit_fails_closed(tmp_path, cfg):
    cfg["limits"]["max_cost_inr"] = 0.0  # nothing may be spent
    store = run_fake(tmp_path, cfg)
    statuses = [r["status"] for r in build_report(store, "t1")["lines"]]
    assert statuses == ["CANNOT_VERIFY", "CANNOT_VERIFY", "HELD_DUPLICATE", "CANNOT_VERIFY"]
    assert store.one("SELECT COUNT(*) AS n FROM journal")["n"] == 0


INVOICES = ROOT / "tests" / "fixtures" / "invoices_sample.json"


def test_invoices_bank_change_duplicate_and_unknown_vendor(tmp_path, cfg):
    store = run_fake(tmp_path, cfg, run_id="inv", input_path=INVOICES)
    lines = {r["line"]: r for r in build_report(store, "inv")["lines"]}
    assert lines[1]["status"] == "VERIFIED" and lines[1]["account"] == "5110 Freight and carriage"
    assert lines[2]["status"] == "HELD_FRAUD"
    assert "33330000999999" not in lines[2]["explanation"]          # account numbers are masked
    assert lines[3]["status"] == "HELD_DUPLICATE" and lines[3]["related_line"] == 1  # INV-0101 == INV-101
    assert lines[4]["status"] == "CANNOT_VERIFY"                     # vendor not in master
    assert store.one("SELECT COUNT(*) AS n FROM journal WHERE run_id='inv'")["n"] == 1


def test_duplicate_explanation_fits_the_line_kind(tmp_path, cfg):
    inv = {r["line"]: r for r in build_report(run_fake(tmp_path / "a", cfg, run_id="inv", input_path=INVOICES),
                                               "inv")["lines"]}
    assert "invoice number" in inv[3]["explanation"] and "descriptor" not in inv[3]["explanation"]
    bank = {r["line"]: r for r in build_report(run_fake(tmp_path / "b", cfg), "t1")["lines"]}
    assert "descriptor" in bank[3]["explanation"]


def test_model_tokens_are_priced_in_rupees(tmp_path, cfg):
    """Regression: a broken price block once logged every Claude call at INR 0, so the cost cap ignored it."""
    from settlebot.ledger import Ledger
    p = cfg["llm"]["price_inr_per_mtok"]
    assert p["input"] > 0 and p["output"] > 0
    ledger = Ledger(Store(tmp_path / "state.db"), "x", cfg)
    assert ledger.llm_inr(1_000_000, 0) == pytest.approx(p["input"])
    assert ledger.llm_inr(800, 120) > 0


def test_lookalike_sender_domain_and_vendor_name_are_blocked(tmp_path, cfg):
    store = run_fake(tmp_path, cfg, run_id="lk", input_path=INVOICES)
    lines = {r["line"]: r for r in build_report(store, "lk")["lines"]}
    assert lines[1]["status"] == "VERIFIED"                       # real domain on file
    assert lines[5]["status"] == "HELD_FRAUD"                     # vrlgr0up.in imitates vrlgroup.in
    assert "vrlgroup.in" in lines[5]["explanation"]
    assert lines[6]["status"] == "HELD_FRAUD"                     # 'VRL Logistlcs' under unknown vendor id
    assert "V001" in lines[6]["explanation"]


def test_unverified_descriptor_that_resembles_known_payee_says_so(tmp_path, cfg):
    p = tmp_path / "lines.json"
    p.write_text(json.dumps([
        {"date": "2026-10-03", "description": "RAZ*SWIGGY BLR", "amount_inr": 1240},
        {"date": "2026-10-04", "description": "RAZ*SW1GGY BLR", "amount_inr": 990},
    ]))
    store = run_fake(tmp_path, cfg, run_id="sw", input_path=p)
    lines = {r["line"]: r for r in build_report(store, "sw")["lines"]}
    assert lines[2]["status"] == "CANNOT_VERIFY" and lines[2]["payee"] is None
    assert any("resembles known payee 'Swiggy Limited'" in m for m in lines[2]["missing"])
