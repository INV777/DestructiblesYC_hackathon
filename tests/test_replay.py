"""Replay mode: real recorded Crustdata responses, no key, no network, no spend."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from settlebot.config import load_config
from settlebot.report import build_report, unit_economics
from settlebot.runner import Runner
from settlebot.store import Store

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "tests" / "fixtures" / "pdf_example_bank_lines.json"
sys.path.insert(0, str(ROOT / "scripts"))


def run_replay(tmp_path, monkeypatch, cassette=True, run_id="r1"):
    monkeypatch.delenv("CRUSTDATA_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def no_network(*a, **k):
        raise AssertionError("replay must not call the network")

    monkeypatch.setattr("requests.post", no_network)
    cfg = load_config(ROOT / "config.yaml")
    store = Store(tmp_path / f"{run_id}.db")
    Runner(store, cfg, run_id, EXAMPLE, {"replay": cassette, "no_llm": True, "out_dir": str(tmp_path / "o")}).run()
    return store, cfg


def test_replay_gives_the_live_verdicts_with_no_key(tmp_path, monkeypatch):
    store, cfg = run_replay(tmp_path, monkeypatch)
    rep = build_report(store, "r1")
    lines = {r["line"]: r for r in rep["lines"]}
    assert lines[1]["status"] == "VERIFIED" and lines[1]["payee"] == "Swiggy Limited"
    assert lines[2]["status"] == "VERIFIED" and lines[2]["payee"] == "Amazon Web Services EMEA SARL"
    assert lines[3]["status"] == "HELD_DUPLICATE"
    assert lines[4]["status"] == "CANNOT_VERIFY"
    # Citations keep the time the response was really fetched (the API's own timestamp), not the replay time.
    assert lines[1]["citations"][0]["retrieved_at"].startswith("2026-11-22T")


def test_replay_spends_nothing_but_reports_the_live_estimate(tmp_path, monkeypatch):
    store, cfg = run_replay(tmp_path, monkeypatch)
    rows = store.q("SELECT provider, cost_inr, units FROM ledger WHERE run_id='r1'")
    assert rows and all(r["provider"] == "crustdata-replay" and r["cost_inr"] == 0 for r in rows)
    rep = build_report(store, "r1")
    assert rep["total_cost_inr"] == 0 and rep["replay"]["credits"] > 0
    ue = unit_economics(rep, cfg)
    assert ue["replay"] and ue["live_cost_estimate_inr"] > 0 and ue["measured_cost_inr"] == 0


def test_exported_cassette_replays_the_same_run(tmp_path, monkeypatch):
    from export_cassette import export
    store, _ = run_replay(tmp_path, monkeypatch)
    cassette = export(store, "r1")
    assert cassette["_recorded_at"] == "2026-11-22"
    path = tmp_path / "exported.json"
    path.write_text(json.dumps(cassette), encoding="utf-8")
    again, _ = run_replay(tmp_path, monkeypatch, cassette=str(path), run_id="r2")
    a = [(r["line"], r["status"], r["payee"]) for r in build_report(store, "r1")["lines"]]
    b = [(r["line"], r["status"], r["payee"]) for r in build_report(again, "r2")["lines"]]
    assert a == b
