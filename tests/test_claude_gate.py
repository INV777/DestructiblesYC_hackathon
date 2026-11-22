"""Claude only proposes. These tests script Claude's answers and check the code's gates."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from settlebot import llm
from settlebot.config import load_config
from settlebot.report import build_report
from settlebot.runner import Runner
from settlebot.sources import crustdata as cd
from settlebot.store import Store

ROOT = Path(__file__).resolve().parents[1]

SEARCH = {"success": True, "timestamp": 1795346060311, "results": [
    {"title": "Contact us", "url": "https://www.grab.com/my/legal/",
     "snippet": "Services in Malaysia are provided by GrabPay Malaysia Berhad, a licensed e-money issuer."},
]}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("CRUSTDATA_API_KEY", "test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(cd.CrustdataClient, "post",
                        lambda self, path, body: (SEARCH if "search" in path else [], 1.0))
    answers: list[dict] = []
    monkeypatch.setattr(llm.Claude, "call_tool", lambda self, tool, prompt, **kw: answers.pop(0))
    inp = tmp_path / "in.json"
    inp.write_text(json.dumps([{"date": "2026-10-11", "description": "GRABPAY", "amount_inr": 900}]))
    cfg = load_config(ROOT / "config.yaml")

    def run():
        store = Store(tmp_path / "s.db")
        Runner(store, cfg, "c1", inp, {"out_dir": str(tmp_path / "o")}).run()
        return build_report(store, "c1")["lines"][0]
    return answers, run


def test_valid_claude_proposal_is_accepted(setup):
    answers, run = setup
    answers += [
        {"found": True, "result_index": 0, "field": "snippet", "legal_name": "GrabPay Malaysia Berhad",
         "quote": "provided by GrabPay Malaysia Berhad", "reason": "states the provider"},
        {"account_code": "6430", "reason": "payment app subscription"},
    ]
    line = run()
    assert line["status"] == "VERIFIED" and line["payee"] == "GrabPay Malaysia Berhad"
    assert line["account"] == "6430 Software subscriptions"
    assert line["citations"][0]["field_or_passage"] == "provided by GrabPay Malaysia Berhad"


def test_quote_not_in_source_is_rejected(setup):
    answers, run = setup
    answers += [{"found": True, "result_index": 0, "field": "snippet", "legal_name": "GrabPay Malaysia Berhad",
                 "quote": "GrabPay Malaysia Berhad is the merchant of record", "reason": "made up"}]
    line = run()
    assert line["status"] == "CANNOT_VERIFY" and line["payee"] is None


def test_name_unrelated_to_descriptor_is_rejected(setup):
    answers, run = setup
    answers += [{"found": True, "result_index": 0, "field": "snippet", "legal_name": "Malaysia Berhad",
                 "quote": "Malaysia Berhad", "reason": "wrong entity"}]
    assert run()["status"] == "CANNOT_VERIFY"


def test_account_outside_chart_fails_closed(setup):
    answers, run = setup
    answers += [
        {"found": True, "result_index": 0, "field": "snippet", "legal_name": "GrabPay Malaysia Berhad",
         "quote": "provided by GrabPay Malaysia Berhad", "reason": "ok"},
        {"account_code": "1234", "reason": "invented code"},
    ]
    line = run()
    assert line["status"] == "CANNOT_VERIFY" and line["payee"] == "GrabPay Malaysia Berhad"


def test_pii_is_masked_before_prompting():
    from settlebot.pii_guard import mask, mask_account
    s = mask("Pay ABCDE1234F at 9876543210, acct 123456789012, rahul@okhdfc, x@y.com")
    assert "ABCDE1234F" not in s and "9876543210" not in s and "123456789012" not in s
    assert "x@y.com" not in s
    assert mask_account("50100123456789") == "XXXXXXXXXX6789"
