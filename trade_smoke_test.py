#!/usr/bin/env python3
"""
Minimal live trading smoke test for Aster Spot + USDT-M perps (same .env as the bot).

Default: prints exchange minimums, required balances, and the planned steps — no orders.

  python trade_smoke_test.py              # plan only (safe)
  python trade_smoke_test.py --execute    # place real minimum-size trades (fees apply)

Spot leg (pick one — auto if you pass neither flag):
  • **USDT-first** (default when spot USDT ≥ min notional): BUY with USDT → later SELL base.
  • **ASTER-first** (`--spot-aster-first`, or auto when spot USDT is below min): SELL base first → later BUY
    with USDT (uses ASTER you already hold; sell size includes a small buffer so buyback still meets min
    notional after fees).

Perp leg (same always):
  1) Enables **multi-asset margin** (so **ASTER / USDF** on futures count as collateral, not only USDT).
  2) Market BUY (min notional)
  3) Wait TRADE_TEST_WAIT_SEC (default 30)
  4) Market SELL reduceOnly

Rough margin check: **USDT available + 80%×ASTER×mark + USDF** vs. ~notional/leverage (see
`ASTER_COLLATERAL_RATIO` / `TRADE_TEST_PERP_IM_BUFFER`).

  python trade_smoke_test.py --execute --spot-aster-first   # force sell-ASTER-first spot leg

Spot needs **USDT** (USDT-first) or **base** (ASTER-first). Perp can fund the min order from **small ASTER**
collateral when multi-asset is on. Symbol: `--symbol ASTERUSDT`
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Optional, Tuple

import requests
from dotenv import load_dotenv

load_dotenv()

from aster_client import FAPI_BASE, SAPI_BASE, credentials_ok, get, post  # noqa: E402

# Match funding_farmer.py — ASTER as futures collateral (multi-asset mode)
ASTER_COLLATERAL_RATIO = float(os.getenv("ASTER_COLLATERAL_RATIO", "0.80"))
USDF_COLLATERAL_RATIO = float(os.getenv("USDF_COLLATERAL_RATIO", "0.9999"))
# Rough initial-margin buffer vs notional/leverage (exchange rules vary)
TRADE_TEST_PERP_IM_BUFFER = float(os.getenv("TRADE_TEST_PERP_IM_BUFFER", "1.25"))


def _filters(sym: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {f["filterType"]: f for f in sym.get("filters", [])}


def _min_notional_spot(fm: Dict[str, Dict[str, Any]]) -> float:
    for ft in ("NOTIONAL", "MIN_NOTIONAL"):
        f = fm.get(ft)
        if not f:
            continue
        v = f.get("minNotional") or f.get("notional")
        if v is not None:
            return float(v)
    return 5.0


def _min_notional_perp(fm: Dict[str, Dict[str, Any]]) -> float:
    f = fm.get("MIN_NOTIONAL")
    if f:
        v = f.get("notional") or f.get("minNotional")
        if v is not None:
            return float(v)
    return 5.0


def _step_lot(fm: Dict[str, Dict[str, Any]]) -> str:
    lot = fm.get("LOT_SIZE") or {}
    return str(lot.get("stepSize", "0.01"))


def _step_market_spot(fm: Dict[str, Dict[str, Any]]) -> str:
    m = fm.get("MARKET_LOT_SIZE") or fm.get("LOT_SIZE") or {}
    return str(m.get("stepSize", "0.01"))


def round_step(value: float, step: str) -> str:
    step_d = Decimal(step)
    return str(Decimal(str(value)).quantize(step_d, rounding=ROUND_DOWN))


def perp_qty_for_min_notional(notional_usdt: float, mark: float, step: str) -> str:
    """
    Perp base qty on LOT_SIZE grid with filled notional >= notional_usdt.

    Pure `floor(notional/mark)` can land below min notional (-4164 on Aster/Binance).
    Uses Decimal for qty steps — float `7.47 + 0.01` can snap back to 7.47 on quantize.
    """
    step_d = Decimal(step)
    m = Decimal(str(mark))
    need = Decimal(str(notional_usdt))
    if m <= 0:
        raise ValueError("mark must be positive")
    q = ((need / m) / step_d).to_integral_value(rounding=ROUND_DOWN) * step_d
    while q * m < need:
        q += step_d
        if q > Decimal("1e15"):
            raise ValueError("could not satisfy min notional")
    return str(q.quantize(step_d))


def _fmt_qty_param(x: float) -> str:
    """String for API qty / quoteOrderQty (avoid trailing '.0' when integer)."""
    return str(int(x)) if float(x).is_integer() else str(x)


def load_symbol_infos(symbol: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    spot_ex = get("/api/v3/exchangeInfo", base_url=SAPI_BASE)
    perp_ex = get("/fapi/v1/exchangeInfo")
    spot_sym = next((s for s in spot_ex["symbols"] if s["symbol"] == symbol), None)
    perp_sym = next((s for s in perp_ex["symbols"] if s["symbol"] == symbol), None)
    if not spot_sym or spot_sym.get("status") != "TRADING":
        raise RuntimeError(f"Spot {symbol} not trading or missing")
    if not perp_sym or perp_sym.get("status") != "TRADING":
        raise RuntimeError(f"Perp {symbol} not trading or missing")
    return spot_sym, perp_sym


def spot_usdt_free() -> float:
    acct = get("/api/v3/account", signed=True, base_url=SAPI_BASE)
    for b in acct.get("balances", []):
        if b.get("asset") == "USDT":
            return float(b.get("free", 0) or 0)
    return 0.0


def perp_usdt_available() -> float:
    rows = get("/fapi/v2/balance", signed=True)
    for b in rows:
        if b.get("asset") == "USDT":
            return float(b.get("availableBalance", 0) or 0)
    return 0.0


def perp_asset_wallet(asset: str) -> float:
    rows = get("/fapi/v2/balance", signed=True)
    for b in rows:
        if b.get("asset") == asset:
            return float(b.get("balance", 0) or 0)
    return 0.0


def perp_effective_margin_usdt() -> Tuple[float, str]:
    """
    Rough USDT-equivalent collateral: USDT available + discounted ASTER/USDF wallet (like funding_farmer).
    """
    rows = get("/fapi/v2/balance", signed=True)
    aster_px = float(get("/fapi/v1/premiumIndex", {"symbol": "ASTERUSDT"})["markPrice"])
    usdt = 0.0
    aster_bal = 0.0
    usdf_bal = 0.0
    for b in rows:
        a = b.get("asset", "")
        if a == "USDT":
            usdt = float(b.get("availableBalance", 0) or 0)
        elif a == "ASTER":
            aster_bal = float(b.get("balance", 0) or 0)
        elif a == "USDF":
            usdf_bal = float(b.get("balance", 0) or 0)
    eff_aster = aster_bal * aster_px * ASTER_COLLATERAL_RATIO
    eff_usdf = usdf_bal * USDF_COLLATERAL_RATIO
    eff = usdt + eff_aster + eff_usdf
    parts = [f"USDT avail {usdt:.4f}"]
    if aster_bal > 0:
        parts.append(f"ASTER {aster_bal:.4f} (~${eff_aster:.2f} @ {aster_px:.4f}×{ASTER_COLLATERAL_RATIO:g})")
    if usdf_bal > 0:
        parts.append(f"USDF {usdf_bal:.4f} (~${eff_usdf:.2f})")
    return eff, " + ".join(parts)


def perp_initial_margin_estimate(notional_usdt: float, leverage: int) -> float:
    """Conservative IM guess: notional / leverage × buffer."""
    lev = max(leverage, 1)
    return notional_usdt / lev * TRADE_TEST_PERP_IM_BUFFER


def ensure_multi_asset_margin() -> None:
    """So ASTER/USDF futures wallet counts as margin (idempotent)."""
    try:
        r = get("/fapi/v1/multiAssetsMargin", signed=True)
        if r.get("multiAssetsMargin") is True:
            return
    except Exception:
        pass
    try:
        post("/fapi/v1/multiAssetsMargin", {"multiAssetsMargin": "true"})
    except RuntimeError as e:
        if "No need" not in str(e):
            raise


def spot_base_free(asset: str) -> float:
    acct = get("/api/v3/account", signed=True, base_url=SAPI_BASE)
    for b in acct.get("balances", []):
        if b.get("asset") == asset:
            return float(b.get("free", 0) or 0)
    return 0.0


def perp_mark_price(symbol: str) -> float:
    return float(get("/fapi/v1/premiumIndex", {"symbol": symbol})["markPrice"])


def spot_last_price(symbol: str) -> float:
    return float(
        get("/api/v3/ticker/price", {"symbol": symbol}, base_url=SAPI_BASE)["price"]
    )


def perp_position_amt(symbol: str) -> float:
    rows = get("/fapi/v2/positionRisk", signed=True)
    for p in rows:
        if p.get("symbol") == symbol:
            return float(p.get("positionAmt", 0) or 0)
    return 0.0


def print_balances() -> None:
    print()
    print("--- Balances ---")
    print(f"Spot  {SAPI_BASE}/api/v3/account")
    acct = get("/api/v3/account", signed=True, base_url=SAPI_BASE)
    hdr = f"  {'Asset':<10} {'Free':>18} {'Locked':>18}"
    print(hdr)
    for b in sorted(acct.get("balances", []), key=lambda x: x.get("asset", "")):
        free = float(b.get("free", 0) or 0)
        locked = float(b.get("locked", 0) or 0)
        if free + locked <= 0:
            continue
        print(f"  {b.get('asset',''):<10} {free:>18.8f} {locked:>18.8f}")

    print()
    print(f"Perp  {FAPI_BASE}/fapi/v3/balance")
    rows = get("/fapi/v2/balance", signed=True)
    hdr2 = f"  {'Asset':<10} {'Available':>18}"
    print(hdr2)
    for b in sorted(rows, key=lambda x: x.get("asset", "")):
        a = b.get("asset", "")
        av = float(b.get("availableBalance", 0) or 0)
        bal = float(b.get("balance", 0) or 0)
        if max(abs(av), abs(bal)) <= 0:
            continue
        print(f"  {a:<10} {av:>18.8f}")


def _fapi_http_error_body(exc: BaseException) -> Optional[dict]:
    """POST /fapi raises requests.HTTPError on 4xx before aster_client parses API `code`."""
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        try:
            j = exc.response.json()
            return j if isinstance(j, dict) else None
        except Exception:
            return None
    return None


def _benign_margin_type_change(exc: BaseException) -> bool:
    """Already CROSSED / no-op margin type (Binance-style -4046)."""
    j = _fapi_http_error_body(exc)
    if j:
        code = j.get("code")
        msg = str(j.get("msg", ""))
        if str(code) in ("-4046", "4046") or "no need" in msg.lower():
            return True
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        try:
            t = exc.response.text or ""
            if "No need" in t or "-4046" in t:
                return True
        except Exception:
            pass
    if isinstance(exc, RuntimeError) and (
        "No need" in str(exc) or "-4046" in str(exc)
    ):
        return True
    return False


def _benign_leverage_change(exc: BaseException) -> bool:
    """Leverage already at requested level or similar no-op."""
    j = _fapi_http_error_body(exc)
    if j:
        msg = str(j.get("msg", "")).lower()
        if "no need" in msg or "not change" in msg:
            return True
    if isinstance(exc, RuntimeError) and "No need" in str(exc):
        return True
    return False


def _set_cross_and_leverage(symbol: str, leverage: int) -> None:
    try:
        post("/fapi/v1/marginType", {"symbol": symbol, "marginType": "CROSSED"})
    except Exception as e:
        if not _benign_margin_type_change(e):
            raise
    try:
        post("/fapi/v1/leverage", {"symbol": symbol, "leverage": str(leverage)})
    except Exception as e:
        if not _benign_leverage_change(e):
            raise


def resolve_spot_mode(
    symbol: str,
    base: str,
    su: float,
    base_free: float,
    spot_min: float,
    spot_price: float,
    step_spot: str,
    force_aster: bool,
    force_usdt: bool,
) -> Tuple[bool, str]:
    """Returns (aster_first, reason)."""
    if force_aster and force_usdt:
        raise ValueError("use only one of --spot-aster-first or --spot-usdt-first")
    if force_aster:
        buf = float(os.getenv("TRADE_TEST_ASTER_SELL_BUFFER", "1.04"))
        qty = float(round_step(spot_min * buf / spot_price, step_spot))
        need = qty
        if base_free + 1e-12 < need:
            need = float(round_step(spot_min / spot_price, step_spot))
        if base_free + 1e-12 < need:
            raise ValueError(
                f"--spot-aster-first needs ~{need:.4f} {base} free at this price; have {base_free:.8f}"
            )
        if need * spot_price < spot_min * 0.98:
            raise ValueError("Calculated SELL size below spot min notional; try a different buffer or price")
        return True, "--spot-aster-first"
    if force_usdt:
        if su < spot_min * 0.999:
            raise ValueError(
                f"--spot-usdt-first requires ≥{spot_min} spot USDT; have {su:.8f}"
            )
        return False, "--spot-usdt-first"
    if su >= spot_min * 0.999:
        return False, "auto (spot USDT ≥ min notional)"
    buf = float(os.getenv("TRADE_TEST_ASTER_SELL_BUFFER", "1.04"))
    qty = float(round_step(spot_min * buf / spot_price, step_spot))
    notion = qty * spot_price
    if base_free + 1e-12 >= qty and notion >= spot_min * 0.99:
        return True, "auto (spot USDT below min; using ASTER-first)"
    qty2 = float(round_step(spot_min / spot_price, step_spot))
    raise ValueError(
        f"Spot leg: need either ≥{spot_min} USDT on spot, or ≥{qty2:.4f} {base} free "
        f"(~{spot_min} USDT notional @ {spot_price:.6f}). "
        f"Have USDT {su:.8f}, {base} {base_free:.8f}."
    )


def run_plan(
    symbol: str,
    spot_sym: Dict[str, Any],
    perp_sym: Dict[str, Any],
    aster_first: bool,
    mode_reason: str,
) -> None:
    sfm = _filters(spot_sym)
    pfm = _filters(perp_sym)
    spot_min = _min_notional_spot(sfm)
    perp_min = _min_notional_perp(pfm)
    mark = perp_mark_price(symbol)
    step_p = _step_lot(pfm)
    qty_perp_s = perp_qty_for_min_notional(perp_min, mark, step_p)
    qty_perp = float(qty_perp_s)
    step_spot = _step_market_spot(sfm)
    spot_px = spot_last_price(symbol)

    base = spot_sym.get("baseAsset", symbol.replace("USDT", ""))
    buf = float(os.getenv("TRADE_TEST_ASTER_SELL_BUFFER", "1.04"))
    qty_sell_ast = float(round_step(spot_min * buf / spot_px, step_spot))

    print(f"Symbol:        {symbol}")
    print(f"Spot base:     {base}")
    print(f"Spot leg:      {'ASTER-first (sell base, then buy back)' if aster_first else 'USDT-first (buy, then sell base)'}  [{mode_reason}]")
    print(f"Spot min notional (USDT):  {spot_min}")
    print(f"Spot last price:           {spot_px}")
    if aster_first:
        print(f"Planned SELL qty (≈{buf:g}× min notional):  {qty_sell_ast} {base}")
    print(f"Perp min notional (USDT):  {perp_min}")
    print(f"Perp mark:                 {mark}")
    print(f"Perp min qty (approx):     {qty_perp}  (step {step_p})")
    print()
    su = spot_usdt_free()
    bf = spot_base_free(base)
    lev = int(os.getenv("TRADE_TEST_LEVERAGE", "5"))
    need_im = perp_initial_margin_estimate(perp_min, lev)
    eff, eff_detail = perp_effective_margin_usdt()
    pu = perp_usdt_available()
    pa = perp_asset_wallet("ASTER")
    print(f"Spot USDT free:         {su:.8f}")
    print(f"Spot {base} free:       {bf:.8f}")
    print(f"Perp USDT available:    {pu:.8f}")
    print(f"Perp ASTER (wallet):    {pa:.8f}  (multi-asset margin)")
    print(f"Perp margin (rough):    ~${eff:.2f} effective  (need ~${need_im:.2f} IM @ {lev}x for {perp_min} USDT notional)")
    print(f"  {eff_detail}")
    print()
    print("Execute sequence:")
    if aster_first:
        print("  1) POST spot  /api/v3/order  MARKET SELL quantity≈%s (min notional + fee buffer)" % qty_sell_ast)
        print("  2) POST perp  /fapi/v3/order MARKET BUY  quantity≈%s" % qty_perp)
    else:
        print("  1) POST spot  /api/v3/order  MARKET BUY  quoteOrderQty=%s" % _fmt_qty_param(spot_min))
        print("  2) POST perp  /fapi/v3/order MARKET BUY  quantity≈%s" % qty_perp)
    print("  3) sleep %s s" % os.getenv("TRADE_TEST_WAIT_SEC", "30"))
    if aster_first:
        print("  4) POST spot  /api/v3/order  MARKET BUY  quoteOrderQty=%s (buy back)" % _fmt_qty_param(spot_min))
    else:
        print("  4) POST spot  /api/v3/order  MARKET SELL quantity=<%s free, stepped>" % base)
    print("  5) POST perp  /fapi/v3/order MARKET SELL reduceOnly")


def run_execute(
    symbol: str,
    spot_sym: Dict[str, Any],
    perp_sym: Dict[str, Any],
    wait_sec: int,
    aster_first: bool,
) -> int:
    sfm = _filters(spot_sym)
    pfm = _filters(perp_sym)
    spot_min = _min_notional_spot(sfm)
    perp_min = _min_notional_perp(pfm)
    step_spot = _step_market_spot(sfm)
    step_perp = _step_lot(pfm)
    base = spot_sym.get("baseAsset", symbol.replace("USDT", ""))

    su = spot_usdt_free()
    bf = spot_base_free(base)
    pu = perp_usdt_available()
    spot_px = spot_last_price(symbol)

    if not aster_first and su < spot_min * 0.999:
        print(
            f"ERROR: USDT-first requires spot USDT ≥ {spot_min}; have {su}. "
            f"Use --spot-aster-first or deposit USDT.",
            file=sys.stderr,
        )
        return 1

    if aster_first:
        buf = float(os.getenv("TRADE_TEST_ASTER_SELL_BUFFER", "1.04"))
        qty_sell = round_step(spot_min * buf / spot_px, step_spot)
        if float(qty_sell) > bf + 1e-12:
            qty_sell = round_step(spot_min / spot_px, step_spot)
        if float(qty_sell) > bf + 1e-12:
            print(
                f"ERROR: ASTER-first needs ≥{qty_sell} {base} free; have {bf}",
                file=sys.stderr,
            )
            return 1
        if float(qty_sell) * spot_px < spot_min * 0.98:
            print("ERROR: sell size below min notional at spot price", file=sys.stderr)
            return 1

    lev = int(os.getenv("TRADE_TEST_LEVERAGE", "5"))
    need_im = perp_initial_margin_estimate(perp_min, lev)
    eff, eff_detail = perp_effective_margin_usdt()
    if eff < need_im * 0.999:
        print(
            f"ERROR: perp collateral (rough) ~${eff:.2f} < ~${need_im:.2f} IM needed @ {lev}x "
            f"for {perp_min} USDT notional. Deposit USDT or ASTER on **futures** wallet, or enable "
            f"multi-asset margin.\n  {eff_detail}",
            file=sys.stderr,
        )
        return 1
    print(
        f"Perp margin OK (rough): ~${eff:.2f} effective vs ~${need_im:.2f} IM — {eff_detail}"
    )

    print("=== Before ===")
    print_balances()

    print()
    if aster_first:
        print("--- 1) Spot MARKET SELL (ASTER-first) ---")
        o1 = post(
            "/api/v3/order",
            {
                "symbol": symbol,
                "side": "SELL",
                "type": "MARKET",
                "quantity": qty_sell,
            },
            base_url=SAPI_BASE,
        )
        print(
            f"  orderId={o1.get('orderId')} status={o1.get('status')} qty={qty_sell}"
        )
    else:
        print("--- 1) Spot MARKET BUY (USDT-first) ---")
        q_quote = _fmt_qty_param(spot_min)
        o1 = post(
            "/api/v3/order",
            {
                "symbol": symbol,
                "side": "BUY",
                "type": "MARKET",
                "quoteOrderQty": q_quote,
            },
            base_url=SAPI_BASE,
        )
        print(
            f"  orderId={o1.get('orderId')} status={o1.get('status')} fills={o1.get('fills', [])[:1]}..."
        )

    print()
    print("--- 2) Perp MARKET BUY (long) ---")
    ensure_multi_asset_margin()
    _set_cross_and_leverage(symbol, lev)
    mark = perp_mark_price(symbol)
    qty_buy = perp_qty_for_min_notional(perp_min, mark, step_perp)
    o2 = post(
        "/fapi/v1/order",
        {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quantity": qty_buy,
        },
    )
    print(f"  orderId={o2.get('orderId')} status={o2.get('status')} qty={qty_buy}")

    print()
    print(f"--- 3) Wait {wait_sec}s ---")
    time.sleep(wait_sec)

    print()
    if aster_first:
        print("--- 4) Spot MARKET BUY (buy back min notional) ---")
        u = spot_usdt_free()
        if u < spot_min * 0.999:
            print(
                f"  ERROR: USDT after sell {u} < min notional {spot_min} "
                f"(raise TRADE_TEST_ASTER_SELL_BUFFER or add USDT)",
                file=sys.stderr,
            )
            return 1
        o3 = post(
            "/api/v3/order",
            {
                "symbol": symbol,
                "side": "BUY",
                "type": "MARKET",
                "quoteOrderQty": _fmt_qty_param(spot_min),
            },
            base_url=SAPI_BASE,
        )
        print(
            f"  orderId={o3.get('orderId')} status={o3.get('status')} quoteOrderQty={spot_min}"
        )
    else:
        print("--- 4) Spot MARKET SELL (base) ---")
        free_base = spot_base_free(base)
        qty_sell_spot = round_step(free_base, step_spot)
        if float(qty_sell_spot) <= 0:
            print(f"  ERROR: no {base} to sell (free={free_base})", file=sys.stderr)
        else:
            o3 = post(
                "/api/v3/order",
                {
                    "symbol": symbol,
                    "side": "SELL",
                    "type": "MARKET",
                    "quantity": qty_sell_spot,
                },
                base_url=SAPI_BASE,
            )
            print(
                f"  orderId={o3.get('orderId')} status={o3.get('status')} qty={qty_sell_spot}"
            )

    print()
    print("--- 5) Perp MARKET SELL (reduceOnly) ---")
    amt = perp_position_amt(symbol)
    if amt <= 0:
        print(f"  WARN: no long position on {symbol} (amt={amt})", file=sys.stderr)
    else:
        qty_close = round_step(amt, step_perp)
        o4 = post(
            "/fapi/v1/order",
            {
                "symbol": symbol,
                "side": "SELL",
                "type": "MARKET",
                "quantity": qty_close,
                "reduceOnly": "true",
            },
        )
        print(f"  orderId={o4.get('orderId')} status={o4.get('status')} qty={qty_close}")

    print()
    print("=== After ===")
    print_balances()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Spot + perp minimum trade smoke test")
    ap.add_argument("--symbol", default=os.getenv("TRADE_TEST_SYMBOL", "ASTERUSDT"))
    ap.add_argument(
        "--execute",
        action="store_true",
        help="Place real orders (default: show plan only)",
    )
    ap.add_argument(
        "--wait",
        type=int,
        default=int(os.getenv("TRADE_TEST_WAIT_SEC", "30")),
        help="Seconds between open and close (default 30 or TRADE_TEST_WAIT_SEC)",
    )
    spot = ap.add_mutually_exclusive_group()
    spot.add_argument(
        "--spot-aster-first",
        action="store_true",
        help="Spot: sell base first, buy back with USDT (use ASTER you already hold)",
    )
    spot.add_argument(
        "--spot-usdt-first",
        action="store_true",
        help="Spot: buy with USDT first, sell base after (needs ≥ min USDT on spot)",
    )
    args = ap.parse_args()

    if not credentials_ok():
        print("Set Pro API V3 or legacy keys in .env (see .env.example).", file=sys.stderr)
        return 1

    try:
        spot_sym, perp_sym = load_symbol_infos(args.symbol)
    except Exception as e:
        print(f"exchangeInfo: {e}", file=sys.stderr)
        return 1

    sfm = _filters(spot_sym)
    step_spot = _step_market_spot(sfm)
    base = spot_sym.get("baseAsset", args.symbol.replace("USDT", ""))
    spot_min = _min_notional_spot(sfm)
    try:
        spot_px = spot_last_price(args.symbol)
        su = spot_usdt_free()
        bf = spot_base_free(base)
        aster_first, mode_reason = resolve_spot_mode(
            args.symbol,
            base,
            su,
            bf,
            spot_min,
            spot_px,
            step_spot,
            args.spot_aster_first,
            args.spot_usdt_first,
        )
    except ValueError as e:
        print(f"{e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Spot mode / price: {e}", file=sys.stderr)
        return 1

    if not args.execute:
        run_plan(args.symbol, spot_sym, perp_sym, aster_first, mode_reason)
        print()
        print("No orders sent. Re-run with --execute to trade (fees apply).")
        print(
            "Tip: with little spot USDT, omit flags — auto picks ASTER-first when possible."
        )
        return 0

    return run_execute(args.symbol, spot_sym, perp_sym, args.wait, aster_first)


if __name__ == "__main__":
    sys.exit(main())
