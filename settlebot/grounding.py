"""A fact is kept only if its quoted passage really appears in the stored source response."""
from __future__ import annotations

import re

_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub(" ", text).strip().casefold()


def passage_in_body(passage: str, body: str) -> bool:
    if not passage or not body:
        return False
    if _norm(passage) in _norm(body):
        return True
    # JSON bodies escape quotes and slashes; compare against an unescaped copy too.
    unescaped = body.replace('\\"', '"').replace("\\/", "/")
    return _norm(passage) in _norm(unescaped)


def name_in_passage(name: str, passage: str) -> bool:
    return bool(name) and _norm(name) in _norm(passage)
