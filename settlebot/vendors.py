"""Vendor master checks for invoices: the payee must be a known vendor, the live legal name
must match the master record, and the bank details must not have changed."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .entities import canonical
from .models import Decision, LineItem, Status
from .pii_guard import mask_account


def load_vendor_master(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    path = cfg.get("vendor_master")
    if not path or not Path(path).exists():
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data.get("vendors", data) if isinstance(data, dict) else data
    return {str(v["vendor_id"]): v for v in rows}


def _acct(a: dict[str, Any] | None) -> tuple[str, str]:
    a = a or {}
    return re.sub(r"\D", "", str(a.get("account_number", ""))), str(a.get("ifsc", "")).upper().strip()


def check_invoice(item: LineItem, decision: Decision, master: dict[str, dict[str, Any]]) -> Decision:
    """Return the decision to keep for an invoice line (bank lines pass through unchanged)."""
    if item.kind != "invoice" or decision.status != Status.VERIFIED:
        return decision
    vendor = master.get(str(item.vendor_id or ""))
    if vendor is None:
        decision.status = Status.CANNOT_VERIFY
        decision.missing = [f"Vendor '{item.vendor_id}' is not in the vendor master."]
        decision.explanation = "Unknown vendor. Held for a person."
        return decision
    if canonical(vendor["legal_name"]) != canonical(decision.payee or ""):
        decision.status = Status.CANNOT_VERIFY
        decision.missing = [f"Live source names '{decision.payee}', but the vendor master says "
                            f"'{vendor['legal_name']}'."]
        decision.explanation = "The live record does not match the vendor master. Held for a person."
        return decision
    got, want = _acct(item.bank_account), _acct(vendor.get("bank_account"))
    if not got[0] or got != want:
        decision.status = Status.HELD_FRAUD
        decision.missing = []
        decision.extra["bank_change"] = {
            "invoice": {"account": mask_account(got[0]), "ifsc": got[1]},
            "vendor_master": {"account": mask_account(want[0]), "ifsc": want[1]},
        }
        decision.explanation = (f"Bank details on this invoice (account {mask_account(got[0]) or 'missing'}, "
                                f"IFSC {got[1] or 'missing'}) differ from the vendor master "
                                f"(account {mask_account(want[0])}, IFSC {want[1]}). Payment blocked; confirm "
                                "the change with the vendor through a contact on file, not the invoice.")
        return decision
    decision.extra["vendor_id"] = vendor["vendor_id"]
    decision.explanation += f" Vendor {vendor['vendor_id']} found in the vendor master; bank details match."
    return decision
