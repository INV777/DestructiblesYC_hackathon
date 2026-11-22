"""Mask Indian personal and financial identifiers before text leaves the machine or hits a log."""
from __future__ import annotations

import re

_RULES: list[tuple[str, re.Pattern, str]] = [
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[EMAIL]"),
    ("pan", re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b"), "[PAN]"),
    ("aadhaar", re.compile(r"\b[2-9][0-9]{3}\s?[0-9]{4}\s?[0-9]{4}\b"), "[AADHAAR]"),
    ("ifsc", re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b"), "[IFSC]"),
    ("phone", re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)"), "[PHONE]"),
    ("account", re.compile(r"(?<!\d)\d{9,18}(?!\d)"), "[ACCOUNT]"),
    ("upi", re.compile(r"\b[\w.-]{2,}@(?:ok\w+|ybl|ibl|axl|upi|paytm|apl|icici|sbi|hdfcbank)\b", re.I), "[UPI]"),
]


def mask(text: str) -> str:
    if not text:
        return text
    for _, pat, repl in _RULES:
        text = pat.sub(repl, text)
    return text


def mask_account(number: str | None) -> str | None:
    """Show only the last 4 digits, the way a bank statement does."""
    if not number:
        return number
    digits = re.sub(r"\D", "", str(number))
    return ("X" * max(0, len(digits) - 4)) + digits[-4:] if digits else number
