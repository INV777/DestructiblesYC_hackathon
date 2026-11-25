"""Durable state in SQLite (WAL). Every state change is one transaction, so a kill
at any moment leaves the run either before or after a step, never half-way."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    input_sha TEXT NOT NULL,
    input_path TEXT,
    started_at TEXT NOT NULL,
    status TEXT NOT NULL,              -- RUNNING | DONE | LIMIT_REACHED
    elapsed_seconds REAL NOT NULL DEFAULT 0,
    config_json TEXT
);
CREATE TABLE IF NOT EXISTS items (
    run_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    kind TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    fingerprint TEXT,
    status TEXT NOT NULL,
    decision_json TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, item_key)
);
CREATE TABLE IF NOT EXISTS steps (
    run_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    step TEXT NOT NULL,
    status TEXT NOT NULL,              -- STARTED | DONE
    attempts INTEGER NOT NULL DEFAULT 0,
    output_json TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, item_key, step)
);
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,      -- sha256(run|source|query|body)[:24]
    run_id TEXT NOT NULL,
    source TEXT NOT NULL,
    query_key TEXT NOT NULL,           -- cache key: same source + query reuses the response
    url TEXT,
    retrieved_at TEXT NOT NULL,
    record_date TEXT,
    body_sha256 TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS evidence_cache ON evidence (run_id, source, query_key);
CREATE TABLE IF NOT EXISTS journal (
    run_id TEXT NOT NULL,
    entry_id TEXT NOT NULL UNIQUE,     -- one posting per item, ever
    item_key TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    date TEXT,
    account_code TEXT NOT NULL,
    account_name TEXT,
    payee TEXT,
    amount_paise INTEGER NOT NULL,
    posted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    run_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    action TEXT NOT NULL,              -- PAY | RELEASE_HOLD
    approver TEXT NOT NULL,
    decision TEXT NOT NULL,            -- APPROVE | REJECT
    reason TEXT,
    decided_at TEXT NOT NULL,
    PRIMARY KEY (run_id, item_key, action, approver)
);
CREATE TABLE IF NOT EXISTS payments (
    run_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    receipt TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,              -- IN_FLIGHT | CREATED | FAILED
    provider TEXT NOT NULL,
    provider_id TEXT,
    amount_paise INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, item_key)
);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    item_key TEXT,
    step TEXT,
    attempt INTEGER,
    provider TEXT NOT NULL,            -- crustdata | anthropic | razorpay | web | fake
    op TEXT NOT NULL,
    units REAL NOT NULL DEFAULT 0,
    unit_type TEXT,                    -- credits | input_tokens+output_tokens | calls
    cost_inr REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    ok INTEGER NOT NULL,
    cache_hit INTEGER NOT NULL DEFAULT 0,
    detail TEXT,
    ts TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: str | Path = "state.db"):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        """Release the file (Windows can't delete an open SQLite file)."""
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def q(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    # ---- runs ---------------------------------------------------------------
    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM runs WHERE run_id=?", (run_id,))

    def create_run(self, run_id: str, input_sha: str, input_path: str, cfg: dict) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO runs (run_id, input_sha, input_path, started_at, status, config_json)"
                " VALUES (?,?,?,?,?,?)",
                (run_id, input_sha, input_path, now_iso(), "RUNNING", json.dumps(cfg, default=str)),
            )

    def set_run_status(self, run_id: str, status: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE runs SET status=? WHERE run_id=?", (status, run_id))

    def add_elapsed(self, run_id: str, seconds: float) -> None:
        with self.tx() as c:
            c.execute("UPDATE runs SET elapsed_seconds = elapsed_seconds + ? WHERE run_id=?", (seconds, run_id))

    # ---- items --------------------------------------------------------------
    def upsert_item(self, run_id: str, item_key: str, line_no: int, kind: str, raw: dict) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO items (run_id, item_key, line_no, kind, raw_json, status, updated_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (run_id, item_key, line_no, kind, json.dumps(raw, ensure_ascii=False), "PENDING", now_iso()),
            )

    def items(self, run_id: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM items WHERE run_id=? ORDER BY line_no", (run_id,))

    # ---- steps --------------------------------------------------------------
    def step_output(self, run_id: str, item_key: str, step: str) -> dict | None:
        row = self.one(
            "SELECT output_json FROM steps WHERE run_id=? AND item_key=? AND step=? AND status='DONE'",
            (run_id, item_key, step),
        )
        return json.loads(row["output_json"]) if row else None

    def begin_step(self, run_id: str, item_key: str, step: str) -> int:
        """Record that a step started; returns the attempt number (1, 2, ...)."""
        with self.tx() as c:
            c.execute(
                "INSERT INTO steps (run_id, item_key, step, status, attempts, updated_at) VALUES (?,?,?,?,1,?)"
                " ON CONFLICT(run_id, item_key, step) DO UPDATE SET attempts = attempts + 1, updated_at=excluded.updated_at",
                (run_id, item_key, step, "STARTED", now_iso()),
            )
            row = c.execute(
                "SELECT attempts FROM steps WHERE run_id=? AND item_key=? AND step=?", (run_id, item_key, step)
            ).fetchone()
            return int(row["attempts"])

    def finish_step(
        self,
        run_id: str,
        item_key: str,
        step: str,
        output: dict,
        effects: list[tuple[str, tuple]] | None = None,
    ) -> None:
        """Mark a step DONE and apply its local side effects in the same transaction."""
        with self.tx() as c:
            for sql, params in effects or []:
                c.execute(sql, params)
            c.execute(
                "UPDATE steps SET status='DONE', output_json=?, updated_at=? WHERE run_id=? AND item_key=? AND step=?",
                (json.dumps(output, ensure_ascii=False, default=str), now_iso(), run_id, item_key, step),
            )

    # ---- evidence (also the source cache) -----------------------------------
    def cached_evidence(self, run_id: str, source: str, query_key: str) -> sqlite3.Row | None:
        return self.one(
            "SELECT * FROM evidence WHERE run_id=? AND source=? AND query_key=? ORDER BY retrieved_at DESC LIMIT 1",
            (run_id, source, query_key),
        )

    def evidence(self, evidence_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,))

    def save_evidence(self, row: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO evidence (evidence_id, run_id, source, query_key, url, retrieved_at,"
                " record_date, body_sha256, body) VALUES (:evidence_id,:run_id,:source,:query_key,:url,"
                ":retrieved_at,:record_date,:body_sha256,:body)",
                row,
            )

    # ---- ledger -------------------------------------------------------------
    def ledger_add(self, row: dict[str, Any]) -> None:
        row = {"item_key": None, "step": None, "attempt": None, "units": 0, "unit_type": None,
               "cost_inr": 0.0, "latency_ms": None, "cache_hit": 0, "detail": None, "ts": now_iso(), **row}
        with self.tx() as c:
            c.execute(
                "INSERT INTO ledger (run_id, item_key, step, attempt, provider, op, units, unit_type, cost_inr,"
                " latency_ms, ok, cache_hit, detail, ts) VALUES (:run_id,:item_key,:step,:attempt,:provider,:op,"
                ":units,:unit_type,:cost_inr,:latency_ms,:ok,:cache_hit,:detail,:ts)",
                row,
            )

    def total_cost(self, run_id: str) -> float:
        row = self.one("SELECT COALESCE(SUM(cost_inr),0) AS c FROM ledger WHERE run_id=?", (run_id,))
        return float(row["c"])

    def export_ledger_jsonl(self, run_id: str, path: str | Path) -> None:
        rows = self.q("SELECT * FROM ledger WHERE run_id=? ORDER BY id", (run_id,))
        tmp = Path(str(path) + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(dict(r), ensure_ascii=False) + "\n")
        tmp.replace(path)  # atomic rename: the file is never half-written


def monotonic() -> float:
    return time.monotonic()
