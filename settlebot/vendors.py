"""Vendor master checks for invoices: the payee must be a known vendor, the live legal name
must match the master record, and the bank details must not have changed."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .entities import canonical
from .lookalike import compare_domains, compare_names, email_domain
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


def _hold_fraud(decision: Decision, explanation: str, **extra) -> Decision:
    decision.status = Status.HELD_FRAUD
    decision.missing = []
    decision.extra.update(extra)
    decision.explanation = explanation
    return decision


def lookalike_vendor(name: str | None, master: dict[str, dict[str, Any]], exclude: str | None = None):
    """First master vendor that `name` imitates, as (vendor, reason)."""
    if not name:
        return None
    for v in master.values():
        if v["vendor_id"] == exclude:
            continue
        why = compare_names(v["legal_name"], name)
        if why:
            return v, why
    return None


def check_invoice(item: LineItem, decision: Decision, master: dict[str, dict[str, Any]]) -> Decision:
    """Return the decision to keep for an invoice line (bank lines pass through unchanged)."""
    if item.kind != "invoice" or decision.status == Status.HELD_DUPLICATE:
        return decision
    vendor = master.get(str(item.vendor_id or ""))
    invoice_name = item.raw.get("vendor_name") or item.description

    # Lookalike of a known vendor under an ID the master doesn't have: classic impersonation.
    if vendor is None:
        hit = lookalike_vendor(invoice_name, master) or lookalike_vendor(decision.payee, master)
        if hit:
            v, why = hit
            return _hold_fraud(decision, (
                f"The invoice names '{invoice_name}' under vendor ID '{item.vendor_id}', which is not in the vendor "
                f"master, and the name imitates vendor {v['vendor_id']} '{v['legal_name']}' ({why}). Payment "
                "blocked; confirm with the real vendor through a contact on file."),
                lookalike_of=v["vendor_id"], lookalike_reason=why)

    # Sender domain that imitates the vendor's domain on file.
    sender = email_domain(item.raw.get("sender_email") or item.raw.get("vendor_email") or item.raw.get("sender_domain"))
    if vendor is not None and sender:
        known = [d.lower() for d in vendor.get("domains", [])]
        if known and sender not in known and not any(sender.endswith("." + k) for k in known):
            for k in known:
                why = compare_domains(k, sender)
                if why:
                    return _hold_fraud(decision, (
                        f"The invoice was sent from '{sender}', which imitates {vendor['vendor_id']}'s domain on file "
                        f"'{k}' ({why}). Payment blocked; confirm with the vendor through a contact on file."),
                        lookalike_domain=sender, domain_on_file=k, lookalike_reason=why)
            decision.extra["sender_domain_not_on_file"] = sender

    if decision.status != Status.VERIFIED:
        return decision
    if vendor is None:
        same = [v for v in master.values() if canonical(v["legal_name"]).replace(" LIMITED", "")
                == canonical(invoice_name or "").replace(" LIMITED", "")]
        decision.status = Status.CANNOT_VERIFY
        decision.missing = [f"Vendor '{item.vendor_id}' is not in the vendor master."]
        if same:
            decision.missing.append(f"The name matches vendor {same[0]['vendor_id']} '{same[0]['legal_name']}'; "
                                    "check whether this is the same vendor under a new ID before paying.")
        decision.explanation = "Unknown vendor. Held for a person."
        return decision
    if canonical(vendor["legal_name"]) != canonical(decision.payee or ""):
        why = compare_names(vendor["legal_name"], decision.payee or "")
        decision.status = Status.CANNOT_VERIFY
        decision.missing = [f"Live source names '{decision.payee}', but the vendor master says "
                            f"'{vendor['legal_name']}'" + (f" ({why})." if why else ".")]
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
