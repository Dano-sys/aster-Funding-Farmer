#!/usr/bin/env python3
"""Print Aster SPOT wallet balances (GET /api/v3/account). Same .env as the bot."""

from __future__ import annotations

import sys

from dotenv import load_dotenv

load_dotenv()

from aster_client import SAPI_BASE, credentials_ok, get  # noqa: E402


def main() -> int:
    if not credentials_ok():
        print("Set Pro API V3 or legacy keys in .env (see .env.example).")
        return 1

    try:
        acct = get("/api/v3/account", signed=True, base_url=SAPI_BASE)
    except Exception as e:
        print(f"Spot account request failed: {e}")
        return 1

    balances = acct.get("balances") if isinstance(acct, dict) else None
    if not isinstance(balances, list):
        print(f"Unexpected response: {acct!r}")
        return 1

    print("Aster SPOT balances")
    print(f"  {SAPI_BASE}/api/v3/account")
    print()
    hdr = f"{'Asset':<12} {'Free':>22} {'Locked':>22} {'Total':>22}"
    print(hdr)
    print("-" * len(hdr))

    n = 0
    for b in sorted(balances, key=lambda x: x.get("asset", "")):
        asset = b.get("asset", "")
        free = float(b.get("free", 0) or 0)
        locked = float(b.get("locked", 0) or 0)
        tot = free + locked
        if tot <= 0:
            continue
        n += 1
        print(f"{asset:<12} {free:>22.8f} {locked:>22.8f} {tot:>22.8f}")

    if n == 0:
        print("(all zero balances)")
    print(f"\nNon-zero assets: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
