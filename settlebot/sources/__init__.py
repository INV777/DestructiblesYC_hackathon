"""Payee sources. Each returns hits that carry a verbatim passage and a stored raw response."""
from __future__ import annotations

from typing import Any

from .base import Source, SourceContext, SourceHit  # noqa: F401


def build_sources(names: list[str], ctx: SourceContext) -> list[Source]:
    from .fake import FakeSource

    registry: dict[str, Any] = {"fake": FakeSource}
    try:
        from .crustdata import CrustdataIdentify, CrustdataWeb
        registry["crustdata_identify"] = CrustdataIdentify
        registry["crustdata_web"] = CrustdataWeb
    except ImportError:  # adapters land in M2
        pass
    out = []
    for n in names:
        if n not in registry:
            raise ValueError(f"unknown source '{n}' in config sources.order")
        out.append(registry[n](ctx))
    return out
