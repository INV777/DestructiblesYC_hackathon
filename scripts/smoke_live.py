#!/usr/bin/env python3
"""Check the three live services with keys from .env, without side effects:
Crustdata identify (about 0.2 credits), one small Claude tool call, and a Razorpay
test-mode order lookup (GET only, creates nothing).

  python scripts/smoke_live.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from settlebot.config import env, load_config, load_dotenv  # noqa: E402


def check(name, fn):
    t0 = time.monotonic()
    try:
        detail = fn()
        print(f"OK    {name:<10} {int((time.monotonic() - t0) * 1000):>5} ms  {detail}")
        return True
    except Exception as e:  # noqa: BLE001 - report every failure plainly
        print(f"FAIL  {name:<10} {type(e).__name__}: {str(e)[:200]}")
        return False


def crustdata(cfg):
    from settlebot.sources.crustdata import CrustdataClient
    data, credits = CrustdataClient(cfg).post("/company/identify", {"names": ["Swiggy"]})
    m = data[0]["matches"][0]
    return f"identify Swiggy -> {m['company_data']['basic_info']['name']} ({credits} credits)"


def claude(cfg):
    import anthropic
    from settlebot.llm import ACCOUNT_TOOL, SYSTEM
    c = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"))
    msg = c.messages.create(model=cfg["llm"]["model"], max_tokens=200, system=SYSTEM,
                            tools=[ACCOUNT_TOOL], tool_choice={"type": "tool", "name": "choose_account"},
                            messages=[{"role": "user", "content": "Payee: Swiggy Limited\nWhat the sources say: Swiggy Limited "
                                                                  "provides a platform to order and pay for food (Food Delivery).\n"
                                                                  "Chart:\n6110: Staff meals\n6420: Cloud hosting\n9999: Suspense\n"
                                                                  "Choose the account using only the text above."}])
    block = next(b for b in msg.content if b.type == "tool_use")
    return (f"model {cfg['llm']['model']} -> account {block.input.get('account_code')} "
            f"({msg.usage.input_tokens} in / {msg.usage.output_tokens} out tokens)")


def razorpay(cfg):
    from settlebot.payments import RazorpayOrders
    rp = RazorpayOrders(cfg)
    found = rp.find_by_receipt("sb_smoke_test_receipt")
    return f"test-mode order lookup works (receipt found: {bool(found)})"


def main() -> int:
    load_dotenv(ROOT / ".env")
    cfg = load_config(ROOT / "config.yaml")
    results = [check("crustdata", lambda: crustdata(cfg)), check("claude", lambda: claude(cfg)),
               check("razorpay", lambda: razorpay(cfg))]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
