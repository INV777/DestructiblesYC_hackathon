"""Lookalike detection for vendor names and email/web domains.

Inspired by dnstwist (github.com/elceef/dnstwist, Apache-2.0), which generates typo
permutations of a known domain. This module works the other way round: given a known
value and a candidate, it names the edit that turns one into the other, so the hold
reason can say exactly what is wrong ("one letter dropped", "0 used for o"). No dnstwist
code is copied; the fuzzer categories and the homoglyph idea are what we borrowed.
"""
from __future__ import annotations

import re
import unicodedata

from .entities import canonical

# Visual confusables, folded to one canonical letter (multi-char first).
_MULTI = [("rn", "m"), ("vv", "w"), ("cl", "d"), ("lh", "h"), ("lc", "k")]
_SINGLE = str.maketrans({"0": "o", "1": "l", "i": "l", "3": "e", "5": "s", "8": "b", "q": "g", "u": "v"})
_CYRILLIC = str.maketrans({"а": "a", "ь": "b", "с": "c", "ԁ": "d", "е": "e", "ԍ": "g", "һ": "h", "і": "i",
                           "ј": "j", "к": "k", "ӏ": "l", "м": "m", "о": "o", "р": "p", "ԛ": "q", "ѕ": "s",
                           "т": "t", "ѵ": "v", "ԝ": "w", "х": "x", "у": "y"})
_VOWELS = set("aeiou")
_LEGAL_FORMS = {"LIMITED", "PRIVATE", "LLP", "SARL", "LLC", "INC", "GMBH", "PLC", "CORPORATION", "CORP",
                "PTE", "PTY", "SDN", "BHD", "AG", "SE", "OPC", "COMPANY", "CO"}


def skeleton(text: str) -> str:
    """Fold a string to how it looks: lowercase, Cyrillic to Latin, confusables merged."""
    s = unicodedata.normalize("NFKC", text).lower().translate(_CYRILLIC)
    for a, b in _MULTI:
        s = s.replace(a, b)
    return s.translate(_SINGLE)


def _dl_edit(a: str, b: str) -> tuple[int, str]:
    """Damerau-Levenshtein distance, plus the kind of the single edit when distance is 1."""
    n, m = len(a), len(b)
    d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    dist = d[n][m]
    kind = ""
    if dist == 1:
        if n == m:
            diff = [i for i in range(n) if a[i] != b[i]]
            if len(diff) == 2 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]:
                kind = "two letters swapped"
            elif len(diff) == 1:
                x, y = a[diff[0]], b[diff[0]]
                kind = "one vowel changed" if x in _VOWELS and y in _VOWELS else f"'{x}' changed to '{y}'"
        elif m == n - 1:
            kind = "one letter dropped"
        elif m == n + 1:
            extra = next((b[i] for i in range(m) if i >= n or a[i] != b[i]), "")
            kind = "hyphen added" if extra == "-" else "one letter added"
    return dist, kind


def compare_domains(known: str, candidate: str) -> str | None:
    """Return why `candidate` imitates `known`, or None if it is the same or unrelated."""
    known, candidate = known.lower().strip().rstrip("."), candidate.lower().strip().rstrip(".")
    if not known or not candidate or known == candidate or candidate.endswith("." + known):
        return None
    k_label, _, k_tld = known.partition(".")
    c_label, _, c_tld = candidate.partition(".")
    if c_label == k_label and c_tld != k_tld:
        return f"same name with a different ending (.{c_tld} instead of .{k_tld})"
    if c_label.replace("-", "") == k_label.replace("-", "") and c_label != k_label:
        return "hyphen added or removed"
    if skeleton(c_label) == skeleton(k_label) and c_label != k_label:
        return "look-alike characters (e.g. 0 for o, rn for m, or Cyrillic letters)"
    dist, kind = _dl_edit(k_label, c_label)
    if dist == 1:
        return kind or "one character different"
    if len(k_label) >= 5 and k_label in c_label and c_label != k_label:
        return f"the real name with extra words ('{c_label}')"
    return None


def core_name(name: str) -> str:
    """The name without legal forms: 'Tarangini Freight Private Limited' -> 'TARANGINI FREIGHT'."""
    return " ".join(t for t in canonical(name).split() if t not in _LEGAL_FORMS)


def compare_names(known: str, candidate: str) -> str | None:
    """Return why a vendor name imitates a known vendor, or None if same or unrelated."""
    ck, cc = canonical(known), canonical(candidate)
    if not ck or not cc or ck == cc:
        return None
    k_core = [t for t in ck.split() if t not in _LEGAL_FORMS]
    c_core = [t for t in cc.split() if t not in _LEGAL_FORMS]
    if k_core and k_core == c_core:
        k_form = [t for t in ck.split() if t in _LEGAL_FORMS]
        c_form = [t for t in cc.split() if t in _LEGAL_FORMS]
        if k_form and c_form and k_form != c_form:
            return "same name, different legal form (a different legal entity)"
        return None  # same name; one side just omits the legal form
    k_join, c_join = "".join(k_core).lower(), "".join(c_core).lower()
    if len(k_join) < 4:
        return None
    if skeleton(c_join) == skeleton(k_join):
        return "look-alike characters in the name"
    dist, kind = _dl_edit(k_join, c_join)
    limit = 1 if len(k_join) < 8 else 2
    if 1 <= dist <= limit:
        return kind or f"{dist} letters different"
    return None


def email_domain(value: str | None) -> str | None:
    if not value:
        return None
    m = re.search(r"@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})", value)
    if m:
        return m.group(1).lower()
    return value.lower().strip() if "." in value and " " not in value else None
