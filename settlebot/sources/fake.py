"""Fixture-backed source for tests and kill tests only. Never used for a real decision."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .base import Fetched, Source, SourceHit

DEFAULT_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "fake_sources.json"


class FakeSource(Source):
    name = "fake"
    provider = "fake"

    def __init__(self, ctx):
        super().__init__(ctx)
        path = ctx.options.get("fake_fixture", DEFAULT_FIXTURE)
        self.records: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))

    def lookup(self, query, *, item_key, step, attempt):
        merchant = query["merchant"].upper()
        rec = self.records.get(merchant)

        def call() -> Fetched:
            slow = float(self.ctx.options.get("slow", 0))
            if slow:
                time.sleep(slow)
            body = json.dumps(rec or {"results": []}, ensure_ascii=False, sort_keys=True)
            return Fetched(body=body, url=(rec or {}).get("url", f"fake://search?q={merchant}"),
                           record_date=(rec or {}).get("record_date"))

        ev = self.fetch(f"merchant={merchant}", call, item_key=item_key, step=step, attempt=attempt, op="lookup")
        if not rec:
            return [], [f"fake: no record for '{merchant}'"]
        hit = SourceHit(
            source=self.name, url=ev["url"], legal_name=rec["legal_name"], passage=rec["passage"],
            field=rec.get("field"), record_date=ev["record_date"], retrieved_at=ev["retrieved_at"],
            evidence_id=ev["evidence_id"], domain=rec.get("domain"),
            extra={"category_hint": rec.get("category_hint")},
        )
        return [hit], []
