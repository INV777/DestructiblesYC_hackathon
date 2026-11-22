"""Core data types. Plain dataclasses so the only runtime deps stay small."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Status(str, Enum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"              # payee and account backed by sources
    CANNOT_VERIFY = "CANNOT_VERIFY"    # no source settles it; held for a person
    HELD_DUPLICATE = "HELD_DUPLICATE"  # same date, descriptor and amount as an earlier line
    HELD_FRAUD = "HELD_FRAUD"          # bank details differ from the vendor master
    REJECTED = "REJECTED"              # a person rejected the action


HELD_STATUSES = {Status.CANNOT_VERIFY, Status.HELD_DUPLICATE, Status.HELD_FRAUD}


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class LineItem:
    """One bank line or invoice from the input batch."""
    line_no: int
    kind: str                      # "bank_line" | "invoice"
    date: str
    description: str
    amount_inr: float
    raw: dict[str, Any]
    vendor_id: str | None = None   # invoices: key into the vendor master
    invoice_no: str | None = None
    bank_account: dict[str, Any] | None = None  # invoices: {account_number, ifsc, name}

    @property
    def amount_paise(self) -> int:
        return int(round(self.amount_inr * 100))

    def item_key(self, input_sha: str) -> str:
        # Position-based, so two identical lines get two different keys.
        return sha256(f"{input_sha}:{self.line_no}")[:32]

    @classmethod
    def from_dict(cls, line_no: int, d: dict[str, Any]) -> "LineItem":
        amount = d.get("amount_inr", d.get("amount"))
        if amount is None:
            raise ValueError(f"line {line_no}: missing amount_inr")
        kind = d.get("kind") or ("invoice" if ("invoice_no" in d or "vendor_id" in d) else "bank_line")
        return cls(
            line_no=line_no,
            kind=kind,
            date=str(d.get("date", "")),
            description=str(d.get("description", d.get("narration", ""))).strip(),
            amount_inr=float(amount),
            raw=d,
            vendor_id=d.get("vendor_id"),
            invoice_no=d.get("invoice_no"),
            bank_account=d.get("bank_account"),
        )


@dataclass
class Citation:
    source: str              # e.g. "crustdata.company_identify", "web_fetch"
    source_url: str
    field_or_passage: str    # exact field path or verbatim passage that states the fact
    record_date: str | None  # date of the record itself, if the source gives one
    retrieved_at: str        # when we fetched it during this run
    evidence_id: str         # row in the evidence table holding the raw response


@dataclass
class Decision:
    status: Status
    payee: str | None = None
    account_code: str | None = None
    account_name: str | None = None
    explanation: str = ""
    citations: list[Citation] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    related_line: int | None = None   # for duplicates: the earlier line
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Decision":
        d = dict(d)
        d["status"] = Status(d["status"])
        d["citations"] = [Citation(**c) for c in d.get("citations", [])]
        return cls(**d)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)
