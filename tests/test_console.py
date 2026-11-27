"""Web console: the API drives real worker processes (main.py run), offline, with no keys."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from settlebot.web.app import create_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(tmp_path, monkeypatch):
    for k in ("CRUSTDATA_API_KEY", "ANTHROPIC_API_KEY", "RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "SETTLEBOT_DEMO"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    app = create_app(data_dir=tmp_path / "data", config_path=ROOT / "config.yaml", demo=False)
    with TestClient(app) as c:
        c.data_dir = tmp_path / "data"
        yield c


def wait_idle(c, run_id, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        d = c.get(f"/api/runs/{run_id}").json()
        if not d["worker"]["running"] and d["status"] in ("DONE", "LIMIT_REACHED"):
            return d
        time.sleep(0.3)
    raise AssertionError(f"run {run_id} did not finish: {d['status']} {d['worker']}")


def start(c, **body):
    r = c.post("/api/runs", json=body)
    assert r.status_code == 201, r.text
    return r.json()["run_id"]


def statuses(d):
    return {l["line"]: l["status"] for l in d["lines"]}


def test_bank_lines_replay_end_to_end(client):
    run = start(client, sample="bank-lines", mode="replay")
    d = wait_idle(client, run)
    assert statuses(d) == {1: "VERIFIED", 2: "VERIFIED", 3: "HELD_DUPLICATE", 4: "CANNOT_VERIFY"}
    assert d["totals"]["total_cost_inr"] == 0 and d["totals"]["replay"]["credits"] > 0
    assert d["unit_economics"]["live_cost_estimate_inr"] > 0

    # Every cited passage is in the stored response the API hands back.
    cite = d["lines"][0]["citations"][0]
    ev = client.get(f"/api/runs/{run}/evidence/{cite['evidence_id']}").json()
    assert cite["field_or_passage"][:40] in ev["body"]
    assert client.get(f"/api/runs/{run}/evidence/nope").status_code == 404

    calls = client.get(f"/api/runs/{run}/ledger").json()
    assert calls and {c["provider"] for c in calls} == {"crustdata-replay"}

    # Release the duplicate: it is posted once, by a settle worker.
    assert d["pending"] == [{"line": 3, "action": "RELEASE_HOLD", "amount_inr": 18500.0, "need": 1, "have": []}]
    r = client.post(f"/api/runs/{run}/decisions",
                    json={"line": 3, "action": "RELEASE_HOLD", "approve": True, "approver": "Asha"})
    assert r.json()["state"] == "APPROVED"
    assert client.post(f"/api/runs/{run}/settle").status_code == 200
    d = wait_idle(client, run)
    assert d["totals"]["posted"] == 3 and d["totals"]["posted_twice"] == 0


def test_invoice_is_paid_once_however_often_you_settle(client):
    run = start(client, sample="invoices", mode="fixtures")
    d = wait_idle(client, run)
    assert statuses(d) == {1: "VERIFIED", 2: "HELD_FRAUD", 3: "HELD_DUPLICATE", 4: "CANNOT_VERIFY",
                           5: "HELD_FRAUD", 6: "HELD_FRAUD"}
    # Bank account numbers never leave the server unmasked.
    line1 = d["lines"][0]
    assert line1["bank_account"]["account"].endswith("0001") and "50100123450001" not in str(d)

    assert client.post(f"/api/runs/{run}/settle").status_code == 200   # nothing approved: nothing paid
    d = wait_idle(client, run)
    assert d["lines"][0]["payment"] is None

    ok = client.post(f"/api/runs/{run}/decisions", json={"line": 1, "action": "PAY", "approve": True, "approver": "Asha"})
    assert ok.json()["state"] == "APPROVED"
    again = client.post(f"/api/runs/{run}/decisions", json={"line": 1, "action": "PAY", "approve": True, "approver": "Asha"})
    assert again.status_code == 409
    for _ in range(2):
        assert client.post(f"/api/runs/{run}/settle").status_code == 200
        d = wait_idle(client, run)
    assert d["lines"][0]["payment"]["status"] == "CREATED" and d["totals"]["paid_twice"] == 0
    orders = sqlite3.connect(client.data_dir / "fake_provider.db").execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    assert orders == 1


def test_crash_the_worker_then_resume(client):
    run = start(client, sample="bank-lines", mode="replay", slow=True)
    end = time.time() + 30
    while time.time() < end:  # wait until a lookup is in flight, then kill mid-step
        d = client.get(f"/api/runs/{run}").json()
        if any(s["status"] == "STARTED" and s["step"] == "resolve" for l in d["lines"] for s in l["steps"]):
            break
        time.sleep(0.1)
    killed = client.post(f"/api/runs/{run}/kill")
    if killed.status_code == 200:
        d = client.get(f"/api/runs/{run}").json()
        assert d["status"] == "RUNNING" and not d["worker"]["running"]
        assert client.post(f"/api/runs/{run}/kill").status_code == 409     # nothing left to kill
        assert client.post(f"/api/runs/{run}/resume").status_code == 200
    d = wait_idle(client, run)
    assert statuses(d) == {1: "VERIFIED", 2: "VERIFIED", 3: "HELD_DUPLICATE", 4: "CANNOT_VERIFY"}
    assert d["totals"]["posted"] == 2 and d["totals"]["posted_twice"] == 0
    # Same calls as a clean run: a cut-off call is made again, nothing finished is repeated.
    fresh = [c for c in client.get(f"/api/runs/{run}/ledger").json() if not c["cache_hit"]]
    assert len(fresh) == 6


def test_bad_requests_are_explained(client):
    assert client.post("/api/runs", json={"sample": "bank-lines", "mode": "live"}).status_code == 400
    assert client.post("/api/runs", json={"sample": "nope", "mode": "replay"}).status_code == 400
    assert client.post("/api/runs", json={"mode": "replay"}).status_code == 400
    r = client.post("/api/runs", json={"lines": [{"amount_inr": "lots"}], "mode": "replay"})
    assert r.status_code == 400 and "could not be read" in r.json()["detail"]
    assert client.get("/api/runs/does-not-exist").status_code == 404


def test_demo_mode_never_allows_live(tmp_path, monkeypatch):
    monkeypatch.setenv("CRUSTDATA_API_KEY", "not-a-real-key")
    app = create_app(data_dir=tmp_path / "d", config_path=ROOT / "config.yaml", demo=True, seed=False)
    with TestClient(app) as c:
        info = c.get("/api/info").json()
        assert info["demo"] and not info["modes"]["live"]["available"] and info["payments"] == "simulated provider"
        assert c.post("/api/runs", json={"sample": "bank-lines", "mode": "live"}).status_code == 400
        too_many = [{"date": "2026-10-01", "description": "X", "amount_inr": 1}] * 301
        assert c.post("/api/runs", json={"lines": too_many, "mode": "fixtures"}).status_code == 413
        assert c.get("/").status_code == 200 and "SettleBot" in c.get("/").text


def test_public_demo_starts_with_finished_example_runs(tmp_path, monkeypatch):
    monkeypatch.delenv("CRUSTDATA_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    app = create_app(data_dir=tmp_path / "d", config_path=ROOT / "config.yaml", demo=True)
    with TestClient(app) as c:
        runs = c.get("/api/runs").json()
        assert {r["label"] for r in runs} == {"Bank lines from the problem statement", "Freight vendor invoices"}
        for r in runs:
            d = wait_idle(c, r["run_id"])
            assert d["lines"] and all(l["status"] != "PENDING" for l in d["lines"])
    # A restart with runs already present does not add more.
    with TestClient(create_app(data_dir=tmp_path / "d", config_path=ROOT / "config.yaml", demo=True)) as c:
        assert len(c.get("/api/runs").json()) == 2
