"""Find legal-entity names in text, and decide whether a name is backed by the descriptor.

Conservative on purpose: a candidate is kept only when the text ties it to the bank
descriptor, either as a defined term (Amazon Web Services EMEA SARL ("AWS EMEA")) or by
containing every word of the merchant (Swiggy Limited for SWIGGY). A person's name, the
payment processor, or a lookalike never qualifies, so the hard cases end in cannot verify.
"""
from __future__ import annotations

import re

SUFFIX = (r"(?:Private\s+Limited|Pvt\.?\s+Ltd\.?|Limited|Ltd\.?|LLP|S\.?A\.?R\.?L\.?|LLC|Inc\.?|GmbH|"
          r"B\.V\.|S\.A\.|PLC|Plc|Corporation|Corp\.?|Pte\.?\s+Ltd\.?|AG|SE|N\.V\.)")
_WORD = r"[A-Z0-9][\w&.\-']*"
_LEADING_JUNK = {"the", "name", "of", "listed", "entity", "legal", "company", "registered", "for", "read",
                 "by", "and", "from", "to", "is", "a", "an", "our", "by", "with", "about"}
_TOKEN = re.compile(r"[A-Za-z0-9]+")


def tokens(text: str) -> list[str]:
    return [t.upper() for t in _TOKEN.findall(text)]


def _trim(name: str) -> str:
    words = name.split()
    while len(words) > 2 and words[0].casefold() in _LEADING_JUNK:
        words.pop(0)
    return " ".join(words).strip(" ,;:-")


def defined_term_names(text: str, merchant: str) -> list[str]:
    """Names introduced as the merchant's defined term: NAME ("MERCHANT") or NAME (MERCHANT)."""
    m = re.escape(merchant)
    pat = re.compile(rf"((?:{_WORD}\s+){{0,7}}?{SUFFIX})\s*\(\s*[\"“”'‘’]?\s*{m}\s*[\"“”'‘’]?\s*\)", re.IGNORECASE)
    return [_trim(x.group(1)) for x in pat.finditer(text)]


def token_names(text: str, merchant: str) -> list[str]:
    """Names that start with the merchant's first word and contain all of its words."""
    mt = tokens(merchant)
    if not mt:
        return []
    first = re.escape(mt[0])
    pat = re.compile(rf"\b({first}(?:\s+{_WORD}){{0,6}}?\s+{SUFFIX})(?![\w])", re.IGNORECASE)
    out = []
    for x in pat.finditer(text):
        name = _trim(x.group(1))
        nt = set(tokens(name))
        if all(t in nt for t in mt):
            out.append(name)
    return out


def corroborates(merchant: str, legal_name: str, passage: str) -> bool:
    """True when the passage ties legal_name to the descriptor's merchant."""
    if not merchant or not legal_name or not passage:
        return False
    norm = lambda s: re.sub(r"\s+", " ", s).casefold()  # noqa: E731
    if norm(legal_name) not in norm(passage):
        return False
    if any(norm(n) == norm(legal_name) for n in defined_term_names(passage, merchant)):
        return True
    nt = set(tokens(legal_name))
    return all(t in nt for t in tokens(merchant))


def canonical(name: str) -> str:
    """Comparison key so 'Swiggy Ltd.' and 'SWIGGY LIMITED' count as one payee."""
    s = name.upper()
    s = re.sub(r"\bPVT\.?\s+LTD\.?", "PRIVATE LIMITED", s)
    s = re.sub(r"\bLTD\.?(?=\s|$)", "LIMITED", s)
    s = re.sub(r"\bS\.?A\.?R\.?L\.?(?=\s|$)", "SARL", s)
    return " ".join(tokens(s))
