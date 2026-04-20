#!/usr/bin/env python3
"""
Show Aster perpetual and spot wallet balances side by side.

  python balances.py
  python balances.py --merge     # one row per asset (perp max vs spot total)
  python balances.py --perp-only

Requires the same .env as the bot (Pro API V3 or legacy HMAC).
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

load_dotenv()

from aster_client import (  # noqa: E402
    FAPI_BASE,
    SAPI_BASE,
    credentials_ok,
    get,
)
import exchange as ex  # noqa: E402


def _perp_row_mag(b: dict) -> Tuple[float, float, float, float]:
    bal = float(b.get("balance", 0) or 0)
    avail = float(b.get("availableBalance", 0) or 0)
    cross = float(b.get("crossWalletBalance", 0) or 0)
    m = max(abs(bal), abs(avail), abs(cross))
    return bal, avail, cross, m


def _spot_tot(b: dict) -> Tuple[float, float, float]:
    free = float(b.get("free", 0) or 0)
    locked = float(b.get("locked", 0) or 0)
    return free, locked, free + locked


def _print_perp(rows: List[dict]) -> None:
    print()
    print("PERPETUAL (USDT-M futures)")
    print(f"  Endpoint: GET {FAPI_BASE}/fapi/v3/balance")
    print()
    hdr = f"{'Asset':<12} {'Wallet':>22} {'Available':>22} {'Cross wallet':>22}"
    print(hdr)
    print("-" * len(hdr))
    kept = 0
    for b in sorted(rows, key=lambda x: x.get("asset", "")):
        bal, avail, cross, m = _perp_row_mag(b)
        if m <= 0:
            continue
        kept += 1
        print(
            f"{b.get('asset',''):<12} {bal:>22.8f} {avail:>22.8f} {cross:>22.8f}"
        )
    if kept == 0:
        print("(no non-zero balances)")
    print(f"\n  Rows shown: {kept}")


def _print_spot(balances: List[dict]) -> None:
    print()
    print("SPOT")
    print(f"  Endpoint: GET {SAPI_BASE}/api/v3/account")
    print()
    hdr = f"{'Asset':<12} {'Free':>22} {'Locked':>22} {'Total':>22}"
    print(hdr)
    print("-" * len(hdr))
    kept = 0
    for b in sorted(balances, key=lambda x: x.get("asset", "")):
        free, locked, tot = _spot_tot(b)
        if max(abs(free), abs(locked), abs(tot)) <= 0:
            continue
        kept += 1
        print(f"{b.get('asset',''):<12} {free:>22.8f} {locked:>22.8f} {tot:>22.8f}")
    if kept == 0:
        print("(no non-zero balances)")
    print(f"\n  Rows shown: {kept}")


def _print_merged(perp_rows: List[dict], spot_balances: List[dict]) -> None:
    perp_map: Dict[str, float] = {}
    for b in perp_rows:
        a = b.get("asset", "")
        _, _, _, m = _perp_row_mag(b)
        perp_map[a] = m

    spot_map: Dict[str, float] = {}
    for b in spot_balances:
        a = b.get("asset", "")
        _, _, tot = _spot_tot(b)
        spot_map[a] = tot

    assets = sorted(set(perp_map) | set(spot_map))
    print()
    print("COMBINED (per asset)")
    print(
        f"  Perp 'size' = max(|wallet|, |available|, |cross|)  |  "
        "Spot 'total' = free + locked"
    )
    print()
    hdr = f"{'Asset':<12} {'Perp (max leg)':>22} {'Spot (free+locked)':>22}"
    print(hdr)
    print("-" * len(hdr))
    for a in assets:
        p = perp_map.get(a, 0.0)
        s = spot_map.get(a, 0.0)
        if p <= 0 and s <= 0:
            continue
        print(f"{a:<12} {p:>22.8f} {s:>22.8f}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Show Aster perp and spot balances.")
    ap.add_argument(
        "--merge",
        action="store_true",
        help="Print one combined table (perp max column vs spot total per asset)",
    )
    ap.add_argument(
        "--perp-only",
        action="store_true",
        help="Only fetch futures balance",
    )
    args = ap.parse_args()

    if not credentials_ok():
        print(
            "Configure .env: Pro V3 (ASTER_USER, ASTER_SIGNER, ASTER_SIGNER_PRIVATE_KEY) "
            "or legacy (ASTER_API_KEY, ASTER_SECRET_KEY)."
        )
        return 1

    try:
        perp_rows = ex.signed_get("/fapi/v2/balance", {})
    except Exception as e:
        print(f"Perpetual balance failed: {e}")
        return 1

    if not isinstance(perp_rows, list):
        print(f"Unexpected perp response: {perp_rows!r}")
        return 1

    _print_perp(perp_rows)

    if args.perp_only:
        print("\nDone.")
        return 0

    spot_balances: Optional[List[dict]] = None
    try:
        acct: Any = get("/api/v3/account", signed=True, base_url=SAPI_BASE)
        if isinstance(acct, dict):
            spot_balances = acct.get("balances")
    except requests.exceptions.HTTPError as e:
        print()
        print(f"Spot balance failed: {e}")
        resp = getattr(e, "response", None)
        if resp is not None and resp.text and not resp.text.strip().startswith("<"):
            print(f"  Body: {resp.text[:300]}")
        print(
            "\n  Tip: enable Spot on your Pro API agent in Aster API Wallet. "
            "Perp table above is still valid."
        )
    except Exception as e:
        print()
        print(f"Spot balance failed: {e}")

    if spot_balances is not None and isinstance(spot_balances, list):
        _print_spot(spot_balances)
        if args.merge:
            _print_merged(perp_rows, spot_balances)
    else:
        print()
        print("SPOT: not loaded (see errors above).")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
