"""Map a verified payee to a ledger account using the chart of accounts in config.yaml."""
from __future__ import annotations

import re
from typing import Any


def map_account(texts: list[str], cfg: dict[str, Any]) -> tuple[str, str, str] | None:
    """Return (code, name, matched_keyword) or None when no rule applies."""
    haystack = " ".join(t for t in texts if t).casefold()
    for acct in cfg.get("chart_of_accounts", []):
        for kw in acct.get("keywords", []):
            if re.search(rf"(?<![a-z0-9]){re.escape(kw.casefold())}(?![a-z0-9])", haystack):
                return str(acct["code"]), acct["name"], kw
    return None


def account_by_code(code: str, cfg: dict[str, Any]) -> tuple[str, str] | None:
    for acct in cfg.get("chart_of_accounts", []):
        if str(acct["code"]) == str(code):
            return str(acct["code"]), acct["name"]
    return None
