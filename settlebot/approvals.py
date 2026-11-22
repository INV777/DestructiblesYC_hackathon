"""Human approval for actions that cannot be undone. Decisions live in the database,
so a pending approval survives a kill and is never asked twice."""
from __future__ import annotations

import json
from typing import Any

from .pii_guard import mask_account
from .store import Store, now_iso


def required_approvers(cfg: dict[str, Any], action: str, amount_inr: float) -> int:
    """The matching rule with the highest threshold wins, so rule order in config doesn't matter."""
    best = None
    for r in cfg.get("approvals", {}).get("rules", []):
        if r.get("action") == action and amount_inr >= float(r.get("min_amount_inr", 0)):
            if best is None or float(r.get("min_amount_inr", 0)) >= float(best.get("min_amount_inr", 0)):
                best = r
    return int(best["approvers"]) if best else 1


def record(store: Store, run_id: str, item_key: str, action: str, approver: str, approve: bool,
           reason: str | None = None) -> None:
    with store.tx() as c:
        c.execute("INSERT OR IGNORE INTO approvals (run_id, item_key, action, approver, decision, reason, decided_at)"
                  " VALUES (?,?,?,?,?,?,?)",
                  (run_id, item_key, action, approver, "APPROVE" if approve else "REJECT", reason, now_iso()))


def state(store: Store, cfg: dict[str, Any], run_id: str, item_key: str, action: str,
          amount_inr: float) -> tuple[str, list[dict]]:
    """('APPROVED' | 'REJECTED' | 'PENDING', approvals so far)."""
    rows = [dict(r) for r in store.q("SELECT * FROM approvals WHERE run_id=? AND item_key=? AND action=? "
                                     "ORDER BY decided_at", (run_id, item_key, action))]
    if any(r["decision"] == "REJECT" for r in rows):
        return "REJECTED", rows
    need = required_approvers(cfg, action, amount_inr)
    distinct = {r["approver"] for r in rows if r["decision"] == "APPROVE"}
    return ("APPROVED" if len(distinct) >= need else "PENDING"), rows


def pending_actions(store: Store, cfg: dict[str, Any], run_id: str) -> list[dict]:
    """Every action waiting for a person: PAY for verified invoices, RELEASE_HOLD for duplicates."""
    out = []
    for it in store.items(run_id):
        raw = json.loads(it["raw_json"])
        amount = float(raw.get("amount_inr", raw.get("amount", 0)))
        action = None
        if it["kind"] == "invoice" and it["status"] == "VERIFIED":
            action = "PAY"
        elif it["status"] == "HELD_DUPLICATE":
            action = "RELEASE_HOLD"
        if not action:
            continue
        st, rows = state(store, cfg, run_id, it["item_key"], action, amount)
        if st == "PENDING":
            out.append({"item": it, "raw": raw, "action": action, "amount_inr": amount,
                        "need": required_approvers(cfg, action, amount), "have": rows})
    return out


def decision_card(p: dict) -> str:
    it, raw = p["item"], p["raw"]
    dec = json.loads(it["decision_json"] or "{}")
    lines = [
        "=" * 72,
        f"{p['action']}  line {it['line_no']}  INR {p['amount_inr']:,.2f}  ({it['kind']}, {raw.get('date')})",
        f"Descriptor : {raw.get('description')}",
        f"Payee      : {dec.get('payee') or '-'}",
        f"Account    : {(dec.get('account_code') or '') + ' ' + (dec.get('account_name') or '')}".rstrip(),
    ]
    if raw.get("bank_account"):
        b = raw["bank_account"]
        lines.append(f"Pay to     : account {mask_account(b.get('account_number'))}, IFSC {b.get('ifsc')}")
    lines.append(f"Why        : {dec.get('explanation', '')}")
    for c in dec.get("citations", [])[:2]:
        lines.append(f"Source     : {c.get('source_url')}  [{c.get('field') or 'passage'}]  "
                     f"retrieved {c.get('retrieved_at')}")
    lines.append(f"Approvals  : {len([h for h in p['have'] if h['decision'] == 'APPROVE'])} of {p['need']}")
    lines.append("=" * 72)
    return "\n".join(lines)
