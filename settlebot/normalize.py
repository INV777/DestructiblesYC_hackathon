"""Turn a raw bank descriptor such as 'RAZ*SWIGGY BLR' into a merchant query."""
from __future__ import annotations

import re
from typing import Any

from .models import LineItem, sha256

_SPACES = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^A-Z0-9&.\- ]")


def clean(text: str) -> str:
    return _SPACES.sub(" ", text.upper()).strip()


def normalize_descriptor(desc: str, cfg: dict[str, Any]) -> dict[str, Any]:
    dcfg = cfg.get("descriptor", {})
    prefixes: dict[str, str] = dcfg.get("processor_prefixes", {})
    cities = {c.upper() for c in dcfg.get("city_tokens", [])}

    text = clean(desc)
    processor = None
    for prefix, name in sorted(prefixes.items(), key=lambda kv: -len(kv[0])):
        if text.startswith(prefix.upper()):
            processor = name
            text = text[len(prefix):].strip()
            break

    tokens = _NON_WORD.sub(" ", text).split()
    city = None
    while len(tokens) > 1 and tokens[-1] in cities:
        city = tokens.pop()
    merchant = " ".join(tokens).strip()
    return {"merchant": merchant, "processor": processor, "city": city, "descriptor_norm": clean(desc)}


def fingerprint(item: LineItem, descriptor_norm: str) -> str:
    """Same date + same descriptor + same amount => same fingerprint (possible duplicate)."""
    return sha256(f"{item.kind}|{item.date}|{descriptor_norm}|{item.amount_paise}")[:32]
