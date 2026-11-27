"""Console uploads: any export in, a preview a person can correct, then a run. Offline, no keys, no AI.

Also covers the public demo's promises: runs from a visitor's own file are private to that browser,
deleted after an hour, and no worker in the demo can ever spend money.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from settlebot.web.app import DEMO_PRIVATE_TTL_SECONDS, create_app  # noqa: E402
from settlebot.web.workers import API_KEY_NAMES  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "tests" / "fixtures" / "imports"
ALICE = {"X-SettleBot-Owner": "alice-" + "a" * 30}
BOB = {"X-SettleBot-Owner": "bob-" + "b" * 30}


def no_keys(monkeypatch, tmp_path):
    for k in (*API_KEY_NAMES, "SETTLEBOT_DEMO"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)


def upload(c, name, headers=None, data=None):
    body = data if data is not None else (SAMPLES / name).read_bytes()
    return c.post(f"/api/imports?filename={name}", content=body, headers=headers or {})


def wait_idle(c, run_id, headers=None, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        d = c.get(f"/api/runs/{run_id}", headers=headers or {}).json()
        if not d["worker"]["running"] and d["status"] in ("DONE", "LIMIT_REACHED"):
            return d
        time.sleep(0.3)
    raise AssertionError(f"run {run_id} did not finish")


@pytest.fixture
def local(tmp_path, monkeypatch):
    no_keys(monkeypatch, tmp_path)
    app = create_app(data_dir=tmp_path / "data", config_path=ROOT / "config.yaml", demo=False)
    with TestClient(app) as c:
        c.paths = app.state.paths
        yield c


def test_upload_preview_and_run_a_messy_statement(local):
    r = upload(local, "statement_withdrawal_deposit.xls")
    assert r.status_code == 201, r.text
    imp = r.json()
    assert imp["kind"] == "bank_line" and imp["line_count"] == 5 and imp["money_in"] == 2
    assert imp["header_row"] == 7 and not imp["problems"]
    assert [round(l["amount_inr"], 2) for l in imp["lines"]] == [1240.0, 18500.0, 18500.0, 4000.0, 860.5]
    assert (local.paths.imports / f"{imp['import_id']}.bin").exists()

    run = local.post("/api/runs", json={"import_id": imp["import_id"], "mode": "replay"})
    assert run.status_code == 201, run.text
    d = wait_idle(local, run.json()["run_id"])
    assert len(d["lines"]) == 5 and d["meta"]["label"] == "statement_withdrawal_deposit.xls"
    assert d["meta"]["source"]["header_row"] == 7 and d["meta"]["source"]["money_in_left_out"] == 2
    assert d["totals"]["total_cost_inr"] == 0
    # The lines are kept; the uploaded file itself is not.
    assert not (local.paths.imports / f"{imp['import_id']}.bin").exists()
    assert local.post("/api/runs", json={"import_id": imp["import_id"], "mode": "replay"}).status_code == 404


def test_a_person_can_correct_what_was_read(local):
    imp = upload(local, "statement_ambiguous_dates.csv").json()
    first = imp["lines"][0]["date"]
    flipped = local.post(f"/api/imports/{imp['import_id']}/preview",
                         json={"day_first": not imp["day_first"]}).json()
    assert flipped["lines"][0]["date"] != first and flipped["day_first"] is (not imp["day_first"])

    # Taking away a column the run needs is caught before a run starts, with a reason.
    gone = local.post(f"/api/imports/{imp['import_id']}/preview",
                      json={"mapping": {"description": None}}).json()
    assert gone["problems"]
    r = local.post("/api/runs", json={"import_id": imp["import_id"], "mode": "replay",
                                      "choices": {"mapping": {"description": None}}})
    assert r.status_code == 400 and r.json()["detail"] == gone["problems"][0]


def test_invoice_list_without_ids_and_account_numbers_masked(local):
    imp = upload(local, "invoice_register.xlsx").json()
    assert imp["kind"] == "invoice" and imp["line_count"] == 4
    assert imp["lines"][0]["vendor_id"] == "V001"
    shown = json.dumps(imp)  # the preview lines, the column samples and the rows left out
    assert "50100123450001" not in shown and "XXXXXXXXXX0001" in shown  # last digits only


def test_files_that_cannot_be_read(local):
    assert upload(local, "x.pdf", data=b"%PDF-1.7 ...").status_code == 400
    assert "Excel or CSV" in upload(local, "x.pdf", data=b"%PDF-1.7 ...").json()["detail"]
    assert upload(local, "empty.csv", data=b"").status_code == 400
    r = upload(local, "junk.xlsx", data=b"PK\x03\x04 not really a workbook")
    assert r.status_code == 400 and "couldn't find a table" in r.json()["detail"]
    assert not list(local.paths.imports.glob("*.bin"))  # nothing kept from a failed read
    assert local.post("/api/imports/0123456789abcdef/preview", json={}).status_code == 404
    assert local.post("/api/imports/../../x/preview", json={}).status_code == 404
    assert local.post("/api/runs", json={"import_id": "../../etc", "mode": "replay"}).status_code == 422


def test_sample_files_can_be_downloaded(local):
    names = [f["name"] for f in local.get("/api/info").json()["sample_files"]]
    assert names
    for n in names:
        r = local.get(f"/api/sample-files/{n}")
        assert r.status_code == 200 and r.content == (SAMPLES / n).read_bytes()
    assert local.get("/api/sample-files/..%2F..%2Fconfig.yaml").status_code == 404
    assert local.get("/api/sample-files/statement_no_header.csv").status_code == 404  # not on the list


@pytest.fixture
def demo(tmp_path, monkeypatch):
    no_keys(monkeypatch, tmp_path)
    app = create_app(data_dir=tmp_path / "data", config_path=ROOT / "config.yaml", demo=True, seed=False)
    with TestClient(app) as c:
        c.app_ = app
        yield c


def test_demo_runs_from_your_own_file_are_private(demo):
    imp = upload(demo, "statement_drcr_semicolon.csv", ALICE).json()
    assert demo.post(f"/api/imports/{imp['import_id']}/preview", json={}, headers=BOB).status_code == 404
    r = demo.post("/api/runs", json={"import_id": imp["import_id"], "mode": "replay"}, headers=BOB)
    assert r.status_code == 404
    run_id = demo.post("/api/runs", json={"import_id": imp["import_id"], "mode": "replay"}, headers=ALICE).json()["run_id"]
    sample_id = demo.post("/api/runs", json={"sample": "bank-lines", "mode": "replay"}, headers=ALICE).json()["run_id"]
    d = wait_idle(demo, run_id, ALICE)
    assert d["meta"]["private"] and d["meta"]["deleted_after_ts"] > time.time()

    seen_by = lambda h: {r["run_id"] for r in demo.get("/api/runs", headers=h).json()}  # noqa: E731
    assert run_id in seen_by(ALICE) and sample_id in seen_by(ALICE)
    assert run_id not in seen_by(BOB) and sample_id in seen_by(BOB)  # sample runs are shared
    assert run_id not in seen_by({})
    cite = next(c for l in d["lines"] for c in l.get("citations") or [])
    for path in (f"/api/runs/{run_id}", f"/api/runs/{run_id}/ledger",
                 f"/api/runs/{run_id}/evidence/{cite['evidence_id']}"):
        assert demo.get(path, headers=BOB).status_code == 404
        assert demo.get(path).status_code == 404
    for path in ("kill", "resume", "settle"):
        assert demo.post(f"/api/runs/{run_id}/{path}", headers=BOB).status_code == 404
    assert demo.post(f"/api/runs/{run_id}/decisions", headers=BOB,
                     json={"line": 1, "action": "PAY", "approver": "x", "approve": True}).status_code == 404

    # Without the browser's id there is nothing to keep a run private to, so the demo refuses.
    assert upload(demo, "statement_drcr_semicolon.csv").status_code == 400
    assert demo.post("/api/runs", json={"mode": "fixtures", "lines": [
        {"date": "2026-10-01", "description": "UPI/SWIGGY", "amount_inr": 100}]}).status_code == 400

    # Lines posted as JSON are private too.
    posted = demo.post("/api/runs", headers=ALICE, json={"mode": "fixtures", "lines": [
        {"date": "2026-10-01", "description": "UPI/SWIGGY", "amount_inr": 100}]}).json()["run_id"]
    assert posted in seen_by(ALICE) and posted not in seen_by(BOB)


def test_demo_deletes_private_runs_and_uploads_after_an_hour(demo):
    paths, store_path = demo.app_.state.paths, demo.app_.state.paths.db
    imp = upload(demo, "statement_signed_two_sheets.xlsx", ALICE).json()
    left_behind = upload(demo, "statement_no_header.csv", ALICE).json()  # uploaded, never run
    run_id = demo.post("/api/runs", json={"import_id": imp["import_id"], "mode": "replay"}, headers=ALICE).json()["run_id"]
    sample_id = demo.post("/api/runs", json={"sample": "bank-lines", "mode": "replay"}).json()["run_id"]
    wait_idle(demo, run_id, ALICE)
    wait_idle(demo, sample_id)

    old = time.time() - DEMO_PRIVATE_TTL_SECONDS - 5
    for p in (paths.meta / f"{run_id}.json", paths.meta / f"{sample_id}.json",
              paths.imports / f"{left_behind['import_id']}.json"):
        m = json.loads(p.read_text(encoding="utf-8"))
        m["created_ts"] = old
        p.write_text(json.dumps(m), encoding="utf-8")
    input_path = Path(json.loads((paths.meta / f"{run_id}.json").read_text())["input_path"])
    assert input_path.exists()

    listed = {r["run_id"] for r in demo.get("/api/runs", headers=ALICE).json()}
    assert run_id not in listed and sample_id in listed  # shared sample runs stay
    assert demo.get(f"/api/runs/{run_id}", headers=ALICE).status_code == 404
    assert not input_path.exists() and not (paths.meta / f"{run_id}.json").exists()
    assert not list(paths.imports.iterdir())
    import sqlite3
    with sqlite3.connect(store_path) as db:
        for table in ("runs", "items", "steps", "evidence", "ledger", "journal"):
            assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id=?", (run_id,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM items WHERE run_id=?", (sample_id,)).fetchone()[0] > 0


def test_demo_upload_limits(demo):
    big = b"date,description,amount\n" + b"2026-10-01,UPI/SWIGGY,100\n" * 120_000
    assert upload(demo, "big.csv", ALICE, data=big).status_code == 413
    many = b"date,description,amount\n" + b"".join(b"2026-10-01,UPI/SHOP %d,100\n" % i for i in range(301))
    imp = upload(demo, "many.csv", ALICE, data=many).json()
    assert imp["line_count"] == 301
    r = demo.post("/api/runs", json={"import_id": imp["import_id"], "mode": "fixtures"}, headers=ALICE)
    assert r.status_code == 413


def test_demo_never_spends_money_even_with_keys_set(tmp_path, monkeypatch):
    """The promise behind the public demo: no Crustdata credits, no Claude tokens, no real payment."""
    no_keys(monkeypatch, tmp_path)
    for k in API_KEY_NAMES:
        monkeypatch.setenv(k, "not-a-real-key")
    app = create_app(data_dir=tmp_path / "data", config_path=ROOT / "config.yaml", demo=True, seed=False)
    workers = app.state.workers
    started: list[tuple[list[str], dict]] = []
    real_popen = __import__("subprocess").Popen

    def spy(cmd, **kw):
        started.append((cmd, kw["env"]))
        return real_popen(cmd, **kw)

    monkeypatch.setattr("settlebot.web.workers.subprocess.Popen", spy)
    with TestClient(app) as c:
        info = c.get("/api/info").json()
        assert not info["modes"]["live"]["available"] and info["payments"] == "simulated provider"
        assert c.post("/api/runs", json={"sample": "bank-lines", "mode": "live"}).status_code == 400
        imp = upload(c, "statement_withdrawal_deposit.xls", ALICE).json()
        runs = [c.post("/api/runs", json={"sample": "bank-lines", "mode": "replay"}).json()["run_id"],
                c.post("/api/runs", json={"sample": "invoices", "mode": "fixtures"}).json()["run_id"],
                c.post("/api/runs", headers=ALICE,
                       json={"import_id": imp["import_id"], "mode": "replay"}).json()["run_id"]]
        for run_id in runs:
            h = ALICE if run_id == runs[-1] else None
            wait_idle(c, run_id, h)
            c.post(f"/api/runs/{run_id}/settle", headers=h or {})
            wait_idle(c, run_id, h)
            assert c.get(f"/api/runs/{run_id}", headers=h or {}).json()["totals"]["total_cost_inr"] == 0
        for run_id in runs:
            meta = workers.load_meta(run_id)
            assert meta["pay_provider"] == "fake"
            for settle in (False, True):
                cmd = workers.command(meta, settle)
                assert ("--replay" in cmd or "--fake" in cmd) and "--no-llm" in cmd
                if settle:
                    assert cmd[cmd.index("--pay-provider") + 1] == "fake"
    assert len(started) >= 6
    for cmd, env in started:
        assert ("--replay" in cmd or "--fake" in cmd) and "--no-llm" in cmd
        assert all(env.get(k) == "" for k in API_KEY_NAMES)  # a .env file can't fill these back in


def test_importer_and_console_never_import_the_ai_client():
    """Reading a file is rules only. The Anthropic client is loaded only by the live decider."""
    import ast
    for f in [ROOT / "settlebot" / "importer.py", *(ROOT / "settlebot" / "web").glob("*.py")]:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not any("anthropic" in m or m.endswith("llm") for m in mods), f.name
