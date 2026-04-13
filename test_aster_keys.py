#!/usr/bin/env python3
"""
Test Aster API credentials and print a breakdown of:
  • Perpetual (USDT-M) wallet — /fapi/v3/balance
  • Spot wallet — /api/v3/account

Rows are split into material vs dust using a size threshold (token units; not USD).
Uses the same auth as aster_client (Pro API V3 or legacy HMAC).

  python test_aster_keys.py
  python test_aster_keys.py --material 0.01
  python test_aster_keys.py --no-spot          # skip spot if you only care about perp
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, List, Tuple

import requests
from dotenv import load_dotenv

load_dotenv()

from aster_client import (  # noqa: E402
    ASTER_SIGNER,
    ASTER_USER,
    FAPI_BASE,
    SAPI_BASE,
    USE_LEGACY,
    USE_V3,
    credentials_ok,
    get,
)

ASTER_COLLATERAL_RATIO = 0.80
USDF_COLLATERAL_RATIO = 0.9999


def _mask_addr(a: str) -> str:
    a = (a or "").strip()
    if len(a) < 12:
        return a or "(empty)"
    return f"{a[:6]}...{a[-4:]}"


def _perp_mag(row: dict) -> Tuple[float, float, float, float]:
    """Returns (wallet, available, cross, max_abs)."""
    bal = float(row.get("balance", 0) or 0)
    avail = float(row.get("availableBalance", 0) or 0)
    cross = float(row.get("crossWalletBalance", 0) or 0)
    m = max(abs(bal), abs(avail), abs(cross))
    return bal, avail, cross, m


def _spot_total(row: dict) -> Tuple[float, float, float]:
    """Returns (free, locked, total)."""
    free = float(row.get("free", 0) or 0)
    locked = float(row.get("locked", 0) or 0)
    return free, locked, free + locked


def _print_perp_section(rows: List[dict], material_thr: float, max_dust: int) -> None:
    print()
    print("=" * 78)
    print("PERPETUAL (futures) wallet  —  GET /fapi/v3/balance")
    print("=" * 78)

    parsed: List[Tuple[dict, float]] = []
    for b in rows:
        _, _, _, m = _perp_mag(b)
        parsed.append((b, m))
    parsed.sort(key=lambda x: x[1], reverse=True)

    material: List[Tuple[dict, float]] = []
    dust: List[Tuple[dict, float]] = []
    zeros = 0
    for b, m in parsed:
        if m <= 0:
            zeros += 1
        elif m >= material_thr:
            material.append((b, m))
        else:
            dust.append((b, m))

    hdr = f"{'Asset':<10} {'Wallet':>18} {'Available':>18} {'Cross':>18} {'max|·|':>14}"
    print()
    print(f"Material  (max column ≥ {material_thr:g})  —  {len(material)} assets")
    print(hdr)
    print("-" * 78)
    for b, _ in material:
        bal, avail, cross, m = _perp_mag(b)
        print(
            f"{b.get('asset',''):<10} {bal:>18.8f} {avail:>18.8f} {cross:>18.8f} {m:>14.8f}"
        )
    if not material:
        print("(none)")

    print()
    print(
        f"Dust / small  (0 < max column < {material_thr:g})  —  {len(dust)} assets"
    )
    print(hdr)
    print("-" * 78)
    shown = 0
    for b, m in dust:
        if shown >= max_dust:
            print(f"... ({len(dust) - max_dust} more rows not shown; increase --max-dust)")
            break
        bal, avail, cross, _ = _perp_mag(b)
        print(
            f"{b.get('asset',''):<10} {bal:>18.8f} {avail:>18.8f} {cross:>18.8f} {m:>14.8f}"
        )
        shown += 1
    if not dust:
        print("(none)")

    print()
    print(f"All-zero rows (wallet / available / cross all ~0):  {zeros}")


def _print_spot_section(balances: List[dict], material_thr: float, max_dust: int) -> None:
    print()
    print("=" * 78)
    print("SPOT wallet  —  GET /api/v3/account  (balances[])")
    print("=" * 78)

    parsed: List[Tuple[dict, float, float, float]] = []
    for b in balances:
        free, locked, total = _spot_total(b)
        mag = max(abs(free), abs(locked), abs(total))
        parsed.append((b, free, locked, mag))
    parsed.sort(key=lambda x: x[3], reverse=True)

    material: List[Tuple[dict, float, float, float]] = []
    dust: List[Tuple[dict, float, float, float]] = []
    zeros = 0
    for b, free, locked, mag in parsed:
        if mag <= 0:
            zeros += 1
        elif mag >= material_thr:
            material.append((b, free, locked, mag))
        else:
            dust.append((b, free, locked, mag))

    hdr = f"{'Asset':<10} {'Free':>20} {'Locked':>20} {'Total':>20}"
    print()
    print(f"Material  (max ≥ {material_thr:g})  —  {len(material)} assets")
    print(hdr)
    print("-" * 78)
    for b, free, locked, mag in material:
        print(
            f"{b.get('asset',''):<10} {free:>20.8f} {locked:>20.8f} {free + locked:>20.8f}"
        )
    if not material:
        print("(none)")

    print()
    print(f"Dust / small  (0 < max < {material_thr:g})  —  {len(dust)} assets")
    print(hdr)
    print("-" * 78)
    shown = 0
    for b, free, locked, mag in dust:
        if shown >= max_dust:
            print(f"... ({len(dust) - max_dust} more rows not shown; increase --max-dust)")
            break
        print(
            f"{b.get('asset',''):<10} {free:>20.8f} {locked:>20.8f} {free + locked:>20.8f}"
        )
        shown += 1
    if not dust:
        print("(none)")

    print()
    print(f"All-zero spot balances:  {zeros}")


def _effective_margin_usd(rows: List[dict]) -> float:
    total = 0.0
    for b in rows:
        asset = b.get("asset", "")
        try:
            bal = float(b.get("balance", 0) or 0)
        except (TypeError, ValueError):
            continue
        if asset == "ASTER":
            total += bal * ASTER_COLLATERAL_RATIO
        elif asset == "USDF":
            total += bal * USDF_COLLATERAL_RATIO
        elif asset == "USDT":
            total += bal
    return total


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Test Aster API keys; show perp + spot balance breakdown (material vs dust)."
    )
    ap.add_argument(
        "--material",
        type=float,
        default=0.01,
        metavar="THR",
        help="Threshold in raw token units: rows below this (but >0) are 'dust/small' (default 0.01)",
    )
    ap.add_argument(
        "--max-dust",
        type=int,
        default=80,
        help="Max rows to print in each dust section (default 80)",
    )
    ap.add_argument(
        "--no-spot",
        action="store_true",
        help="Only fetch perpetual balances",
    )
    args = ap.parse_args()

    print("Aster API — balance breakdown")
    print(f"  Futures REST: {FAPI_BASE}")
    print(f"  Spot REST:    {SAPI_BASE}")

    if USE_V3:
        print("  Auth: Pro API V3 (EIP-712)")
        print(f"    user:   {_mask_addr(ASTER_USER)}")
        print(f"    signer: {_mask_addr(ASTER_SIGNER)}")
    elif USE_LEGACY:
        print("  Auth: legacy HMAC (ASTER_API_KEY + ASTER_SECRET_KEY)")
    else:
        print("  Auth: none configured")
        print()
        print(
            "Set in .env either:\n"
            "  Pro API V3: ASTER_USER, ASTER_SIGNER, ASTER_SIGNER_PRIVATE_KEY\n"
            "  or legacy:  ASTER_API_KEY, ASTER_SECRET_KEY"
        )
        return 1

    if not credentials_ok():
        return 1

    print()

    # --- connectivity ---
    try:
        t = get("/fapi/v1/time", signed=False)
        print(f"OK  futures public  /fapi/v3/time  serverTime_ms={t.get('serverTime')}")
    except Exception as e:
        print(f"FAIL  futures /time  {e}")
        return 1

    if not args.no_spot:
        try:
            st = get("/api/v3/time", signed=False, base_url=SAPI_BASE)
            print(f"OK  spot public     /api/v3/time  serverTime_ms={st.get('serverTime')}")
        except Exception as e:
            print(f"WARN  spot /time  {e}  (continuing; spot signed calls may fail too)")

    # --- perp balances ---
    try:
        rows = get("/fapi/v2/balance", signed=True)
    except Exception as e:
        print(f"FAIL  signed  GET /fapi/v3/balance  {e}")
        print("  Check keys, agent registration (Pro API), and futures permission.")
        return 1

    if not isinstance(rows, list):
        print(f"Unexpected perp balance response: {rows!r}")
        return 1

    eff = _effective_margin_usd(rows)
    _print_perp_section(rows, args.material, args.max_dust)
    print()
    print(
        "Approx effective futures margin (bot formula: USDT + USDF×99.99% + ASTER×80% "
        f"on wallet balance):  ${eff:,.2f}"
    )

    if args.no_spot:
        print()
        print("OK  (spot skipped)")
        return 0

    # --- spot account ---
    # GET /api/v3/account (signed). Use EIP-55 addresses in .env if you see 500s.
    try:
        acct: Any = get("/api/v3/account", signed=True, base_url=SAPI_BASE)
    except requests.exceptions.HTTPError as e:
        print()
        print(f"WARN  signed  GET /api/v3/account (spot)  {e}")
        resp = getattr(e, "response", None)
        if resp is not None and resp.text:
            snippet = resp.text.strip().replace("\n", " ")[:400]
            print(f"  Server body: {snippet}")
        print(
            "  Common cause: Pro API agent is futures-only — Aster often returns 500 (not 403) on spot."
            "  In API Wallet, enable Spot for this agent, or run with --no-spot."
        )
        print()
        print("OK  futures balance check complete (spot skipped).")
        return 0
    except Exception as e:
        print()
        print(f"WARN  signed  GET /api/v3/account (spot)  {e}")
        print("  Use --no-spot to skip spot. Perp section above is still valid.")
        print()
        print("OK  futures balance check complete (spot skipped).")
        return 0

    balances = acct.get("balances") if isinstance(acct, dict) else None
    if not isinstance(balances, list):
        print(f"Unexpected spot account response: {acct!r}")
        return 1

    _print_spot_section(balances, args.material, args.max_dust)

    print()
    print("OK  futures + spot balance endpoints responded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
